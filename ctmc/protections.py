"""Protections — the bot's brakes. Checked before every buy (in-memory + one SQLite read).

  kill switch      a HALT file exists (data/HALT; Telegram /pause creates it) -> no buys
  anomaly guard    N ANOMALY exits (rug alerts, leader crashes, stops hit within 2 min of
                   buying) inside a lookback window -> pause all buys      (freqtrade StoplossGuard)
                   Ordinary stop-outs don't count: in a strategy where most trades lose and a
                   few run 10-100x, 7+ losses in a row is normal (99% likely in 100 trades at a
                   75% loss rate), and braking on them shrinks you right before the big winner.
  drawdown guard   equity falls X% from its peak inside a rolling window -> pause
                   (catches two -14% days that a 15% daily limit never sees)  (freqtrade MaxDrawdown)
  coin cooldown    no re-buying a coin for a while after selling it (a retweet of a coin
                   we just stopped out of can't pull us back in)          (freqtrade CooldownPeriod)
  daily caps       max buys per day, and per signal source per day          (Vibe-Trading mandate)
  source scorecard each celebrity / signal is scored on its own record: share of its trades
                   that reached 2x. Weak record -> half size; dead record -> benched
                                                                        (freqtrade LowProfitPairs, DSA, TSP)
  losing streak    optional (off by default, see above) -> half size until a winner
Locks live in SQLite, so they survive restarts. Scales can only SHRINK a bet.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ctmc.storage import ANOMALY_EXITS, BAD_EXITS, QUICK_STOP_SECONDS, Store


@dataclass
class Gate:
    ok: bool
    scale: float = 1.0
    reason: str = ""


def _utc_midnight(now: float) -> float:
    d = datetime.fromtimestamp(now, tz=timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


class Protections:
    def __init__(self, rules: dict, store: Store, clock=time.time):
        self.r = rules
        self.store = store
        self.clock = clock
        self.halt_path = Path(rules.get("halt_file", "data/HALT"))

    # ── kill switch ──────────────────────────────────────────
    def halt(self, reason: str = "manual") -> None:
        self.halt_path.parent.mkdir(parents=True, exist_ok=True)
        self.halt_path.write_text(f"{datetime.now(timezone.utc).isoformat()} {reason}\n")

    def resume(self) -> None:
        self.halt_path.unlink(missing_ok=True)
        self.store.unlock("*")

    def halted(self) -> bool:
        return self.halt_path.exists()            # any content, even garbage, means halted

    # ── before a buy ─────────────────────────────────────────
    def check_entry(self, mint: str, signal: str) -> Gate:
        now = self.clock()
        if self.halted():
            return Gate(False, 0, "kill switch is on (HALT file) — /resume to trade again")
        g = self.store.active_lock("*")
        if g:
            return Gate(False, 0, f"paused until {time.strftime('%H:%M', time.localtime(g['until']))}: {g['reason']}")
        c = self.store.active_lock(f"mint:{mint}")
        if c:
            return Gate(False, 0, f"cooling down on this coin: {c['reason']}")
        s = self.store.active_lock(f"signal:{signal}")
        if s:
            return Gate(False, 0, f"{signal} is benched: {s['reason']}")

        day = _utc_midnight(now)
        if self.store.buys_since(day) >= self.r.get("max_buys_per_day", 12):
            return Gate(False, 0, f"daily cap of {self.r.get('max_buys_per_day', 12)} buys reached")
        if signal and self.store.buys_since(day, signal) >= self.r.get("max_buys_per_signal_per_day", 3):
            return Gate(False, 0, f"daily cap for {signal} reached")

        scale, notes = 1.0, []
        card = self.scorecard(signal)
        if card and card["n"] >= self.r.get("min_outcomes", 10):
            if card["hit_rate"] <= self.r.get("bench_hit_rate", 0.10):
                self.store.lock(f"signal:{signal}", now + self.r.get("bench_days", 7) * 86400,
                                f"only {card['hit_rate']:.0%} of its last {card['n']} trades reached 2x")
                return Gate(False, 0, f"{signal} benched: {card['hit_rate']:.0%} of {card['n']} trades reached 2x")
            if card["hit_rate"] <= self.r.get("weak_hit_rate", 0.25):
                scale *= 0.5
                notes.append(f"{signal} weak record ({card['hit_rate']:.0%} hit 2x) → half size")
        streak = self.losing_streak()
        if self.r.get("losing_streak", 0) and streak >= self.r["losing_streak"]:
            scale *= 0.5
            notes.append(f"{streak} losers in a row → half size")
        return Gate(True, round(scale, 3), "; ".join(notes))

    # ── after a close ────────────────────────────────────────
    def after_close(self, mint: str, code: str, pnl: float) -> list[str]:
        """Record locks a close should cause. Returns human-readable notes for the log."""
        now, notes = self.clock(), []
        cool = self.r.get("coin_cooldown_minutes", 60)
        if cool:
            self.store.lock(f"mint:{mint}", now + cool * 60, f"sold {cool} min ago ({code})")

        window = now - self.r.get("stopout_lookback_hours", 24) * 3600
        bad = [r for r in self.store.closed(window) if self.is_anomaly(r)]
        if bad and bad[-1]["token"] == mint and len(bad) >= self.r.get("stopout_limit", 3):
            hours = self.r.get("stopout_pause_hours", 12)
            self.store.lock("*", now + hours * 3600, f"{len(bad)} bad exits in {self.r.get('stopout_lookback_hours', 24)}h")
            notes.append(f"PROTECTION: {len(bad)} bad exits recently — pausing new buys for {hours}h")

        dd = self.rolling_drawdown()
        if dd >= self.r.get("max_drawdown_pct", 30):
            hours = self.r.get("drawdown_pause_hours", 24)
            self.store.lock("*", now + hours * 3600, f"equity down {dd:.0f}% from its recent peak")
            notes.append(f"PROTECTION: equity {dd:.0f}% below its {self.r.get('drawdown_lookback_hours', 72)}h peak — pausing {hours}h")
        return notes

    @staticmethod
    def is_anomaly(r) -> bool:
        if (r["pnl_usd"] or 0) >= 0:
            return False
        if r["exit_code"] in ANOMALY_EXITS:
            return True
        held = (r["closed_at"] or 0) - (r["opened_at"] or 0)
        return r["exit_code"] in BAD_EXITS and held <= QUICK_STOP_SECONDS

    # ── stats ────────────────────────────────────────────────
    def scorecard(self, signal: str) -> dict | None:
        if not signal:
            return None
        since = self.clock() - self.r.get("scorecard_days", 30) * 86400
        rows = [r for r in self.store.closed(since) if r["signal"] == signal]
        if not rows:
            return None
        hits = sum(1 for r in rows if r["hit_2x"])
        return {"n": len(rows), "hit_rate": hits / len(rows), "pnl": sum(r["pnl_usd"] or 0 for r in rows)}

    def losing_streak(self) -> int:
        streak = 0
        for r in reversed(self.store.closed(self.clock() - 14 * 86400)):
            if (r["pnl_usd"] or 0) < 0:
                streak += 1
            else:
                break
        return streak

    def rolling_drawdown(self) -> float:
        curve = self.store.equity_curve(self.clock() - self.r.get("drawdown_lookback_hours", 72) * 3600)
        if len(curve) < 2:
            return 0.0
        peak = max(e for _, e in curve)
        return (1 - curve[-1][1] / peak) * 100 if peak > 0 else 0.0
