"""Wires feeds -> trader -> broker and keeps everything running at once."""
from __future__ import annotations

import asyncio
import contextlib

import httpx
from rich.console import Console
from rich.live import Live

from ctmc.dashboard import render
from ctmc.data.market import Market, PriceFeed
from ctmc.feeds.twitter import WebhookSource, make_source
from ctmc.safety.checker import SafetyChecker
from ctmc.signals.hype import HypeChecker
from ctmc.strategy.engine import Trader


async def run(cfg, live: bool, console: Console) -> None:
    http = httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    market = Market(cfg.chain, http)
    if live:
        from ctmc.execution.broker import JupiterBroker
        broker = JupiterBroker(cfg["execution"], http)
        await broker.start()
    else:
        from ctmc.execution.broker import PaperBroker
        broker = PaperBroker(cfg.bankroll["starting_usd"], cfg["execution"])

    trader = Trader(cfg, broker, market, SafetyChecker(cfg["safety"], cfg["clusters"], http),
                    HypeChecker(cfg["hype"], http))
    feed = PriceFeed(market, cfg.monitor["price_refresh_seconds"])
    feed.watch.update(cfg.leaders.values())
    trader.on_watch = feed.watch.add
    trader.on_unwatch = feed.watch.discard
    source = make_source(cfg)
    mode = "live" if live else "paper"
    feeds = (f"tweets: {'webhook :' + str(cfg.twitter['webhook_port']) if isinstance(source, WebhookSource) else 'X stream'}"
             f" · prices every {cfg.monitor['price_refresh_seconds']}s · flow every {cfg.monitor['metrics_refresh_seconds']}s")

    async def metrics_loop() -> None:
        while True:
            held = list(trader.positions)
            backed = [m for m in trader.backed if m not in trader.positions][:10]
            if held or backed:
                for snap in (await market.snapshots(held + backed)).values():
                    await trader.on_metrics(snap)
            await asyncio.sleep(cfg.monitor["metrics_refresh_seconds"])

    async def guarded(name: str, coro_fn) -> None:
        while True:  # a crashed feed restarts instead of killing the bot
            try:
                await coro_fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                trader.log("HALT", name, f"{exc.__class__.__name__}: {exc} — restarting")
                await asyncio.sleep(3)

    async def ui_loop(live_ui: Live) -> None:
        while True:
            live_ui.update(render(trader, mode, feeds))
            await asyncio.sleep(0.5)

    with Live(render(trader, mode, feeds), console=console, refresh_per_second=4) as live_ui:
        tasks = [
            asyncio.create_task(guarded("tweets", lambda: source.run(trader.on_tweet))),
            asyncio.create_task(guarded("prices", lambda: feed.run(trader.on_price))),
            asyncio.create_task(guarded("flow", metrics_loop)),
            asyncio.create_task(ui_loop(live_ui)),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                await http.aclose()
