"""Performance report from the journal — metrics chosen for a FEW-BIG-WINNERS strategy.

Win rate and Sharpe mislead when most trades lose and a handful run 10-100x (freqtrade/jesse
research). So alongside the basics this reports what actually tells you if it works:
  best-trade share      how much of all profit came from the single best trade
  PF without best       profit factor if that one trade never happened
  median vs mean        the gap shows how skewed results are
  capture ratio         banked multiple / peak multiple (did the exits keep the gains?)
  hit-2x rate           share of trades that reached 2x (the ladder's first rung)
  Omega(0), CVaR 95%    upside vs downside mass; average of the worst 5% of trades
  max drawdown          marked to market from equity snapshots (not just closed trades)
  latency               tweet -> fill
Broken down per signal source (each celebrity / narrative / KOL cluster), per exit rule and per profile.
"""
from __future__ import annotations

import statistics
import time
from collections import defaultdict


def _pf(pnls: list[float]) -> float:
    wins, losses = sum(p for p in pnls if p > 0), -sum(p for p in pnls if p < 0)
    return round(wins / losses, 2) if losses else (float("inf") if wins else 0.0)


def summarize(rows: list, equity_curve: list[tuple[float, float]] | None = None) -> dict:
    """Pure: closed-position rows (sqlite Rows or dicts) -> metrics dict."""
    if not rows:
        return {"n": 0}
    pnl = [float(r["pnl_usd"] or 0) for r in rows]
    cost = [float(r["cost_usd"] or 0) for r in rows]
    ret = [p / c if c else 0.0 for p, c in zip(pnl, cost)]                # per-trade return on stake
    total = sum(pnl)
    best = max(pnl)
    without_best = list(pnl)
    without_best.remove(best)
    peaks = [float(r["peak_multiple"] or 0) for r in rows]
    exits = [float(r["exit_multiple"] or 0) for r in rows]
    capture = [e / p for e, p in zip(exits, peaks) if p >= 1.5]
    holds = [(r["closed_at"] or 0) - (r["opened_at"] or 0) for r in rows]
    lat = [float(r["entry_latency_s"]) for r in rows if r["entry_latency_s"] is not None]
    worst = sorted(ret)[: max(1, len(ret) // 20)]
    streak = best_streak = 0
    for p in pnl:
        streak = streak + 1 if p < 0 else 0
        best_streak = max(best_streak, streak)

    out = {
        "n": len(rows),
        "total_pnl": round(total, 2),
        "win_rate": round(100 * sum(1 for p in pnl if p > 0) / len(pnl), 1),
        "hit_2x_rate": round(100 * sum(1 for r in rows if r["hit_2x"]) / len(rows), 1),
        "expectancy": round(total / len(pnl), 2),
        "median_return_pct": round(100 * statistics.median(ret), 1),
        "mean_return_pct": round(100 * statistics.mean(ret), 1),
        "profit_factor": _pf(pnl),
        "pf_without_best": _pf(without_best),
        "best_trade_share_pct": round(100 * best / total, 1) if total > 0 else None,
        "omega": _pf(ret),
        "cvar95_pct": round(100 * statistics.mean(worst), 1),
        "capture_ratio": round(statistics.mean(capture), 2) if capture else None,
        "max_losing_streak": best_streak,
        "median_hold_min": round(statistics.median(holds) / 60, 1),
        "median_latency_s": round(statistics.median(lat), 2) if lat else None,
        "best_peak_x": round(max(peaks), 1),
    }
    if equity_curve and len(equity_curve) > 1:
        peak, dd = equity_curve[0][1], 0.0
        for _, e in equity_curve:
            peak = max(peak, e)
            dd = max(dd, (1 - e / peak) * 100 if peak else 0)
        out["max_drawdown_pct"] = round(dd, 1)
    return out


def breakdown(rows: list, key: str) -> dict[str, dict]:
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[r[key] or "-"].append(r)
    return {k: summarize(v) for k, v in sorted(groups.items(), key=lambda kv: -sum(float(r["pnl_usd"] or 0) for r in kv[1]))}


def text_summary(store, days: int = 30) -> str:
    rows = store.closed(time.time() - days * 86400)
    s = summarize(rows, store.equity_curve(time.time() - days * 86400))
    if not s["n"]:
        return f"no closed trades in the last {days} days"
    bts = f"{s['best_trade_share_pct']}%" if s["best_trade_share_pct"] is not None else "n/a"
    return (f"{days}d: {s['n']} trades · PnL ${s['total_pnl']:,.2f} · hit 2x {s['hit_2x_rate']}% · "
            f"PF {s['profit_factor']} (without best {s['pf_without_best']}) · best trade = {bts} of profit · "
            f"median {s['median_return_pct']}% / mean {s['mean_return_pct']}% · max DD {s.get('max_drawdown_pct', '—')}%")


def render(store, days: int = 30):
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    rows = store.closed(time.time() - days * 86400)
    s = summarize(rows, store.equity_curve(time.time() - days * 86400))
    if not s["n"]:
        return Text(f"No closed trades in the last {days} days yet — run the bot (paper is fine) to build a journal.")
    head = Table(show_header=False, box=None, pad_edge=False)
    head.add_column(style="grey62"); head.add_column(style="bold")
    labels = [
        ("Trades", s["n"]), ("Total PnL", f"${s['total_pnl']:,.2f}"), ("Reached 2x", f"{s['hit_2x_rate']}%"),
        ("Win rate", f"{s['win_rate']}%  (misleading alone)"), ("Expectancy / trade", f"${s['expectancy']:,.2f}"),
        ("Median vs mean return", f"{s['median_return_pct']}%  vs  {s['mean_return_pct']}%"),
        ("Profit factor", f"{s['profit_factor']}   without best trade: {s['pf_without_best']}"),
        ("Best trade's share of profit", f"{s['best_trade_share_pct']}%" if s["best_trade_share_pct"] is not None else "n/a"),
        ("Capture ratio (banked ÷ peak)", s["capture_ratio"] if s["capture_ratio"] is not None else "n/a"),
        ("Omega(0)", s["omega"]), ("CVaR 95% (worst 5% of trades)", f"{s['cvar95_pct']}%"),
        ("Max drawdown (marked to market)", f"{s.get('max_drawdown_pct', '—')}%"),
        ("Longest losing streak", s["max_losing_streak"]), ("Median hold", f"{s['median_hold_min']} min"),
        ("Median tweet → fill", f"{s['median_latency_s']} s" if s["median_latency_s"] is not None else "n/a"),
        ("Best peak", f"{s['best_peak_x']}x"),
    ]
    for k, v in labels:
        head.add_row(k, str(v))

    def table(title: str, key: str) -> Table:
        t = Table(title=title, header_style="bold bright_cyan", border_style="grey37", title_justify="left")
        for c in (key.replace("_", " ").title(), "Trades", "PnL", "Reached 2x", "PF", "Median ret"):
            t.add_column(c, justify="left" if c[0].isalpha() and c not in ("Trades", "PnL", "PF") else "right")
        for k, m in breakdown(rows, key).items():
            t.add_row(k, str(m["n"]), f"${m['total_pnl']:,.2f}", f"{m['hit_2x_rate']}%", str(m["profit_factor"]),
                      f"{m['median_return_pct']}%")
        return t

    return Group(Text(f"CTMC performance · last {days} days", style="bold"), head, "",
                 table("By signal source", "signal"), table("By exit rule", "exit_code"), table("By profile", "profile"))
