"""Post-buy rug watch — the checks before a buy can't see what happens after it.

  liquidity pull  pooled liquidity falls more than the price move explains.
                  In a constant-product pool the USD value of liquidity moves with
                  sqrt(price), so: removed = 1 - (L_now / L_then) / sqrt(P_now / P_then).
  dev sells       the creator's token account balance drops.
  whale dump      top holders (pools/exchanges excluded) sell a big share of supply.

Wallets are watched in real time over the Solana RPC websocket (accountSubscribe),
with a polling fallback if the websocket is down. Any trigger -> urgent full exit.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from ctmc.models import PairSnapshot
from ctmc.safety.checker import people_holders

Alert = Callable[[str, str], Awaitable[None]]   # (mint, reason)


@dataclass
class Watched:
    mint: str
    account: str          # token account address
    owner: str
    role: str             # "dev" | "whale"
    supply_pct: float     # share of supply at entry
    start_amount: float | None = None
    last_amount: float | None = None

    def sold_fraction(self) -> float:
        if not self.start_amount or self.last_amount is None:
            return 0.0
        return max(0.0, 1 - self.last_amount / self.start_amount)


@dataclass
class _MintState:
    liq: deque = field(default_factory=lambda: deque(maxlen=120))   # (ts, liquidity, price)
    alerted: bool = False


def liquidity_removed(then: tuple[float, float], now: tuple[float, float]) -> float:
    """Fraction of liquidity removed beyond what the price change explains."""
    (l0, p0), (l1, p1) = then, now
    if l0 <= 0 or p0 <= 0 or p1 <= 0:
        return 0.0
    expected = l0 * math.sqrt(p1 / p0)
    return max(0.0, 1 - l1 / expected)


class RugWatch:
    def __init__(self, rules: dict, on_alert: Alert, http: httpx.AsyncClient | None = None):
        self.r = rules
        self.on_alert = on_alert
        self.http = http or httpx.AsyncClient(timeout=10)
        self.rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.ws_url = os.getenv("SOLANA_WS_URL") or self.rpc.replace("https://", "wss://").replace("http://", "ws://")
        self.watched: dict[str, Watched] = {}          # token account -> Watched
        self.mints: dict[str, _MintState] = {}
        self._subs: dict[int, str] = {}                # subscription id -> token account
        self._req: dict[int, str] = {}                 # request id -> token account (until confirmed)
        self._next_id = 1
        self._pending: asyncio.Queue[str] = asyncio.Queue()
        self.ws_connected = False

    # ── lifecycle ────────────────────────────────────────────
    async def start(self, mint: str, report: dict | None) -> None:
        """Begin watching a coin we just bought. `report` is its rugcheck report."""
        self.mints[mint] = _MintState()
        if not report:
            return
        creator = report.get("creator")
        token = report.get("token") or {}
        supply = float(token.get("supply") or 0) / 10 ** int(token.get("decimals") or 0) if token.get("supply") else 0
        if creator:
            for acct, amount in (await self._token_accounts(creator, mint)).items():
                pct = amount / supply * 100 if supply else float(report.get("creatorBalancePct") or 0)
                if pct >= self.r.get("min_dev_pct_to_watch", 0.1) or not supply:
                    self._add(Watched(mint, acct, creator, "dev", pct, amount, amount))
        for h in people_holders(report)[: self.r.get("watch_top_holders", 10)]:
            if h.get("address") and h.get("owner") != creator:
                amt = float(h.get("uiAmount") or 0) or None
                self._add(Watched(mint, h["address"], h.get("owner", ""), "whale", float(h.get("pct") or 0), amt, amt))

    def stop(self, mint: str) -> None:
        self.mints.pop(mint, None)
        for acct in [a for a, w in self.watched.items() if w.mint == mint]:
            self.watched.pop(acct, None)

    def _add(self, w: Watched) -> None:
        self.watched[w.account] = w
        self._pending.put_nowait(w.account)

    # ── liquidity (fed by the trader's 3s metrics loop) ─────
    async def on_metrics(self, s: PairSnapshot) -> None:
        st = self.mints.get(s.token)
        if not st or st.alerted:
            return
        now = time.time()
        st.liq.append((now, s.liquidity_usd, s.price_usd))
        window = self.r.get("lp_window_seconds", 60)
        then = next((x for x in st.liq if now - x[0] <= window), None)
        if not then or then is st.liq[-1]:
            return
        removed = liquidity_removed((then[1], then[2]), (s.liquidity_usd, s.price_usd))
        if removed * 100 >= self.r.get("lp_pull_pct", 25):
            await self._fire(s.token, f"liquidity pulled: {removed * 100:.0f}% removed in {now - then[0]:.0f}s")

    # ── wallet balances ──────────────────────────────────────
    async def on_balance(self, account: str, amount: float) -> None:
        w = self.watched.get(account)
        if not w:
            return
        if w.start_amount is None:
            w.start_amount = amount
        w.last_amount = amount
        await self._evaluate(w.mint)

    async def _evaluate(self, mint: str) -> None:
        st = self.mints.get(mint)
        if not st or st.alerted:
            return
        mine = [w for w in self.watched.values() if w.mint == mint]
        for w in mine:
            if w.role == "dev" and w.sold_fraction() * 100 >= self.r.get("dev_sell_pct", 10):
                await self._fire(mint, f"DEV IS SELLING: creator dumped {w.sold_fraction() * 100:.0f}% of their bag")
                return
        dumped = sum(w.supply_pct * w.sold_fraction() for w in mine if w.role == "whale")
        if dumped >= self.r.get("whale_supply_dumped_pct", 5):
            sellers = sum(1 for w in mine if w.role == "whale" and w.sold_fraction() > 0.2)
            await self._fire(mint, f"whales dumping: {sellers} top holders sold {dumped:.1f}% of supply")

    async def _fire(self, mint: str, reason: str) -> None:
        st = self.mints.get(mint)
        if st and not st.alerted:
            st.alerted = True
            await self.on_alert(mint, reason)

    # ── RPC plumbing ─────────────────────────────────────────
    async def _rpc(self, method: str, params: list):
        r = await self.http.post(self.rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        out = r.json()
        if "error" in out:
            raise RuntimeError(out["error"])
        return out["result"]

    async def _token_accounts(self, owner: str, mint: str) -> dict[str, float]:
        try:
            res = await self._rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
            return {a["pubkey"]: float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                    for a in res["value"]}
        except (httpx.HTTPError, RuntimeError, KeyError, TypeError):
            return {}

    async def run(self) -> None:
        """Websocket push for every watched account; polling while the socket is down."""
        import websockets

        poller = asyncio.create_task(self._poll_while_disconnected())
        try:
            while True:
                try:
                    async with websockets.connect(self.ws_url, ping_interval=20, max_size=2**22) as ws:
                        self.ws_connected = True
                        self._subs.clear()
                        self._req.clear()
                        for acct in list(self.watched):
                            self._pending.put_nowait(acct)
                        reader = asyncio.create_task(self._read(ws))
                        try:
                            while True:
                                acct = await self._pending.get()
                                if acct in self.watched:
                                    rid, self._next_id = self._next_id, self._next_id + 1
                                    self._req[rid] = acct
                                    await ws.send(json.dumps({
                                        "jsonrpc": "2.0", "id": rid, "method": "accountSubscribe",
                                        "params": [acct, {"encoding": "jsonParsed", "commitment": "confirmed"}]}))
                        finally:
                            reader.cancel()
                except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
                    self.ws_connected = False
                    await asyncio.sleep(2)
        finally:
            poller.cancel()

    async def _read(self, ws) -> None:
        async for raw in ws:
            await self.handle_message(json.loads(raw))

    async def handle_message(self, msg: dict) -> None:
        if "id" in msg and "result" in msg:                          # subscription confirmed
            acct = self._req.pop(msg["id"], None)
            if acct:
                self._subs[msg["result"]] = acct
        elif msg.get("method") == "accountNotification":
            p = msg["params"]
            acct = self._subs.get(p["subscription"])
            amount = _ui_amount(p["result"]["value"])
            if acct is not None and amount is not None:
                await self.on_balance(acct, amount)

    async def _poll_while_disconnected(self) -> None:
        while True:
            await asyncio.sleep(self.r.get("poll_seconds", 5))
            if self.ws_connected or not self.watched:
                continue
            for acct in list(self.watched):
                try:
                    res = await self._rpc("getTokenAccountBalance", [acct])
                    await self.on_balance(acct, float(res["value"]["uiAmount"] or 0))
                except (httpx.HTTPError, RuntimeError, KeyError, TypeError):
                    continue


def _ui_amount(value: dict | None) -> float | None:
    """Token balance from an account notification. A closed account (no data, 0 lamports) = 0."""
    if value is None or value.get("lamports") == 0:
        return 0.0
    try:
        return float(value["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
    except (KeyError, TypeError):
        return None
