"""Live KOL / celebrity wallet tracker.

For every tracked wallet we hold an RPC websocket subscription
(logsSubscribe with `mentions: [wallet]` — one wallet per subscription, all on one
connection). Each new transaction signature is fetched (getTransaction, jsonParsed)
and turned into buys/sells from the wallet's own balance changes, so it works on
every DEX and launchpad, not just one.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from ctmc.kol.registry import Kol, Registry

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWJ5DtsqmT6PrCNTP9e3ArWn8GFbQiZgYMg9oWtpc", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
HELIUS_WS = "wss://atlas-mainnet.helius-rpc.com/?api-key={key}"     # enhanced websocket (Business plans)


@dataclass
class KolTrade:
    wallet: str
    kol: Kol
    mint: str
    side: str                # "buy" | "sell"
    tokens: float            # absolute token amount moved
    sol: float               # absolute SOL (incl. wrapped) moved; 0 if paid in stables
    balance_before: float
    balance_after: float
    signature: str
    ts: float = field(default_factory=time.time)

    @property
    def fraction_sold(self) -> float:
        return min(1.0, self.tokens / self.balance_before) if self.side == "sell" and self.balance_before else 0.0


def parse_wallet_trades(tx: dict, wallet: str) -> list[dict]:
    """Pure: one jsonParsed transaction -> the wallet's token buys/sells.

    Uses meta.pre/postTokenBalances filtered by owner, and the wallet's lamport change
    (+ wrapped SOL) as the SOL leg. Returns dicts with mint, side, tokens, sol, before, after.
    """
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    def by_mint(rows):
        out: dict[str, float] = defaultdict(float)
        for r in rows or []:
            if r.get("owner") == wallet:
                out[r["mint"]] += float((r.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        return out
    pre, post = by_mint(meta.get("preTokenBalances")), by_mint(meta.get("postTokenBalances"))

    keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys") or []
    idx = next((i for i, k in enumerate(keys) if (k.get("pubkey") if isinstance(k, dict) else k) == wallet), None)
    sol = 0.0
    if idx is not None and idx < len(meta.get("preBalances") or []):
        sol = (meta["postBalances"][idx] - meta["preBalances"][idx]) / 1e9
        if idx == 0:
            sol += float(meta.get("fee") or 0) / 1e9            # ignore the network fee
    sol += post.get(WSOL, 0.0) - pre.get(WSOL, 0.0)

    trades = []
    for mint in set(pre) | set(post):
        if mint == WSOL or mint in STABLES:
            continue
        d = post.get(mint, 0.0) - pre.get(mint, 0.0)
        if abs(d) <= 1e-12:
            continue
        side = "buy" if d > 0 else "sell"
        # a transfer in/out with no SOL leg is not a trade (airdrop, wallet shuffle)
        stable_leg = any(abs(post.get(s, 0) - pre.get(s, 0)) > 0 for s in STABLES)
        if not stable_leg and ((side == "buy" and sol >= 0) or (side == "sell" and sol <= 0)):
            continue
        trades.append({"mint": mint, "side": side, "tokens": abs(d), "sol": abs(sol),
                       "before": pre.get(mint, 0.0), "after": post.get(mint, 0.0)})
    return trades


OnTrade = Callable[[KolTrade], Awaitable[None]]


class KolTracker:
    def __init__(self, registry: Registry, rules: dict, on_trade: OnTrade, http: httpx.AsyncClient | None = None,
                 store=None):
        self.registry = registry
        self.store = store                              # holdings index survives restarts
        self.mode_note = ""
        self.rules = rules
        self.on_trade = on_trade
        self.http = http or httpx.AsyncClient(timeout=15)
        self.rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.ws_url = os.getenv("SOLANA_WS_URL") or self.rpc.replace("https://", "wss://").replace("http://", "ws://")
        self._req: dict[int, str] = {}
        self._subs: dict[int, str] = {}
        self._seen: set[str] = set()
        self.connected = False
        # mint -> wallet -> latest known balance (what the smart money holds right now)
        self.holdings: dict[str, dict[str, float]] = defaultdict(dict)
        self.first_buy: dict[tuple[str, str], float] = {}         # (wallet, mint) -> ts
        self.recent: list[KolTrade] = []
        if store is not None:
            for wallet, mint, bal in store.smart_holdings(time.time() - 14 * 86400):
                self.holdings[mint][wallet] = bal

    async def _rpc(self, method: str, params: list):
        r = await self.http.post(self.rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        out = r.json()
        if "error" in out:
            raise RuntimeError(out["error"])
        return out["result"]

    async def handle_signature(self, wallet: str, signature: str) -> None:
        if signature in self._seen:
            return
        self._seen.add(signature)
        if len(self._seen) > 50_000:
            self._seen.clear()
        kol = self.registry.by_wallet.get(wallet)
        if not kol:
            return
        tx = None
        for _ in range(3):                         # the tx can lag the log notification briefly
            try:
                tx = await self._rpc("getTransaction", [signature, {"encoding": "jsonParsed",
                                     "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
            except (httpx.HTTPError, RuntimeError):
                tx = None
            if tx:
                break
            await asyncio.sleep(0.4)
        for t in parse_wallet_trades(tx or {}, wallet):
            await self.record(KolTrade(wallet, kol, t["mint"], t["side"], t["tokens"], t["sol"],
                                       t["before"], t["after"], signature))

    async def record(self, trade: KolTrade) -> None:
        self.holdings[trade.mint][trade.wallet] = trade.balance_after
        if self.store is not None:
            self.store.smart_holding(trade.wallet, trade.mint, trade.balance_after)
        if trade.side == "buy":
            self.first_buy.setdefault((trade.wallet, trade.mint), trade.ts)
        self.recent.append(trade)
        del self.recent[:-int(self.rules.get("recent_trades_kept", 50_000))]
        await self.on_trade(trade)

    def buyers(self, mint: str, within_s: float | None = None) -> list[KolTrade]:
        """Distinct tracked wallets that bought `mint` (optionally within the last N seconds)."""
        now, seen, out = time.time(), set(), []
        for t in reversed(self.recent):
            if t.mint == mint and t.side == "buy" and t.wallet not in seen and (within_s is None or now - t.ts <= within_s):
                seen.add(t.wallet)
                out.append(t)
        return out

    async def balances_of(self, mint: str, wallets: list[str]) -> dict[str, float]:
        """Point-in-time check: which of these wallets hold `mint` right now (parallel RPC)."""
        async def one(w: str) -> tuple[str, float]:
            try:
                res = await self._rpc("getTokenAccountsByOwner", [w, {"mint": mint}, {"encoding": "jsonParsed"}])
                return w, sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                              for a in res["value"])
            except (httpx.HTTPError, RuntimeError, KeyError, TypeError):
                return w, 0.0
        got = dict(await asyncio.gather(*(one(w) for w in wallets)))
        for w, bal in got.items():
            if bal > 0:
                self.holdings[mint][w] = bal
        return got

    async def run(self) -> None:
        """Stream every watched wallet. Helius enhanced websocket when available (one subscription
        covers thousands of wallets and delivers the full transaction); otherwise standard RPC
        log subscriptions, sharded across connections."""
        wallets = self.registry.wallets(self.rules)
        if not wallets:
            await asyncio.Event().wait()                     # nothing to track: idle quietly
        mode = self.rules.get("tracker_mode", "auto")
        if mode == "helius" or (mode == "auto" and os.getenv("HELIUS_API_KEY")):
            if await self._run_helius(wallets) == "unsupported":
                self.mode_note = "Helius enhanced websocket not available on this plan — using standard RPC subscriptions"
            else:
                return
        per = max(1, int(self.rules.get("wallets_per_connection", 100)))
        shards = [wallets[i:i + per] for i in range(0, len(wallets), per)]
        await asyncio.gather(*(self._run_shard(sh) for sh in shards))

    async def _run_shard(self, wallets: list[str]) -> None:
        import websockets

        while True:
            req: dict[int, str] = {}
            subs: dict[int, str] = {}
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, max_size=2**22) as ws:
                    self.connected = True
                    for i, w in enumerate(wallets, start=1):
                        req[i] = w
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                                  "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                    async for raw in ws:
                        await self.handle_message(json.loads(raw), req, subs)
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
                self.connected = False
                await asyncio.sleep(2)

    async def handle_message(self, msg: dict, req: dict | None = None, subs: dict | None = None) -> None:
        req = self._req if req is None else req
        subs = self._subs if subs is None else subs
        if "id" in msg and "result" in msg:
            w = req.pop(msg["id"], None)
            if w:
                subs[msg["result"]] = w
        elif msg.get("method") == "logsNotification":
            p = msg["params"]
            w = subs.get(p["subscription"])
            val = (p.get("result") or {}).get("value") or {}
            if w and not val.get("err") and val.get("signature"):
                asyncio.create_task(self.handle_signature(w, val["signature"]))

    async def _run_helius(self, wallets: list[str]) -> str:
        import websockets

        url = HELIUS_WS.format(key=os.getenv("HELIUS_API_KEY", ""))
        watched = set(wallets)
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, max_size=2**24) as ws:
                    self.connected = True
                    for n, i in enumerate(range(0, len(wallets), 1000), start=1):
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": n, "method": "transactionSubscribe", "params": [
                            {"accountInclude": wallets[i:i + 1000], "vote": False, "failed": False},
                            {"commitment": "confirmed", "encoding": "jsonParsed", "transactionDetails": "full",
                             "maxSupportedTransactionVersion": 0}]}))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "error" in msg and "id" in msg:
                            return "unsupported"
                        await self.handle_helius(msg, watched)
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
                self.connected = False
                if "401" in str(exc) or "403" in str(exc):
                    return "unsupported"
                await asyncio.sleep(2)

    async def handle_helius(self, msg: dict, watched: set[str]) -> None:
        """transactionNotification -> trades for every watched wallet in it (no extra RPC call)."""
        if msg.get("method") != "transactionNotification":
            return
        res = (msg.get("params") or {}).get("result") or {}
        sig = res.get("signature", "")
        if not sig or sig in self._seen:
            return
        self._seen.add(sig)
        wrapped = res.get("transaction") or {}
        tx = {"meta": wrapped.get("meta") or {}, "transaction": wrapped.get("transaction") or {}}
        keys = (tx["transaction"].get("message") or {}).get("accountKeys") or []
        for k in keys:
            w = k.get("pubkey") if isinstance(k, dict) else k
            kol = self.registry.by_wallet.get(w) if w in watched else None
            if not kol:
                continue
            for t in parse_wallet_trades(tx, w):
                await self.record(KolTrade(w, kol, t["mint"], t["side"], t["tokens"], t["sol"],
                                           t["before"], t["after"], sig))
