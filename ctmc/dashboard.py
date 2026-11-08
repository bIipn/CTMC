"""Clean terminal dashboard (rich)."""
from __future__ import annotations

from datetime import datetime

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ctmc.models import PairSnapshot

ACCENT = "bright_cyan"
KIND_STYLE = {"BUY": "bold green", "SELL": "bold magenta", "SKIP": "grey50", "HALT": "bold red"}


def _money(v: float) -> Text:
    return Text(f"{'+' if v >= 0 else '-'}${abs(v):,.2f}", style="green" if v >= 0 else "red")


def render(engine, snaps: list[PairSnapshot], mode: str) -> Group:
    prices = {s.token: s.price_usd for s in snaps}
    eq = engine.equity(prices)
    pnl = eq - engine.day_start_equity
    lead, chg = engine.leaders.worst()

    head = Text.assemble(
        ("  CTMC ", f"bold black on {ACCENT}"), "  ",
        (mode.upper(), "bold yellow" if mode == "paper" else "bold red"), "   equity ",
        (f"${eq:,.2f}", "bold white"), "   day ", _money(pnl), "   cash ",
        (f"${engine.broker.cash:,.2f}", "white"), "   leaders ",
        (f"{lead} {chg:+.2f}%", "red" if chg < -1 else "green"),
        ("   ■ HALTED" if engine.halted else "", "bold red"),
    )

    pos = Table(expand=True, box=None, header_style=f"bold {ACCENT}", pad_edge=False)
    for col in ("Coin", "Entry", "Now", "x", "Value", "Stage"):
        pos.add_column(col, justify="right" if col not in ("Coin", "Stage") else "left")
    for t, p in engine.positions.items():
        px = prices.get(t, p.entry_price)
        m = p.multiple(px)
        pos.add_row(p.symbol, f"{p.entry_price:.6g}", f"{px:.6g}",
                    Text(f"{m:.2f}x", style="green" if m >= 1 else "red"),
                    f"${p.value(px):,.2f}", "runner (free)" if p.initial_taken else "core")
    if not engine.positions:
        pos.add_row(Text("waiting for a flush to exhaust…", style="grey50"), "", "", "", "", "")

    log = Table(expand=True, box=None, show_header=False, pad_edge=False)
    log.add_column(width=8); log.add_column(width=5); log.add_column(width=10); log.add_column(); log.add_column(justify="right")
    for e in engine.events[-12:][::-1]:
        log.add_row(datetime.fromtimestamp(e.ts).strftime("%H:%M:%S"), Text(e.kind, KIND_STYLE.get(e.kind, "")),
                    e.symbol, Text(e.detail, "grey70"), _money(e.pnl_usd) if e.pnl_usd else "")

    return Group(
        head, "",
        Panel(pos, title="[b]positions[/b]", border_style=ACCENT, title_align="left"),
        Panel(log, title="[b]activity[/b]", border_style="grey37", title_align="left"),
    )
