"""Time machine: replay the exit rules thousands of times before trusting them with money.

Price paths (each = the coin's price as a multiple of our entry, over time since entry):
  recorded   every position the bot holds (paper or live) records its path automatically
  synthetic  made-up fat-tailed meme paths. ONLY shows how the rules behave — it is NOT
             evidence the strategy works (the mix of outcomes is an assumption).

1. Each path is run through the CURRENT exit rules (2x cost-out, halving ladder, final exit,
   profit floors, crash backstop, hard stop, no-2x time barrier) -> banked multiple of the stake.
   Flow / smart-money / rug-watch exits need live data, so they can't be replayed from price
   alone; recorded paths end where the real trade ended.
2. Monte Carlo: 10,000 runs, each drawing N whole trades with replacement (keeps each event's
   cause and effect intact — day-trader-skill / jesse method), staking risk_per_trade_pct of
   current equity per trade.
Outputs: chance of profit, final-equity spread (5% / median / 95%), drawdown distribution,
chance of a 50% drawdown, and side-by-side comparisons of rule variants.
"""
from __future__ import annotations

import copy
import math
import random
import statistics
from dataclasses import dataclass

from ctmc.models import Position
from ctmc.strategy.ladder import ladder_sells

Path = list[tuple[float, float]]          # (seconds since entry, price multiple)


STAKE = 100.0          # simulate a realistic $100 position so dollar rules (dust) behave as live


def replay(path: Path, ex: dict, fee: float = 0.01) -> float:
    """Banked multiple of the stake (1.0 = broke even) when `ex` exit rules run over `path`."""
    pos = Position("sim", "SIM", 1.0, STAKE, STAKE)
    banked = 0.0
    floors = ex.get("profit_floors", [])
    m = 1.0
    for t, m in path:
        pos.peak_price = max(pos.peak_price, m)
        for s in ladder_sells(pos, m, ex):
            q = min(s.qty, pos.qty)
            banked += q * m * (1 - fee)
            pos.qty -= q
            pos.initial_taken |= s.initial
            if s.rung:
                pos.ladder_hits.append(s.rung)
            if s.closes or pos.qty <= 1e-12:
                return banked / STAKE
        peak = pos.peak_price
        floor = max((f for trig, f in floors if peak >= trig), default=0.0)
        out = (floor and m < floor) \
            or (pos.initial_taken and (m / peak - 1) * 100 <= -ex.get("crash_trailing_stop_pct", 50)) \
            or (not pos.initial_taken and (m - 1) * 100 <= -ex.get("hard_stop_loss_pct", 30)) \
            or (ex.get("no_2x_exit_hours") and not pos.initial_taken and t > ex["no_2x_exit_hours"] * 3600)
        if out:
            return (banked + pos.qty * m * (1 - fee)) / STAKE
    return (banked + pos.qty * m * (1 - fee)) / STAKE


def synthetic_paths(n: int = 1000, seed: int = 19, step_s: int = 120, hours: int = 48) -> list[Path]:
    """Assumed outcome mix for celebrity-tweeted coins. Illustration only — NOT market data."""
    rng = random.Random(seed)
    steps = hours * 3600 // step_s
    # Pessimistic on purpose: we buy seconds AFTER the tweet, so snipers are already in.
    regimes = [("rug", 0.35), ("fade", 0.40), ("pump_dump", 0.20), ("runner", 0.045), ("moonshot", 0.005)]
    out = []
    for _ in range(n):
        r, acc = rng.random(), 0.0
        kind = next(k for k, p in regimes if (acc := acc + p) >= r)
        top = {"rug": rng.uniform(1.0, 1.6), "fade": rng.uniform(1.0, 1.4), "pump_dump": rng.uniform(1.8, 5),
               "runner": rng.uniform(5, 25), "moonshot": rng.uniform(50, 300)}[kind]
        end = {"rug": 0.03, "fade": rng.uniform(0.15, 0.6), "pump_dump": rng.uniform(0.3, 1.2),
               "runner": top * rng.uniform(0.15, 0.5), "moonshot": top * rng.uniform(0.1, 0.4)}[kind]
        peak_at = {"rug": rng.uniform(0.002, 0.02), "fade": rng.uniform(0.01, 0.1), "pump_dump": rng.uniform(0.02, 0.2),
                   "runner": rng.uniform(0.2, 0.8), "moonshot": rng.uniform(0.4, 0.95)}[kind]
        vol = {"rug": 0.04, "fade": 0.05, "pump_dump": 0.07, "runner": 0.08, "moonshot": 0.09}[kind]
        path, k_peak, z = [], max(1, int(steps * peak_at)), 0.0
        for i in range(steps):
            frac = i / k_peak if i <= k_peak else (i - k_peak) / max(1, steps - k_peak)
            base = math.exp(math.log(top) * frac) if i <= k_peak else math.exp(math.log(top) + (math.log(end) - math.log(top)) * frac)
            z = 0.95 * z + rng.gauss(0, vol)          # persistent chop (+-20% swings) that can trip stops
            path.append(((i + 1) * step_s, max(1e-4, base * math.exp(z))))
        out.append(path)
    return out


@dataclass
class MonteCarlo:
    runs: int
    trades: int
    p_profit: float
    final_p5: float
    final_median: float
    final_p95: float
    dd_median: float
    p_dd50: float
    avg_banked: float
    share_no_loss: float


def monte_carlo(banked: list[float], risk_pct: float, trades: int = 100, runs: int = 10_000, seed: int = 7) -> MonteCarlo:
    rng = random.Random(seed)
    finals, dds = [], []
    for _ in range(runs):
        eq, peak, dd = 1.0, 1.0, 0.0
        for _ in range(trades):
            stake = eq * risk_pct / 100
            eq += stake * (rng.choice(banked) - 1)
            peak = max(peak, eq)
            dd = max(dd, 1 - eq / peak)
        finals.append(eq)
        dds.append(dd)
    finals.sort()
    q = lambda p: finals[min(len(finals) - 1, int(p * len(finals)))]  # noqa: E731
    return MonteCarlo(runs, trades, sum(f > 1 for f in finals) / runs, q(0.05), q(0.5), q(0.95),
                      statistics.median(dds), sum(d >= 0.5 for d in dds) / runs,
                      statistics.mean(banked), sum(b >= 1.0 for b in banked) / len(banked))


VARIANTS = {
    "your rules": {},
    "no profit floors": {"profit_floors": []},
    "tighter backstop (-30% from peak)": {"crash_trailing_stop_pct": 30},
    "ladder 5/10/20/50x": {"ladder": [5, 10, 20, 50]},
    "no 24h time barrier": {"no_2x_exit_hours": 0},
}


def run(paths: list[Path], ex: dict, risk_pct: float, trades: int, runs: int, compare: bool) -> dict[str, MonteCarlo]:
    out = {}
    for name, override in (VARIANTS.items() if compare else [("your rules", {})]):
        rules = {**copy.deepcopy(ex), **override}
        banked = [replay(p, rules) for p in paths]
        out[name] = monte_carlo(banked, risk_pct, trades, runs)
    return out


def recorded_paths(store) -> list[Path]:
    paths = []
    for r in store.closed():
        pts = store.path(r["token"], r["opened_at"], (r["closed_at"] or r["opened_at"]) + 1)
        if len(pts) >= 3 and r["entry_price"]:
            paths.append([(ts - r["opened_at"], px / r["entry_price"]) for ts, px in pts])
    return paths
