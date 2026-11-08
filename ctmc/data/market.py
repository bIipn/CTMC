"""Market data from every Solana DEX, async.

DexScreener   all pools for a token on every DEX (Raydium, Orca, Meteora, Pump AMM, ...)
              -> liquidity, volume and buy/sell counts are SUMMED across pools.
Jupiter       aggregated price across all routes (1s refresh).
Birdeye       optional (BIRDEYE_API_KEY): holders, unique wallets, websocket prices.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from ctmc.models import PairSnapshot

DEX = "https://api.dexscreener.com"
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
BIRDEYE = "https://public-api.birdeye.so"


def _f(d: dict | None, k: str) -> float:
    try:
        return float((d or {}).get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def aggregate_pairs(pairs: list[dict], chain: str = "solana") -> PairSnapshot | None:
    """Fold every pool for one token into a single snapshot."""
    pairs = [p for p in pairs if p.get("chainId") == chain and _f(p, "priceUsd") > 0]
    if not pairs:
        return None
    deepest = max(pairs, key=lambda p: _f(p.get("liquidity"), "usd"))
    pools: dict[str, float] = {}
    for p in pairs:
        pools[p.get("dexId", "?")] = pools.get(p.get("dexId", "?"), 0) + _f(p.get("liquidity"), "usd")

    def total(section: str, key: str) -> float:
        return sum(_f(p.get(section), key) for p in pairs)

    def txns(window: str, side: str) -> int:
        return int(sum(_f((p.get("txns") or {}).get(window), side) for p in pairs))

    created = [int(p["pairCreatedAt"]) for p in pairs if p.get("pairCreatedAt")]
    base = deepest.get("baseToken") or {}
    return PairSnapshot(
        token=base.get("address", ""), symbol=base.get("symbol", "?"),
        pair_address=deepest.get("pairAddress", ""), dex=deepest.get("dexId", ""),
        price_usd=_f(deepest, "priceUsd"),
        liquidity_usd=sum(pools.values()),
        volume_24h=total("volume", "h24"), volume_h1=total("volume", "h1"), volume_m5=total("volume", "m5"),
        change_m5=_f(deepest.get("priceChange"), "m5"), change_h1=_f(deepest.get("priceChange"), "h1"),
        buys_m5=txns("m5", "buys"), sells_m5=txns("m5", "sells"),
        buys_h1=txns("h1", "buys"), sells_h1=txns("h1", "sells"),
        created_at_ms=min(created) if created else 0,
        pools=pools, market_cap=_f(deepest, "marketCap") or _f(deepest, "fdv"),
        name=base.get("name", ""),
    )


class Market:
    def __init__(self, chain: str = "solana", http: httpx.AsyncClient | None = None):
        self.chain = chain
        self.http = http or httpx.AsyncClient(timeout=8, http2=False)
        self.birdeye_key = os.getenv("BIRDEYE_API_KEY", "")

    async def snapshot(self, mint: str) -> PairSnapshot | None:
        r = await self.http.get(f"{DEX}/token-pairs/v1/{self.chain}/{mint}")
        r.raise_for_status()
        data = r.json()
        snap = aggregate_pairs(data if isinstance(data, list) else data.get("pairs") or [], self.chain)
        if snap and self.birdeye_key:
            await self._enrich_birdeye(snap)
        return snap

    async def snapshots(self, mints: list[str]) -> dict[str, PairSnapshot]:
        out = await asyncio.gather(*(self.snapshot(m) for m in mints), return_exceptions=True)
        return {m: s for m, s in zip(mints, out) if isinstance(s, PairSnapshot)}

    async def search_ticker(self, ticker: str, min_liquidity: float) -> PairSnapshot | None:
        """$TICKER -> the single dominant coin with that symbol, or None if ambiguous/small."""
        r = await self.http.get(f"{DEX}/latest/dex/search", params={"q": ticker})
        r.raise_for_status()
        by_token: dict[str, list[dict]] = {}
        for p in r.json().get("pairs") or []:
            if p.get("chainId") == self.chain and (p.get("baseToken") or {}).get("symbol", "").upper() == ticker.upper():
                by_token.setdefault(p["baseToken"]["address"], []).append(p)
        snaps = sorted((s for s in (aggregate_pairs(v, self.chain) for v in by_token.values()) if s),
                       key=lambda s: s.liquidity_usd, reverse=True)
        if not snaps or snaps[0].liquidity_usd < min_liquidity:
            return None
        # copycat tokens share tickers; demand the leader be at least 10x the runner-up
        if len(snaps) > 1 and snaps[1].liquidity_usd * 10 > snaps[0].liquidity_usd:
            return None
        return snaps[0]

    async def search_name(self, keyword: str) -> list[PairSnapshot]:
        """Every coin on our chain whose search hit fits `keyword` (one snapshot per token)."""
        r = await self.http.get(f"{DEX}/latest/dex/search", params={"q": keyword})
        r.raise_for_status()
        by_token: dict[str, list[dict]] = {}
        for p in r.json().get("pairs") or []:
            if p.get("chainId") == self.chain and (p.get("baseToken") or {}).get("address"):
                by_token.setdefault(p["baseToken"]["address"], []).append(p)
        return [s for s in (aggregate_pairs(v, self.chain) for v in by_token.values()) if s]

    async def prices(self, mints: list[str]) -> dict[str, float]:
        """Jupiter aggregated price (all routes) for up to 50 mints in one call."""
        if not mints:
            return {}
        r = await self.http.get(JUP_PRICE, params={"ids": ",".join(mints[:50])})
        r.raise_for_status()
        return {m: float(v["usdPrice"]) for m, v in (r.json() or {}).items() if v and v.get("usdPrice")}

    async def _enrich_birdeye(self, s: PairSnapshot) -> None:
        try:
            r = await self.http.get(f"{BIRDEYE}/defi/token_overview", params={"address": s.token},
                                    headers={"X-API-KEY": self.birdeye_key, "x-chain": self.chain})
            d = (r.json() or {}).get("data") or {}
            s.holders = int(d.get("holder") or 0)
            s.unique_wallets_1h = int(d.get("uniqueWallet1h") or 0)
        except (httpx.HTTPError, ValueError):
            pass


class PriceFeed:
    """Continuous prices for open positions + leaders. Calls back on every tick."""

    def __init__(self, market: Market, interval: float = 1.0):
        self.market = market
        self.interval = interval
        self.watch: set[str] = set()
        self.last: dict[str, tuple[float, float]] = {}

    async def run(self, on_price) -> None:
        while True:
            started = time.monotonic()
            if self.watch:
                try:
                    for mint, px in (await self.market.prices(sorted(self.watch))).items():
                        self.last[mint] = (time.time(), px)
                        await on_price(mint, px)
                except (httpx.HTTPError, ValueError):
                    pass
            await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - started)))
