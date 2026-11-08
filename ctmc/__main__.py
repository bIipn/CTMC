"""CTMC command line.

  python -m ctmc check <mint>                 every check on one coin, YES / NO
  python -m ctmc test-tweet "<text>" --as elonmusk
                                              dry-run what the bot would do with a tweet
  python -m ctmc run                          start the bot (paper by default)
  python -m ctmc run --live --i-understand-the-risk
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import httpx
from rich.console import Console

from ctmc import config
from ctmc.data.market import Market
from ctmc.execution.broker import PaperBroker, max_size_for_impact
from ctmc.feeds.twitter import Tweet, extract_coins
from ctmc.safety.checker import SafetyChecker
from ctmc.signals.hype import HypeChecker
from ctmc.strategy.engine import Trader

con = Console()


def _load_dotenv(path: str = ".env") -> None:
    try:
        for line in open(path, encoding="utf-8"):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#") and v:
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


def _badge(ok: bool) -> str:
    return "[bold green]✔ PASS[/]" if ok else "[bold red]✘ FAIL[/]"


async def cmd_check(cfg, mint: str) -> None:
    async with httpx.AsyncClient(timeout=15) as http:
        market = Market(cfg.chain, http)
        snap = await market.snapshot(mint)
        if not snap:
            con.print("[bold red]NO[/] — no tradable pool on any Solana DEX.")
            return
        safety = SafetyChecker(cfg["safety"], cfg["clusters"], http)
        trader = Trader(cfg, PaperBroker(0, cfg["execution"]), market, safety)
        u = trader.passes_universe(snap)
        sv, hv = await asyncio.gather(safety.check(mint), HypeChecker(cfg["hype"], http).check(snap.symbol, mint))

    yes = u.ok and sv.ok
    con.print(f"\n[bold {'green' if yes else 'red'}]{'YES — passes the hard filters' if yes else 'NO — the bot would not buy this'}[/]")
    con.rule(f"[bold bright_cyan]{snap.symbol}[/]  ·  {mint[:6]}…{mint[-4:]}")
    con.print(f"price ${snap.price_usd:.6g}   mcap ${snap.market_cap:,.0f}   age {snap.age_hours:.1f}h")
    con.print(f"liquidity ${snap.liquidity_usd:,.0f} across {len(snap.pools)} pools: "
              + ", ".join(f"{d} ${v:,.0f}" for d, v in sorted(snap.pools.items(), key=lambda kv: -kv[1])))
    con.print(f"volume 5m ${snap.volume_m5:,.0f} · 1h ${snap.volume_h1:,.0f} · 24h ${snap.volume_24h:,.0f}   "
              f"5m buys/sells {snap.buys_m5}/{snap.sells_m5}")
    con.print(f"max buy within {cfg.execution['max_price_impact_pct']}% price impact: "
              f"[bold]${max_size_for_impact(snap.liquidity_usd, cfg.execution['max_price_impact_pct']):,.0f}[/]")
    rows = (("Volume & liquidity", u), ("Rug, bundles & bubble map", sv),
            ("X hype via Grok (info only)", hv))
    for name, v in rows:
        con.print(f"\n{_badge(v.ok)}  [bold]{name}[/]")
        for r in v.reasons:
            con.print(f"     • {r}", style="grey70")


async def cmd_test_tweet(cfg, text: str, handle: str) -> None:
    ids = {c["handle"].lower(): c["id"] for c in cfg["celebrities"]}
    author = ids.get(handle.lower().lstrip("@"), "0")
    coins = extract_coins(text)
    con.print(f"contract addresses found: {coins.mints or 'none'}   tickers: {coins.tickers or 'none'}")
    async with httpx.AsyncClient(timeout=15) as http:
        market = Market(cfg.chain, http)
        t = Trader(cfg, PaperBroker(cfg.bankroll["starting_usd"], cfg["execution"]), market,
                   SafetyChecker(cfg["safety"], cfg["clusters"], http), HypeChecker(cfg["hype"], http))
        await t.on_tweet(Tweet(id="test", author_id=author, text=text, created_at=time.time()))
    if not t.events:
        con.print("[bold red]NO[/] — ignored (not a watched celebrity, or no coin in the tweet).")
    for e in t.events:
        con.print(f"[bold]{e.kind:<6}[/] {e.symbol:<12} {e.detail}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ctmc", description="Celebrity-signal meme-coin trader")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("mint")
    tt = sub.add_parser("test-tweet"); tt.add_argument("text"); tt.add_argument("--as", dest="handle", required=True)
    r = sub.add_parser("run")
    r.add_argument("--live", action="store_true")
    r.add_argument("--i-understand-the-risk", dest="ack", action="store_true")
    a = ap.parse_args(argv)
    _load_dotenv()
    cfg = config.load(a.config)

    if a.cmd == "check":
        asyncio.run(cmd_check(cfg, a.mint))
    elif a.cmd == "test-tweet":
        asyncio.run(cmd_test_tweet(cfg, a.text, a.handle))
    else:
        if a.live and not a.ack:
            sys.exit("Live trading spends real money. Re-run with --i-understand-the-risk.")
        from ctmc.runner import run
        try:
            asyncio.run(run(cfg, a.live, con))
        except KeyboardInterrupt:
            con.print("\n[grey50]stopped.[/]")


if __name__ == "__main__":
    main()
