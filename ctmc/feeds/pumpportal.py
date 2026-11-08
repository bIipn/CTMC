"""PumpPortal real-time stream: every new pump.fun token and every graduation (migration).

wss://pumpportal.fun/api/data — `subscribeNewToken` and `subscribeMigration` are free.
(Per-account/token trade streams are metered by PumpPortal, so wallet tracking uses
our own RPC subscriptions instead — see ctmc/kol/tracker.py.)
One connection only, as PumpPortal asks.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

URL = "wss://pumpportal.fun/api/data"


@dataclass
class NewToken:
    mint: str
    name: str
    symbol: str
    creator: str
    market_cap_sol: float
    ts: float


def parse_message(msg: dict) -> tuple[str, object] | None:
    """('new', NewToken) | ('migration', mint) | None for anything else."""
    if msg.get("txType") == "create" and msg.get("mint"):
        return "new", NewToken(msg["mint"], msg.get("name", ""), msg.get("symbol", ""),
                               msg.get("traderPublicKey", ""), float(msg.get("marketCapSol") or 0), time.time())
    if msg.get("txType") == "migrate" or ("mint" in msg and "pool" in msg and msg.get("txType") in (None, "migration")):
        return "migration", msg["mint"]
    return None


class PumpPortalStream:
    def __init__(self, on_new: Callable[[NewToken], Awaitable[None]],
                 on_migration: Callable[[str], Awaitable[None]] | None = None):
        self.on_new = on_new
        self.on_migration = on_migration
        key = os.getenv("PUMPPORTAL_API_KEY", "")
        self.url = f"{URL}?api-key={key}" if key else URL

    async def run(self) -> None:
        import websockets

        backoff = 2
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20, max_size=2**21) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    backoff = 2
                    async for raw in ws:
                        try:
                            parsed = parse_message(json.loads(raw))
                        except ValueError:
                            continue
                        if not parsed:
                            continue
                        kind, val = parsed
                        if kind == "new":
                            await self.on_new(val)
                        elif self.on_migration:
                            await self.on_migration(val)
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)       # PumpPortal bans clients that reconnect in a loop
