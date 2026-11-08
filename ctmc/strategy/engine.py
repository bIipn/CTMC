"""The trading brain.

Per tick:
  1. Read leaders (SOL / WIF / FARTCOIN). If they dump -> trim everything first.
  2. Manage open positions:
       - at 2x: sell enough to pull the original cost back out ("sell what you bought")
       - the rest is a free runner: trailing stop from its peak
       - exit the runner when buys dry up AND heavy sells hit
       - hard stop / max-hold time as backstops
  3. Look for entries among screened coins: only after a panic flush has exhausted.
     Every entry must pass universe + safety + X-hype filters.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ctmc.models import PairSnapshot, Position, Verdict
from ctmc.signals.flow import FlowReader
from ctmc.signals.leader import LeaderGuard


@dataclass
class Event:
    ts: float
    kind: str        # BUY | SELL | SKIP | HALT
    symbol: str
    detail: str
    pnl_usd: float = 0.0


@dataclass
class Engine:
    cfg: dict
    broker: object
    safety: object
    hype: object
    leaders: LeaderGuard = field(init=False)
    flow: FlowReader = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    day_start_equity: float = 0.0
    halted: bool = False
    cooldown_s: float = 1800           # no re-entry into a coin for 30 min after selling it
    _last_exit: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.leaders = LeaderGuard(self.cfg["leader_guard"])
        self.flow = FlowReader(self.cfg["flow"])
        self.day_start_equity = self.broker.cash
        self._vetted: dict[str, tuple[float, Verdict]] = {}

    # ── bookkeeping ──────────────────────────────────────────
    def log(self, kind: str, symbol: str, detail: str, pnl: float = 0.0) -> None:
        self.events.append(Event(time.time(), kind, symbol, detail, pnl))

    def equity(self, prices: dict[str, float]) -> float:
        return self.broker.cash + sum(p.value(prices.get(t, p.entry_price)) for t, p in self.positions.items())

    # ── filters ──────────────────────────────────────────────
    def passes_universe(self, s: PairSnapshot) -> Verdict:
        u, why = self.cfg["universe"], []
        if s.volume_24h < u["min_volume_24h_usd"]:
            why.append(f"24h volume ${s.volume_24h:,.0f} too low")
        if s.liquidity_usd < u["min_liquidity_usd"]:
            why.append(f"liquidity ${s.liquidity_usd:,.0f} too thin")
        if s.age_hours < u["min_pair_age_hours"]:
            why.append(f"pair only {s.age_hours:.1f}h old")
        return Verdict(not why, why)

    def vet(self, s: PairSnapshot) -> Verdict:
        """Safety + hype, cached 30 min so we don't hammer APIs every tick."""
        hit = self._vetted.get(s.token)
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
        v = self.safety.check(s.token, self.cfg.get("chain", "solana"))
        if v:
            v = self.hype.check(s.symbol, s.token)
        self._vetted[s.token] = (time.time(), v)
        return v

    # ── main loop body ───────────────────────────────────────
    def tick(self, leader_prices: dict[str, float], snaps: list[PairSnapshot]) -> None:
        for name, px in leader_prices.items():
            self.leaders.update(name, px)
        by_token = {s.token: s for s in snaps}
        prices = {s.token: s.price_usd for s in snaps}
        for s in snaps:
            self.flow.observe(s)

        self._check_daily_limit(prices)
        self._manage_positions(by_token)
        if not self.halted:
            self._find_entries(snaps, prices)

    def _check_daily_limit(self, prices: dict[str, float]) -> None:
        eq = self.equity(prices)
        dd = (eq / self.day_start_equity - 1) * 100 if self.day_start_equity else 0
        if not self.halted and dd <= -self.cfg["bankroll"]["daily_loss_limit_pct"]:
            self.halted = True
            self.log("HALT", "-", f"daily loss limit hit ({dd:.1f}%) — no new entries today")

    def _manage_positions(self, by_token: dict[str, PairSnapshot]) -> None:
        ex = self.cfg["exits"]
        risk_off = self.leaders.risk_off()
        for token, pos in list(self.positions.items()):
            s = by_token.get(token)
            if not s:
                continue
            px = s.price_usd
            pos.peak_price = max(pos.peak_price, px)

            # 1) leaders dumping: get out before this coin's holders panic
            if risk_off:
                lead, chg = self.leaders.worst()
                self._close(pos, px, f"leader {lead} {chg:+.1f}% — front-running the follow-through")
                continue

            # 2) take the initial: recover cost basis at 2x, keep the rest riding
            if not pos.initial_taken and pos.multiple(px) >= ex["take_initial_at_multiple"]:
                qty = min(pos.qty, pos.cost_usd / px)
                fill = self.broker.sell(token, qty, px)
                pos.qty -= qty
                pos.realized_usd += -fill.usd
                pos.initial_taken = True
                # PnL is booked once, on the final close, so the partial logs 0 here.
                self.log("SELL", pos.symbol,
                         f"initial pulled at {pos.multiple(px):.2f}x (${-fill.usd:,.0f} back) — runner is house money")
                continue

            # 3) runner exits
            trail = (px / pos.peak_price - 1) * 100
            if pos.initial_taken and trail <= -ex["runner_trailing_stop_pct"]:
                self._close(pos, px, f"runner trailing stop ({trail:.1f}% off peak)")
            elif self.flow.distribution(s) and pos.multiple(px) > 1.0:
                self._close(pos, px, "buys dried up + heavy sells — distribution")
            elif not pos.initial_taken and (pos.multiple(px) - 1) * 100 <= -ex["hard_stop_loss_pct"]:
                self._close(pos, px, f"hard stop ({(pos.multiple(px) - 1) * 100:.1f}%)")
            elif time.time() - pos.opened_at > ex["max_hold_hours"] * 3600:
                self._close(pos, px, "max hold time")

    def _close(self, pos: Position, px: float, why: str) -> None:
        fill = self.broker.sell(pos.token, pos.qty, px)
        pnl = pos.realized_usd + (-fill.usd) - pos.cost_usd
        self.positions.pop(pos.token, None)
        self._last_exit[pos.token] = time.time()
        self.log("SELL", pos.symbol, why, pnl)

    def _find_entries(self, snaps: list[PairSnapshot], prices: dict[str, float]) -> None:
        b = self.cfg["bankroll"]
        if self.leaders.entries_blocked() or len(self.positions) >= b["max_open_positions"]:
            return
        equity = self.equity(prices)
        deployable = self.broker.cash - equity * b["reserve_pct"] / 100
        size = equity * b["risk_per_trade_pct"] / 100
        if deployable < size or size <= 0:
            return

        for s in snaps:
            if s.token in self.positions or len(self.positions) >= b["max_open_positions"]:
                continue
            if time.time() - self._last_exit.get(s.token, 0) < self.cooldown_s:
                continue
            if not self.flow.capitulation_exhausted(s):
                continue
            if not (u := self.passes_universe(s)):
                self.log("SKIP", s.symbol, "; ".join(u.reasons))
                continue
            if not (v := self.vet(s)):
                self.log("SKIP", s.symbol, "; ".join(v.reasons[:3]))
                continue
            fill = self.broker.buy(s.token, size, s.price_usd)
            self.positions[s.token] = Position(s.token, s.symbol, fill.price, fill.qty, fill.usd)
            self.flow.consume(s.token)
            self.log("BUY", s.symbol, f"panic flush exhausted — bought ${fill.usd:,.0f} @ {fill.price:.6g}")
            deployable -= size
            if deployable < size:
                break
