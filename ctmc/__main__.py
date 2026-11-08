"""CTMC command line.

  python -m ctmc check <mint>      safety + X-hype report for one coin
  python -m ctmc scan              screen trending coins through every filter
  python -m ctmc run               start the bot (paper by default)
  python -m ctmc run --live --i-understand-the-risk
"""
from __future__ import annotations

import argparse
import sys
import time

from rich.console import Console
from rich.live import Live
from rich.table import Table

from ctmc import config
from ctmc.dashboard import render
from ctmc.data.dexscreener import DexScreener
from ctmc.safety.checker import SafetyChecker
from ctmc.signals.hype import HypeChecker
from ctmc.strategy.engine import Engine

con = Console()


def _load_dotenv(path: str = ".env") -> None:
    import os
    try:
        for line in open(path, encoding="utf-8"):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#") and v:
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


def _tick_badge(ok: bool) -> str:
    return "[bold green]✔ PASS[/]" if ok else "[bold red]✘ FAIL[/]"


def cmd_check(cfg, mint: str) -> None:
    dex = DexScreener(cfg.chain)
    s = dex.best_pair(mint)
    if not s:
        con.print("[red]No pair found on this chain.[/]")
        return
    eng = Engine(cfg, broker=type("B", (), {"cash": 0})(), safety=None, hype=None)
    u = eng.passes_universe(s)
    sv = SafetyChecker(cfg["safety"]).check(mint, cfg.chain)
    hv = HypeChecker(cfg["hype"]).check(s.symbol, mint)

    con.rule(f"[bold bright_cyan]{s.symbol}[/]  ·  {mint[:6]}…{mint[-4:]}")
    con.print(f"price ${s.price_usd:.6g}   liq ${s.liquidity_usd:,.0f}   vol24h ${s.volume_24h:,.0f}   "
              f"age {s.age_hours:.0f}h   5m buys/sells {s.buys_m5}/{s.sells_m5}")
    for name, v in (("Volume & liquidity", u), ("Rug / bundle safety", sv), ("X hype (Grok)", hv)):
        con.print(f"\n{_tick_badge(v.ok)}  [bold]{name}[/]")
        for r in v.reasons:
            con.print(f"     • {r}", style="grey70")
    verdict = u and sv and hv
    con.print(f"\n[bold]{'YES — tradable' if verdict else 'NO — avoid'}[/]", style="green" if verdict else "red")


def cmd_scan(cfg) -> None:
    dex = DexScreener(cfg.chain)
    safety = SafetyChecker(cfg["safety"])
    eng = Engine(cfg, broker=type("B", (), {"cash": 0})(), safety=None, hype=None)
    t = Table(title="CTMC scan", header_style="bold bright_cyan", border_style="grey37")
    for c in ("Coin", "Vol 24h", "Liq", "Universe", "Safety", "Why not"):
        t.add_column(c)
    for mint in dex.trending_tokens()[: cfg.universe["max_pairs_tracked"]]:
        s = dex.best_pair(mint)
        if not s:
            continue
        u = eng.passes_universe(s)
        sv = safety.check(mint, cfg.chain) if u else None
        why = (u.reasons or (sv.reasons if sv else []))[:2]
        t.add_row(s.symbol, f"${s.volume_24h/1e6:.1f}M", f"${s.liquidity_usd/1e3:.0f}k",
                  _tick_badge(u.ok), _tick_badge(sv.ok) if sv else "—", "; ".join(why))
    con.print(t)


def cmd_run(cfg, live: bool) -> None:
    from ctmc.execution.broker import JupiterBroker, PaperBroker

    broker = JupiterBroker() if live else PaperBroker(cfg.bankroll["starting_usd"])
    eng = Engine(cfg, broker=broker, safety=SafetyChecker(cfg["safety"]), hype=HypeChecker(cfg["hype"]))
    dex = DexScreener(cfg.chain)
    watch: list[str] = []
    last_refresh = 0.0
    mode = "live" if live else "paper"

    with Live(render(eng, [], mode), console=con, refresh_per_second=2, screen=False) as ui:
        while True:
            try:
                if time.time() - last_refresh > 600:      # refresh candidate list every 10 min
                    watch = dex.trending_tokens()[: cfg.universe["max_pairs_tracked"]]
                    last_refresh = time.time()
                leaders = {}
                for name, mint in cfg.leaders.items():
                    p = dex.best_pair(mint)
                    if p:
                        leaders[name] = p.price_usd
                tokens = list(dict.fromkeys(watch + list(eng.positions)))
                snaps = [s for m in tokens if (s := dex.best_pair(m))]
                eng.tick(leaders, snaps)
                ui.update(render(eng, snaps, mode))
            except KeyboardInterrupt:
                break
            except Exception as exc:  # keep running through transient API errors
                eng.log("HALT", "-", f"tick error: {exc.__class__.__name__}: {exc}")
            time.sleep(cfg.poll_seconds)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ctmc", description="Meme-coin flow trader")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("mint")
    sub.add_parser("scan")
    r = sub.add_parser("run")
    r.add_argument("--live", action="store_true")
    r.add_argument("--i-understand-the-risk", dest="ack", action="store_true")
    a = ap.parse_args(argv)
    _load_dotenv()
    cfg = config.load(a.config)

    if a.cmd == "check":
        cmd_check(cfg, a.mint)
    elif a.cmd == "scan":
        cmd_scan(cfg)
    else:
        if a.live and not a.ack:
            sys.exit("Live trading spends real money. Re-run with --i-understand-the-risk.")
        cmd_run(cfg, a.live)


if __name__ == "__main__":
    main()
