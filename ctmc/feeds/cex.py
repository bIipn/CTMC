"""Real-time leader prices from Binance websockets (no key needed).

`bookTicker` is Binance's fastest top-of-book stream: every best bid/ask change is pushed,
so the leader guard reacts within milliseconds instead of waiting for the next poll — and it
frees the Jupiter key's 60 calls/minute for the coins we actually hold.
If a stream goes quiet for 15 s it reconnects; while it's down the leader guard goes stale
and blocks new buys (fail closed).
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

URLS = {"spot": "wss://stream.binance.com:9443/stream?streams=",
        "futures": "wss://fstream.binance.com/stream?streams="}


def parse_book_ticker(msg: dict) -> tuple[str, float] | None:
    """Combined-stream message -> (SYMBOL, mid price)."""
    d = msg.get("data", msg)
    try:
        bid, ask = float(d["b"]), float(d["a"])
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    return d.get("s", "").upper(), (bid + ask) / 2


class BinanceLeaders:
    def __init__(self, symbols: dict[str, str], on_price: Callable[[str, float], Awaitable[None]]):
        """symbols: leader name -> "spot:SOLUSDT" | "futures:FARTCOINUSDT"."""
        self.on_price = on_price
        self.by_market: dict[str, dict[str, str]] = {}
        for name, spec in symbols.items():
            market, _, sym = spec.partition(":") if ":" in spec else ("spot", "", spec)
            self.by_market.setdefault(market, {})[sym.upper()] = name

    async def run(self) -> None:
        await asyncio.gather(*(self._run(m, syms) for m, syms in self.by_market.items() if m in URLS))

    async def _run(self, market: str, syms: dict[str, str]) -> None:
        import websockets

        url = URLS[market] + "/".join(f"{s.lower()}@bookTicker" for s in syms)
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, max_size=2**20) as ws:
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)     # data-idle timeout
                        parsed = parse_book_ticker(json.loads(raw))
                        if parsed and parsed[0] in syms:
                            await self.on_price(syms[parsed[0]], parsed[1])
            except (OSError, asyncio.TimeoutError, ValueError, websockets.WebSocketException):
                await asyncio.sleep(2)
