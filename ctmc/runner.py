"""Wires feeds -> trader -> broker and keeps everything running at once."""
from __future__ import annotations

import asyncio
import contextlib

import httpx
from rich.console import Console
from rich.live import Live

from ctmc import report
from ctmc.dashboard import render
from ctmc.notify import Notifier, TelegramControl
from ctmc.protections import Protections
from ctmc.signals.postbuy import PostBuyReview
from ctmc.storage import Store
from ctmc.data.market import Market, PriceFeed
from ctmc.feeds.cex import BinanceLeaders
from ctmc.feeds.pumpportal import PumpPortalStream
from ctmc.feeds.twitter import WebhookSource, make_source, resolve_ids
from ctmc.kol.registry import Registry
from ctmc.kol.signals import SmartMoney
from ctmc.kol.tracker import KolTracker
from ctmc.safety.checker import SafetyChecker
from ctmc.safety.rugwatch import RugWatch
from ctmc.signals.hype import HypeChecker
from ctmc.signals.narrative import Narrative
from ctmc.strategy.engine import Trader


async def run(cfg, live: bool, console: Console) -> None:
    http = httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=60, max_keepalive_connections=20))
    market = Market(cfg.chain, http)
    mode = "live" if live else "paper"
    store = Store.for_mode(mode)                                   # data/paper.sqlite or data/live.sqlite
    if live:
        from ctmc.execution.broker import JupiterBroker
        broker = JupiterBroker(cfg["execution"], http, store=store)
        await broker.start()
    else:
        from ctmc.execution.broker import PaperBroker
        broker = PaperBroker(store.get("cash", cfg.bankroll["starting_usd"]), cfg["execution"])

    id_notes: list[str] = []
    unresolved = await resolve_ids(cfg["celebrities"], http, id_notes.append)   # fill + verify X user IDs
    hype = HypeChecker(cfg["hype"], http)
    trader = Trader(cfg, broker, market, SafetyChecker(cfg["safety"], cfg["clusters"], http), hype)
    trader.ready = False                                           # no buys until saved state is reconciled
    trader.store, trader.protect = store, Protections(cfg["protections"], store)
    trader.postbuy = PostBuyReview(cfg["postbuy"], store, hype, http)
    notifier = Notifier(cfg["notify"], http)
    trader.notifier = notifier
    for note in id_notes:
        trader.log("KOL", "-", note)
    if unresolved:
        trader.log("HALT", "-", f"{len(unresolved)} celebrities have no X user ID yet (set X_BEARER_TOKEN to look them up, "
                                f"or fill celebrities.yaml): {', '.join('@' + h for h in unresolved[:8])}"
                                + (" …" if len(unresolved) > 8 else ""))

    tasks_extra: list = []
    if cfg["rugwatch"].get("enabled", True):
        trader.rugwatch = RugWatch(cfg["rugwatch"], on_alert=lambda m, why: trader.on_rug_alert(m, why), http=http)
        tasks_extra.append(("rugwatch", trader.rugwatch.run))

    tracker = None
    if cfg["kol"].get("enabled", True):
        registry = Registry(cfg["kol"].get("registry", "data/kols.json")).load()
        tracker = KolTracker(registry, cfg["kol"], on_trade=lambda t: trader.on_kol_trade(t), http=http, store=store)
        trader.smart = SmartMoney(registry, tracker, cfg["kol"])
        tasks_extra.append(("kol wallets", tracker.run))
        n, watched = len(registry.tracked(cfg["kol"])), len(registry.wallets(cfg["kol"]))
        trader.log("KOL", "-", f"streaming {watched} wallets: {n} proven confirmers + fast-flippers as bait radar "
                               f"(registry: {len(registry.kols)})"
                   + ("" if watched else " — run `python -m ctmc kols import` and `kols discover` to fill it"))

    if cfg["narrative"].get("enabled", True):
        trader.narrative = Narrative(cfg["celebrities"], cfg["narrative"])
        if cfg["narrative"].get("new_token_stream", True):
            stream = PumpPortalStream(on_new=trader.on_new_token)
            tasks_extra.append(("pump.fun launches", stream.run))

    feed = PriceFeed(market, cfg.monitor["price_refresh_seconds"])
    cex = dict(cfg["leader_feed"].get("symbols") or {}) if cfg["leader_feed"].get("binance") else {}
    feed.watch.update(mint for name, mint in cfg.leaders.items() if name not in cex)   # Jupiter covers the rest
    if cex:
        async def leader_tick(name: str, px: float) -> None:
            await trader.on_price(cfg.leaders[name], px)
        tasks_extra.append(("binance leaders", BinanceLeaders(cex, leader_tick).run))
    trader.on_watch = feed.watch.add
    trader.on_unwatch = feed.watch.discard
    await restore(trader, store, broker, live)
    source = make_source(cfg)
    feeds = (f"tweets: {'webhook :' + str(cfg.twitter['webhook_port']) if isinstance(source, WebhookSource) else 'X stream'}"
             f" · prices every {cfg.monitor['price_refresh_seconds']}s · flow every {cfg.monitor['metrics_refresh_seconds']}s"
             + (f" · {len(tracker.registry.wallets(cfg['kol']))} smart wallets live" if tracker else ""))

    async def metrics_loop() -> None:
        while True:
            held = list(trader.positions)
            backed = [m for m in trader.backed if m not in trader.positions][:10]
            if held or backed:
                for snap in (await market.snapshots(held + backed)).values():
                    await trader.on_metrics(snap)
            await trader.check_radar()
            await asyncio.sleep(cfg.monitor["metrics_refresh_seconds"])

    async def equity_loop() -> None:
        while True:
            store.snapshot_equity(trader.equity(), broker.cash)
            await asyncio.sleep(cfg.monitor.get("equity_snapshot_seconds", 60))

    async def guarded(name: str, coro_fn) -> None:
        """A crashed feed restarts instead of killing the bot — with backoff, and the same
        error is logged once (not every few seconds, which would also spam your phone)."""
        delay, last = 3.0, ""
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await coro_fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                msg = f"{exc.__class__.__name__}: {exc}"
                if asyncio.get_running_loop().time() - started > 60:
                    delay, last = 3.0, ""                     # it ran fine for a while: fresh start
                if msg != last:
                    trader.log("HALT", name, f"{msg} — retrying with backoff")
                    last = msg
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def ui_loop(live_ui: Live) -> None:
        while True:
            live_ui.update(render(trader, mode, feeds))
            await asyncio.sleep(0.5)

    with Live(render(trader, mode, feeds), console=console, refresh_per_second=4) as live_ui:
        tasks = [
            asyncio.create_task(guarded("tweets", lambda: source.run(trader.on_tweet))),
            asyncio.create_task(guarded("prices", lambda: feed.run(trader.on_price))),
            asyncio.create_task(guarded("flow", metrics_loop)),
            *[asyncio.create_task(guarded(name, fn)) for name, fn in tasks_extra],
            asyncio.create_task(guarded("equity", equity_loop)),
            asyncio.create_task(guarded("alerts", notifier.run)),
            asyncio.create_task(guarded("telegram", TelegramControl(
                trader, trader.protect, lambda: report.text_summary(store), http).run)),
            asyncio.create_task(ui_loop(live_ui)),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                await http.aclose()


async def restore(trader: Trader, store: Store, broker, live: bool) -> None:
    """Bring back open positions after a restart. Live: the chain wins over the database."""
    for p in store.open_positions():
        if live:
            try:
                held = await broker.token_balance(p.token)
            except Exception as exc:                               # can't verify -> keep, but say so
                trader.log("HALT", p.symbol, f"could not verify wallet balance on restart ({exc}) — keeping it")
                held = p.qty
            if held <= 0:
                store.close_position(p, "reconcile: no tokens left in the wallet (sold outside the bot?)")
                trader.log("HALT", p.symbol, "restart: position gone from the wallet — marked closed")
                continue
            if abs(held - p.qty) > 0.02 * max(p.qty, 1e-12):
                trader.log("HALT", p.symbol, f"restart: wallet holds {held:,.4f}, records said {p.qty:,.4f} — using the wallet")
                p.qty = held
                store.save_position(p)
        trader.positions[p.token] = p
        trader.on_watch(p.token)
        if trader.rugwatch:
            await trader.rugwatch.start(p.token, None)
    trader.seen = {tuple(x) for x in store.get("seen", [])}
    if trader.positions:
        trader.log("KOL", "-", f"restored {len(trader.positions)} open position(s) from {store.path}")
    trader.ready = True
