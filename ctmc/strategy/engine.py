"""The trading brain — event-driven, no timers deciding when to look.

  on_tweet    a watched celebrity posts a coin -> verify the account, pull the
              contract address, run every check IN PARALLEL with a hard time
              budget, size the order to the pool, buy.
  on_price    every price tick (about 1s) -> exit ladder, crash backstop, leader guard.
  on_metrics  every few seconds per held coin -> volume death and distribution exits,
              plus a dip re-entry on coins a celebrity already backed.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from ctmc.execution.broker import TradeError
from ctmc.feeds.twitter import CelebrityFilter, Tweet, extract_coins
from ctmc.models import PairSnapshot, Position, Verdict
from ctmc.signals.flow import FlowReader
from ctmc.signals.leader import LeaderGuard
from ctmc.strategy.ladder import ladder_sells


@dataclass
class Event:
    ts: float
    kind: str        # BUY | SELL | SKIP | HALT | TWEET
    symbol: str
    detail: str
    pnl_usd: float = 0.0


@dataclass
class Trader:
    cfg: dict
    broker: object
    market: object
    safety: object
    hype: object = None
    positions: dict[str, Position] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    halted: bool = False

    def __post_init__(self) -> None:
        self.leaders = LeaderGuard(self.cfg["leader_guard"])
        self.leader_mints = {v: k for k, v in self.cfg["leaders"].items()}
        self.flow = FlowReader(self.cfg["flow"])
        self.celebs = CelebrityFilter(self.cfg["celebrities"], self.cfg["twitter"])
        self.day_start_equity = self.broker.cash
        self.last_px: dict[str, float] = {}
        self.last_snap: dict[str, PairSnapshot] = {}
        self.backed: dict[str, tuple[float, str]] = {}   # mint -> (when, who) celebrity-backed coins
        self.seen: set[tuple[str, str]] = set()          # (tweet id, mint) already handled
        self.lock = asyncio.Lock()
        self.on_watch = lambda mint: None                 # wired to the price feed by the runner
        self.on_unwatch = lambda mint: None

    # ── bookkeeping ──────────────────────────────────────────
    def log(self, kind: str, symbol: str, detail: str, pnl: float = 0.0) -> None:
        self.events.append(Event(time.time(), kind, symbol, detail, pnl))
        del self.events[:-500]

    def equity(self) -> float:
        return self.broker.cash + sum(p.value(self.last_px.get(t, p.entry_price)) for t, p in self.positions.items())

    def _check_daily_limit(self) -> None:
        dd = (self.equity() / self.day_start_equity - 1) * 100 if self.day_start_equity else 0
        if not self.halted and dd <= -self.cfg["bankroll"]["daily_loss_limit_pct"]:
            self.halted = True
            self.log("HALT", "-", f"daily loss limit hit ({dd:.1f}%) — no new buys today")

    # ── 1. tweets ────────────────────────────────────────────
    async def on_tweet(self, t: Tweet) -> None:
        ok, why = self.celebs.accept(t)
        if not ok:
            return
        coins = extract_coins(t.text)
        if not coins:
            return
        who = f"@{t.handle}"
        self.log("TWEET", who, (t.text[:90] + "…") if len(t.text) > 90 else t.text)

        targets = list(coins.mints)
        for ticker in coins.tickers:
            if targets:
                break  # an explicit address always beats a ticker
            snap = await self.market.search_ticker(ticker, self.cfg["twitter"]["ticker_min_liquidity_usd"])
            if snap:
                targets.append(snap.token)
            else:
                self.log("SKIP", f"${ticker}", "ticker is ambiguous or too small to trust — needs a contract address")
        for mint in targets[:2]:
            if (t.id, mint) in self.seen:
                continue
            self.seen.add((t.id, mint))
            self.backed[mint] = (time.time(), who)
            await self.try_buy(mint, reason=f"{who} tweeted it", tweet=t)

    async def vet(self, mint: str) -> tuple[Verdict, PairSnapshot | None]:
        """Market, safety and bubble-map checks at the same time, under one time budget."""
        jobs = [self.market.snapshot(mint), self.safety.check(mint)]
        use_grok = self.hype is not None and self.cfg["hype"].get("confirm_with_grok")
        try:
            res = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True),
                                         self.cfg["checks"]["timeout_seconds"])
        except asyncio.TimeoutError:
            return Verdict(False, [f"checks took over {self.cfg['checks']['timeout_seconds']}s — skipped"]), None
        snap, safety = res
        if isinstance(snap, Exception) or snap is None:
            return Verdict(False, ["no tradable pool found"]), None
        if isinstance(safety, Exception):
            return Verdict(False, [f"safety check error: {safety}"]), snap
        u = self.passes_universe(snap)
        if not u:
            return u, snap
        if not safety:
            return safety, snap
        if use_grok:
            h = await self.hype.check(snap.symbol, mint)
            if not h:
                return h, snap
        return Verdict(True, safety.reasons), snap

    def passes_universe(self, s: PairSnapshot) -> Verdict:
        u, why = self.cfg["universe"], []
        if s.liquidity_usd < u["min_liquidity_usd"]:
            why.append(f"liquidity ${s.liquidity_usd:,.0f} (all pools) too thin")
        if s.volume_h1 < u["min_volume_1h_usd"]:
            why.append(f"1h volume ${s.volume_h1:,.0f} too low")
        if s.age_minutes < u.get("min_pair_age_minutes", 0):
            why.append(f"pool only {s.age_minutes:.0f} min old")
        return Verdict(not why, why)

    async def try_buy(self, mint: str, reason: str, tweet: Tweet | None = None) -> None:
        b = self.cfg["bankroll"]
        if self.halted or mint in self.positions or mint in self.leader_mints:
            return
        if self.cfg["leader_guard"].get("enabled", True) and self.leaders.entries_blocked():
            lead, chg = self.leaders.worst()
            self.log("SKIP", mint[:6], f"leaders falling ({lead} {chg:+.1f}%) — not buying into that")
            return
        verdict, snap = await self.vet(mint)
        sym = snap.symbol if snap else mint[:6]
        if not verdict:
            self.log("SKIP", sym, "; ".join(verdict.reasons[:3]))
            return
        async with self.lock:
            if mint in self.positions:
                return
            if len(self.positions) >= b["max_open_positions"]:
                self.log("SKIP", sym, "all position slots full")
                return
            equity = self.equity()
            size = min(equity * b["risk_per_trade_pct"] / 100,
                       self.broker.cash - equity * b["reserve_pct"] / 100)
            if size < 5:
                self.log("SKIP", sym, "no free cash outside the reserve")
                return
            try:
                fill = await self.broker.buy(mint, size, snap.price_usd, snap.liquidity_usd)
            except TradeError as exc:
                self.log("SKIP", sym, f"buy failed: {exc}")
                return
            pos = Position(mint, sym, fill.price, fill.qty, fill.usd, source=reason)
            self.positions[mint] = pos
            self.last_px[mint] = snap.price_usd
            self.last_snap[mint] = snap
        lag = f" · {time.time() - tweet.created_at:.1f}s after the tweet" if tweet else ""
        self.log("BUY", sym, f"{reason} — ${fill.usd:,.0f} @ {fill.price:.6g}, impact {fill.impact_pct:.2f}%{lag}")
        self.on_watch(mint)

    # ── 2. prices ────────────────────────────────────────────
    async def on_price(self, mint: str, px: float) -> None:
        if mint in self.leader_mints:
            self.leaders.update(self.leader_mints[mint], px)
            if self.cfg["leader_guard"].get("enabled", True) and self.leaders.risk_off() and self.positions:
                lead, chg = self.leaders.worst()
                for p in list(self.positions.values()):
                    await self.close(p, f"leader {lead} {chg:+.1f}% — out before the followers dump", urgent=True)
            return
        self.last_px[mint] = px
        pos = self.positions.get(mint)
        if not pos:
            return
        pos.peak_price = max(pos.peak_price, px)
        ex = self.cfg["exits"]
        liq = self.last_snap[mint].liquidity_usd if mint in self.last_snap else 0

        async with self.lock:
            if mint not in self.positions:
                return
            for s in ladder_sells(pos, px, ex):
                qty = min(s.qty, pos.qty)
                if qty <= 0:
                    break
                try:
                    fill = await self.broker.sell(mint, qty, px, liq)
                except TradeError as exc:
                    self.log("HALT", pos.symbol, f"ladder sell failed: {exc}")
                    break
                pos.qty -= qty
                pos.realized_usd += -fill.usd
                pos.initial_taken |= s.initial
                if s.rung:
                    pos.ladder_hits.append(s.rung)
                self.log("SELL", pos.symbol, f"{s.reason} (+${-fill.usd:,.0f})")
                if s.closes or pos.qty <= 0:
                    self._finish(pos, "fully laddered out")
                    return

        drop = (px / pos.peak_price - 1) * 100
        loss = (pos.multiple(px) - 1) * 100
        if pos.initial_taken and drop <= -ex["crash_trailing_stop_pct"]:
            await self.close(pos, f"crash backstop: {drop:.0f}% off the peak", urgent=True)
        elif not pos.initial_taken and loss <= -ex["hard_stop_loss_pct"]:
            await self.close(pos, f"hard stop ({loss:.0f}%)", urgent=True)
        elif ex.get("max_hold_hours") and time.time() - pos.opened_at > ex["max_hold_hours"] * 3600:
            await self.close(pos, "max hold time")
        self._check_daily_limit()

    # ── 3. flow metrics ──────────────────────────────────────
    def volume_dead(self, pos: Position, s: PairSnapshot) -> bool:
        ex = self.cfg["exits"]
        normal_5m = s.volume_h1 / 12
        if normal_5m > 0 and s.volume_m5 < normal_5m * ex["volume_death_ratio"]:
            pos.low_volume_streak += 1
        else:
            pos.low_volume_streak = 0
        return pos.low_volume_streak >= ex["volume_death_checks"]

    async def on_metrics(self, s: PairSnapshot) -> None:
        self.last_snap[s.token] = s
        self.flow.observe(s)
        pos = self.positions.get(s.token)
        if pos:
            if self.volume_dead(pos, s):
                await self.close(pos, "volume died — the party's over")
            elif self.flow.distribution(s) and pos.multiple(s.price_usd) > 1.0:
                await self.close(pos, "buys dried up + heavy sells hitting", urgent=True)
            return
        # dip re-entry: only coins a celebrity backed in the last 72h
        backed = self.backed.get(s.token)
        if backed and time.time() - backed[0] < 72 * 3600 and self.flow.capitulation_exhausted(s):
            self.flow.consume(s.token)
            await self.try_buy(s.token, reason=f"panic flush exhausted (backed by {backed[1]})")

    # ── exits ────────────────────────────────────────────────
    async def close(self, pos: Position, why: str, urgent: bool = False) -> None:
        async with self.lock:
            if pos.token not in self.positions or pos.qty <= 0:
                return
            px = self.last_px.get(pos.token, pos.entry_price)
            liq = self.last_snap[pos.token].liquidity_usd if pos.token in self.last_snap else 0
            try:
                fill = await self.broker.sell(pos.token, pos.qty, px, liq, urgent=urgent)
            except TradeError as exc:
                self.log("HALT", pos.symbol, f"EXIT FAILED — {exc}. Will retry on next tick.")
                return
            pos.realized_usd += -fill.usd
            pos.qty = 0
            self._finish(pos, why)

    def _finish(self, pos: Position, why: str) -> None:
        self.positions.pop(pos.token, None)
        self.log("SELL", pos.symbol, why, pos.realized_usd - pos.cost_usd)
        self.on_unwatch(pos.token)
