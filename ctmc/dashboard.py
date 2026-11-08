"""Clean terminal dashboard (rich)."""
from __future__ import annotations

from datetime import datetime

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ACCENT = "bright_cyan"
KIND_STYLE = {"BUY": "bold green", "SELL": "bold magenta", "SKIP": "grey50", "HALT": "bold red",
              "TWEET": "bold bright_cyan", "KOL": "bold yellow", "RADAR": "cyan"}


def _money(v: float) -> Text:
    return Text(f"{'+' if v >= 0 else '-'}${abs(v):,.2f}", style="green" if v >= 0 else "red")


def _stage(p) -> str:
    if p.ladder_hits:
        return f"ladder {max(p.ladder_hits):g}x ✓"
    return "runner (free)" if p.initial_taken else "core → 2x"


def render(t, mode: str, feeds: str = "") -> Group:
    eq = t.equity()
    lead, chg = t.leaders.worst()
    head = Text.assemble(
        ("  CTMC ", f"bold black on {ACCENT}"), "  ",
        (mode.upper(), "bold yellow" if mode == "paper" else "bold red"), "   equity ",
        (f"${eq:,.2f}", "bold white"), "   day ", _money(eq - t.day_start_equity), "   cash ",
        (f"${t.broker.cash:,.2f}", "white"), "   leaders ",
        (f"{lead} {chg:+.2f}%", "red" if chg < -1 else "green"),
        ("   ■ HALTED" if t.halted else "", "bold red"),
        ("   ■ PAUSED" if t.protect and t.protect.halted() else "", "bold red"),
    )
    radar = len(t.narrative.radar) if t.narrative else 0
    sub = Text(f"  watching {len(t.celebs.ids)} celebrities · {feeds}" + (f" · {radar} on narrative radar" if radar else ""),
               style="grey50")

    pos = Table(expand=True, box=None, header_style=f"bold {ACCENT}", pad_edge=False)
    for col in ("Coin", "Entry", "Now", "x", "Bag", "Banked", "Stage", "Pools", "Why"):
        pos.add_column(col, justify="left" if col in ("Coin", "Stage", "Why", "Pools") else "right")
    for m, p in t.positions.items():
        px = t.last_px.get(m, p.entry_price)
        x = p.multiple(px)
        snap = t.last_snap.get(m)
        pools = ", ".join(sorted(snap.pools, key=snap.pools.get, reverse=True)[:3]) if snap else ""
        pos.add_row(p.symbol, f"{p.entry_price:.6g}", f"{px:.6g}",
                    Text(f"{x:.2f}x", style="green" if x >= 1 else "red"),
                    f"${p.value(px):,.2f}", f"${p.realized_usd:,.2f}", _stage(p), pools,
                    Text(p.source, "grey62"))
    if not t.positions:
        pos.add_row(Text("waiting for a celebrity to tweet a coin…", style="grey50"), *[""] * 8)

    log = Table(expand=True, box=None, show_header=False, pad_edge=False)
    log.add_column(width=8); log.add_column(width=5); log.add_column(width=14)
    log.add_column(); log.add_column(justify="right")
    for e in t.events[-14:][::-1]:
        log.add_row(datetime.fromtimestamp(e.ts).strftime("%H:%M:%S"), Text(e.kind, KIND_STYLE.get(e.kind, "")),
                    e.symbol, Text(e.detail, "grey70"), _money(e.pnl_usd) if e.pnl_usd else "")

    return Group(
        head, sub, "",
        Panel(pos, title="[b]positions[/b]", border_style=ACCENT, title_align="left"),
        Panel(log, title="[b]activity[/b]", border_style="grey37", title_align="left"),
    )
