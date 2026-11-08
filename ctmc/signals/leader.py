"""Leader-token guard.

Meme coins on a chain move with their "governing" tokens (SOL, WIF, FARTCOIN).
When the leaders roll over, the long tail usually follows a beat later.
We watch the leaders and get out *before* the followers' holders panic.
"""
from __future__ import annotations

import time
from collections import deque


class LeaderGuard:
    def __init__(self, rules: dict):
        self.window_s = rules.get("window_minutes", 5) * 60
        self.exit_drop = rules.get("exit_drop_pct", 2.5)
        self.block_drop = rules.get("block_entries_drop_pct", 1.5)
        self.release = rules.get("release_ratio", 0.5)      # hysteresis: must recover to half the trigger
        self.stale_s = rules.get("stale_seconds", 90)
        self.history: dict[str, deque[tuple[float, float]]] = {}
        self._blocked = False
        self.last_update = 0.0

    def update(self, name: str, price: float, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        self.last_update = max(self.last_update, ts)
        q = self.history.setdefault(name, deque())
        q.append((ts, price))
        while q and ts - q[0][0] > self.window_s:
            q.popleft()

    def change_pct(self, name: str) -> float:
        """% change from the window's high to now (a drawdown is negative)."""
        q = self.history.get(name)
        if not q or len(q) < 2:
            return 0.0
        peak = max(p for _, p in q)
        return (q[-1][1] / peak - 1) * 100

    def worst(self) -> tuple[str, float]:
        if not self.history:
            return ("-", 0.0)
        name = min(self.history, key=self.change_pct)
        return name, self.change_pct(name)

    def risk_off(self) -> bool:
        """Leaders are dumping -> sell exposure now."""
        return self.worst()[1] <= -self.exit_drop

    def stale(self) -> bool:
        """Leader prices were flowing and stopped: the guard is blind, so don't buy."""
        return bool(self.history) and time.time() - self.last_update > self.stale_s

    def entries_blocked(self) -> bool:
        if self.stale():
            return True
        chg = self.worst()[1]
        if chg <= -self.block_drop:
            self._blocked = True
        elif self._blocked and chg > -self.block_drop * self.release:
            self._blocked = False                           # no flapping on/off around the threshold
        return self._blocked
