"""Profit-taking ladder.

  2x                 sell exactly the cost basis -> the rest is free
  10x, 30x, 50x, 100x  sell half of what is left at each
  past 100x          halve again at every further 2x (200x, 400x, ...)
  final exit         sell everything left at `final_exit_multiple` (default 1000x). Halving at
                     every doubling keeps the bag's dollar value flat, so it needs a last rung.
  dust               close once the bag is worth less than `dust_usd`
"""
from __future__ import annotations

from dataclasses import dataclass

from ctmc.models import Position


@dataclass
class Sell:
    qty: float
    reason: str
    closes: bool = False
    rung: float | None = None      # ladder multiple this sell belongs to
    initial: bool = False          # the 2x cost-basis sell


def next_rungs(rules: dict, hits: list[float]) -> list[float]:
    ladder = list(rules.get("ladder", [10, 30, 50, 100]))
    step = rules.get("after_ladder_every_x", 2.0)
    top = max(ladder + hits) if (ladder or hits) else 1
    # extend the ladder past its last rung by repeated doubling
    rungs, x = list(ladder), (ladder[-1] if ladder else 1)
    final = rules.get("final_exit_multiple") or float("inf")
    while x <= top * step and x * step < final:
        x *= step
        rungs.append(x)
    return [r for r in rungs if r not in hits]


def ladder_sells(pos: Position, price: float, rules: dict) -> list[Sell]:
    """All sells due at this price, in order. Mutates nothing."""
    out: list[Sell] = []
    m = pos.multiple(price)
    qty_left = pos.qty

    if not pos.initial_taken and m >= rules.get("take_initial_at_multiple", 2.0):
        q = min(qty_left, pos.cost_usd / price)
        out.append(Sell(q, f"{m:.1f}x — initial ${pos.cost_usd:,.0f} pulled out, rest rides free", initial=True))
        qty_left -= q

    frac = rules.get("ladder_sell_fraction", 0.5)
    for rung in next_rungs(rules, pos.ladder_hits):
        if m < rung or qty_left <= 0:
            break
        q = qty_left * frac
        out.append(Sell(q, f"hit {rung:g}x — sold half of the bag", rung=rung))
        qty_left -= q

    final = rules.get("final_exit_multiple")
    if qty_left > 0 and final and m >= final:
        out.append(Sell(qty_left, f"hit {final:g}x — final exit, sold the rest", closes=True))
    elif out and qty_left * price < rules.get("dust_usd", 5):
        out.append(Sell(qty_left, "bag is dust — closed", closes=True))
    return out
