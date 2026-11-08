"""DexScreener public API — prices, liquidity, volume and buy/sell counts.

Free, no key. Rate limit ~300 req/min on the pairs/tokens endpoints.
"""
from __future__ import annotations

import requests

from ctmc.models import PairSnapshot

BASE = "https://api.dexscreener.com"
TIMEOUT = 10


def _num(d: dict | None, key: str) -> float:
    try:
        return float((d or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_pair(p: dict) -> PairSnapshot:
    txns = p.get("txns") or {}
    vol = p.get("volume") or {}
    chg = p.get("priceChange") or {}
    base = p.get("baseToken") or {}
    return PairSnapshot(
        token=base.get("address", ""),
        symbol=base.get("symbol", "?"),
        pair_address=p.get("pairAddress", ""),
        dex=p.get("dexId", ""),
        price_usd=_num(p, "priceUsd"),
        liquidity_usd=_num(p.get("liquidity"), "usd"),
        volume_24h=_num(vol, "h24"),
        volume_h1=_num(vol, "h1"),
        volume_m5=_num(vol, "m5"),
        change_m5=_num(chg, "m5"),
        change_h1=_num(chg, "h1"),
        buys_m5=int(_num(txns.get("m5"), "buys")),
        sells_m5=int(_num(txns.get("m5"), "sells")),
        buys_h1=int(_num(txns.get("h1"), "buys")),
        sells_h1=int(_num(txns.get("h1"), "sells")),
        created_at_ms=int(p.get("pairCreatedAt") or 0),
    )


class DexScreener:
    def __init__(self, chain: str = "solana", session: requests.Session | None = None):
        self.chain = chain
        self.http = session or requests.Session()

    def _get(self, path: str) -> dict | list:
        r = self.http.get(f"{BASE}{path}", timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def best_pair(self, token: str) -> PairSnapshot | None:
        """Deepest-liquidity pair for a token on our chain."""
        data = self._get(f"/tokens/v1/{self.chain}/{token}")
        pairs = data if isinstance(data, list) else data.get("pairs") or []
        pairs = [p for p in pairs if p.get("chainId") == self.chain]
        if not pairs:
            return None
        top = max(pairs, key=lambda p: _num(p.get("liquidity"), "usd"))
        return parse_pair(top)

    def trending_tokens(self) -> list[str]:
        """Candidate mints: boosted + latest profiles on our chain (de-duplicated)."""
        seen: dict[str, None] = {}
        for path in ("/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1"):
            try:
                for row in self._get(path) or []:
                    if row.get("chainId") == self.chain and row.get("tokenAddress"):
                        seen.setdefault(row["tokenAddress"], None)
            except requests.RequestException:
                continue
        return list(seen)
