"""CTMC command line.

  python -m ctmc check <mint>                 every check on one coin, YES / NO
  python -m ctmc test-tweet "<text>" --as elonmusk
                                              dry-run what the bot would do with a tweet
  python -m ctmc run                          start the bot (paper by default)
  python -m ctmc run --live --i-understand-the-risk

  python -m ctmc kols import [--limit 60]     pull KOL wallets + stats from kolexplorer.com
  python -m ctmc kols list                    who counts as smart money, and why
  python -m ctmc kols score <wallet> [--name] score any wallet from its own on-chain swaps
  python -m ctmc kols add <name> <wallet> [--x handle] [--celebrity]

  python -m ctmc report [--days 30] [--live]  performance: per celebrity, per exit rule, honest metrics
  python -m ctmc simulate [--compare] [--synthetic] [--trades 100] [--runs 10000]
                                              time machine: replay the exit rules thousands of times
  python -m ctmc pause | resume               kill switch (same as Telegram /pause, /resume)
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
from ctmc.kol.registry import Kol, Registry
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
        profile, prof = trader.profile_for(snap)
        u = trader.passes_universe(snap, prof.get("universe"))
        overrides = {**(prof.get("safety") or {}),
                     **({"lp_exempt_market_types": prof["lp_exempt_market_types"]} if prof.get("lp_exempt_market_types") else {})}
        smart_rows = []
        reg = Registry(cfg["kol"].get("registry", "data/kols.json")).load()
        if reg.kols:
            from ctmc.kol.signals import SmartMoney
            from ctmc.kol.tracker import KolTracker

            async def _noop(_t):
                return None
            sm = SmartMoney(reg, KolTracker(reg, cfg["kol"], _noop, http), cfg["kol"])
            conf = await sm.confluence(mint)
            smart_rows = conf.names
        sv, hv = await asyncio.gather(safety.check(mint, overrides), HypeChecker(cfg["hype"], http).check(snap.symbol, mint))

    yes = u.ok and sv.ok
    con.print(f"\n[bold {'green' if yes else 'red'}]{'YES — passes the hard filters' if yes else 'NO — the bot would not buy this'}[/]")
    con.rule(f"[bold bright_cyan]{snap.symbol}[/]  ·  {mint[:6]}…{mint[-4:]}")
    con.print(f"price ${snap.price_usd:.6g}   mcap ${snap.market_cap:,.0f}   age {snap.age_hours:.1f}h   "
              f"rules: [bold]{profile}[/]")
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
    con.print(f"\n[bold bright_cyan]◆ SMART MONEY[/]  [bold]{len(smart_rows)} proven wallets hold it[/]"
              + ("" if reg.kols else "  (registry empty — run `python -m ctmc kols import`)"))
    for r in smart_rows:
        con.print(f"     • {r}", style="grey70")


def cmd_simulate(cfg, a) -> None:
    from rich.table import Table

    from ctmc.simulate import recorded_paths, run, synthetic_paths
    from ctmc.storage import Store
    paths, source = recorded_paths(Store.for_mode("live" if a.live else "paper")), "recorded"
    if a.synthetic or len(paths) < 20:
        if not a.synthetic:
            con.print(f"[yellow]Only {len(paths)} recorded price paths so far (need 20+). Run the bot in paper mode "
                      f"to build real ones, or pass --synthetic to see how the rules behave on made-up paths.[/]")
            return
        paths, source = synthetic_paths(1000), "SYNTHETIC"
        con.print("[bold yellow]⚠ SYNTHETIC PATHS — an assumed, deliberately pessimistic outcome mix. These numbers "
                  "show how your rules interact, NOT what you will earn.[/]")
    res = run(paths, dict(cfg["exits"]), cfg["bankroll"]["risk_per_trade_pct"], a.trades, a.runs, a.compare)
    t = Table(title=f"Time machine · {len(paths)} {source} paths · {a.runs:,} runs × {a.trades} trades · "
                    f"{cfg['bankroll']['risk_per_trade_pct']}% of equity per trade",
              header_style="bold bright_cyan", border_style="grey37")
    for c in ("Rules", "Chance of profit", "Final equity 5% / median / 95%", "Median drawdown", "Chance of -50% drawdown",
              "Avg banked per trade"):
        t.add_column(c, justify="left" if c == "Rules" else "right")
    for name, mc in res.items():
        t.add_row(name, f"{mc.p_profit:.0%}", f"{mc.final_p5:.2f}x / {mc.final_median:.2f}x / {mc.final_p95:.2f}x",
                  f"{mc.dd_median:.0%}", f"{mc.p_dd50:.0%}", f"{mc.avg_banked:.2f}x")
    con.print(t)


def _kol_table(reg: Registry, rules: dict, limit: int = 40):
    from rich.table import Table
    t = Table(header_style="bold bright_cyan", border_style="grey37", title="Smart money registry")
    for col in ("KOL", "Tier", "Weight", "Win rate", "30d PnL", "Median hold", "Entry mcap", "Trades/7d", "X"):
        t.add_column(col, justify="left" if col in ("KOL", "Tier", "X") else "right")
    style = {"A": "bold green", "B": "green", "C": "yellow", "CELEB": "bold magenta", "unfollowable": "grey50", "no edge": "red"}
    for k in sorted(reg.kols.values(), key=lambda k: (-k.weight(rules), -k.stats.pnl_30d_usd))[:limit]:
        s, tier = k.stats, k.tier(rules)
        hold = f"{s.median_hold_s / 3600:.1f} h" if s.median_hold_s >= 3600 else f"{s.median_hold_s / 60:.0f} min" if s.median_hold_s >= 60 else f"{s.median_hold_s:.0f} s"
        t.add_row(k.name, f"[{style.get(tier, '')}]{tier}[/]", f"{k.weight(rules):.2f}", f"{s.win_rate:.1f}%",
                  f"${s.pnl_30d_usd:,.0f}", hold, f"${s.median_entry_mcap_usd:,.0f}" if s.median_entry_mcap_usd else "—",
                  f"{s.trades_7d:,}", f"@{k.x_handle}" if k.x_handle else "")
    return t


async def cmd_kols(cfg, a) -> None:
    rules = cfg["kol"]
    reg = Registry(rules.get("registry", "data/kols.json")).load()
    if a.kcmd == "import":
        from ctmc.kol.importer import import_kolexplorer
        con.print(f"Importing up to {a.limit} KOLs from kolexplorer.com (public pages, one every ~1.5 s)…")
        n = await import_kolexplorer(reg, limit=a.limit, log=con.print)
        reg.save()
        con.print(f"[green]✔ {n} KOLs imported → {reg.path}[/]")
        con.print(_kol_table(reg, rules))
    elif a.kcmd == "add":
        reg.add(Kol(a.name, [a.wallet], x_handle=(a.x or "").lstrip("@"), role="celebrity" if a.celebrity else "kol"))
        reg.save()
        con.print(f"[green]✔ added {a.name}[/] ({'celebrity' if a.celebrity else 'KOL'}) → {reg.path}")
    elif a.kcmd == "score":
        from ctmc.kol.scorer import fetch_swaps, score_history
        async with httpx.AsyncClient(timeout=30) as http:
            txs = await fetch_swaps(a.wallet, http=http)
            sol = (await Market(cfg.chain, http).prices([cfg.leaders["SOL"]])).get(cfg.leaders["SOL"], 0.0)
        rep = score_history(txs, a.wallet, sol)
        s = rep.stats
        con.rule(f"[bold bright_cyan]{a.name or a.wallet[:8]}[/] · {len(txs)} swaps")
        con.print(f"closed {s.closed_trades} · win rate [bold]{s.win_rate:.1f}%[/] · realized ${s.pnl_30d_usd:,.0f} · "
                  f"profit factor {s.profit_factor:g} · median hold {s.median_hold_s / 60:.1f} min · "
                  f"flips <60s {s.flip_rate:.0%} · rugs {s.rug_rate:.0%} · open {rep.open_positions}")
        for n in rep.notes:
            con.print(f"   • {n}", style="grey62")
        if a.name:
            old = reg.kols.get(a.name.lower())
            if old and old.stats.median_entry_mcap_usd:
                s.median_entry_mcap_usd = old.stats.median_entry_mcap_usd
            reg.add(Kol(a.name, [a.wallet], x_handle=old.x_handle if old else "", role=old.role if old else "kol", stats=s))
            reg.save()
            k = reg.kols[a.name.lower()]
            con.print(f"saved → tier [bold]{k.tier(rules)}[/], weight {k.weight(rules):.2f}")
    else:
        con.print(_kol_table(reg, rules))
        tracked = reg.tracked(rules)
        con.print(f"[grey62]{len(tracked)} of {len(reg.kols)} count as smart money (proven edge + hold long enough to follow).[/]")


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
    rp = sub.add_parser("report"); rp.add_argument("--days", type=int, default=30); rp.add_argument("--live", action="store_true")
    sm = sub.add_parser("simulate"); sm.add_argument("--compare", action="store_true")
    sm.add_argument("--synthetic", action="store_true"); sm.add_argument("--trades", type=int, default=100)
    sm.add_argument("--runs", type=int, default=10_000); sm.add_argument("--live", action="store_true")
    for name in ("pause", "resume"):
        sub.add_parser(name).add_argument("--live", action="store_true")
    k = sub.add_parser("kols", help="manage the smart-money registry")
    ksub = k.add_subparsers(dest="kcmd")
    ki = ksub.add_parser("import"); ki.add_argument("--limit", type=int, default=60)
    ksub.add_parser("list")
    ks = ksub.add_parser("score"); ks.add_argument("wallet"); ks.add_argument("--name")
    ka = ksub.add_parser("add"); ka.add_argument("name"); ka.add_argument("wallet")
    ka.add_argument("--x"); ka.add_argument("--celebrity", action="store_true")
    a = ap.parse_args(argv)
    _load_dotenv()
    cfg = config.load(a.config)

    if a.cmd == "check":
        asyncio.run(cmd_check(cfg, a.mint))
    elif a.cmd == "test-tweet":
        asyncio.run(cmd_test_tweet(cfg, a.text, a.handle))
    elif a.cmd == "kols":
        asyncio.run(cmd_kols(cfg, a))
    elif a.cmd == "report":
        from ctmc import report
        from ctmc.storage import Store
        con.print(report.render(Store.for_mode("live" if a.live else "paper"), a.days))
    elif a.cmd == "simulate":
        cmd_simulate(cfg, a)
    elif a.cmd in ("pause", "resume"):
        from ctmc.protections import Protections
        from ctmc.storage import Store
        pr = Protections(cfg["protections"], Store.for_mode("live" if a.live else "paper"))
        pr.halt("manual (command line)") if a.cmd == "pause" else pr.resume()
        con.print("[bold red]⛔ paused — no new buys (exits keep running)[/]" if a.cmd == "pause"
                  else "[bold green]🟢 resumed — buys allowed[/]")
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
