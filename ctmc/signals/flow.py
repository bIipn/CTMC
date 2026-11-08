"""Order-flow reads from 5m / 1h buy-sell activity.

capitulation  — someone got scared and dumped: price flushed hard, sells swamped
                buys... and then the sellers ran out. That's the dip-buy.
distribution  — buys have dried up and heavy sells are coming through: get out.
"""
from __future__ import annotations

from collections import deque

from ctmc.models import PairSnapshot


class FlowReader:
    def __init__(self, rules: dict):
        self.r = rules
        # remembers recent panic flushes per token so we can buy the exhaustion after
        self._flush: dict[str, deque[float]] = {}

    def heavy_selling(self, s: PairSnapshot) -> bool:
        """Sell volume in the last 5m is a multiple of a normal 5m sell slice."""
        avg_sell_5m = (s.volume_h1 / 12) * 0.5        # typical 5m volume, half of it sells
        sell_share = s.sells_m5 / max(s.buys_m5 + s.sells_m5, 1)
        sell_vol_5m = s.volume_m5 * sell_share
        return avg_sell_5m > 0 and sell_vol_5m >= self.r.get("heavy_sell_volume_mult", 2.0) * avg_sell_5m

    def buyers_dried_up(self, s: PairSnapshot) -> bool:
        return s.buy_ratio_m5 < self.r.get("dryup_buy_ratio", 0.6)

    def distribution(self, s: PairSnapshot) -> bool:
        """Exit signal: buys dry up AND major sales come through."""
        return self.buyers_dried_up(s) and self.heavy_selling(s)

    def observe(self, s: PairSnapshot) -> None:
        q = self._flush.setdefault(s.token, deque(maxlen=12))
        flushed = (
            s.change_h1 <= -self.r.get("capitulation_drop_pct", 12)
            and s.sell_ratio_m5 >= self.r.get("capitulation_sell_ratio", 1.8)
        )
        q.append(1.0 if flushed else 0.0)

    def consume(self, token: str) -> None:
        """A flush is traded once; forget it after we buy it."""
        self._flush.pop(token, None)

    def capitulation_exhausted(self, s: PairSnapshot) -> bool:
        """Entry signal: a panic flush happened recently and sellers are now spent."""
        q = self._flush.get(s.token)
        if not q or sum(q) == 0 or q[-1] == 1.0:
            return False  # no flush yet, or still flushing — don't catch the knife
        return (
            s.sell_ratio_m5 <= self.r.get("exhaustion_sell_ratio", 1.1)
            and s.change_m5 >= 0  # first green 5m candle after the flush
        )
