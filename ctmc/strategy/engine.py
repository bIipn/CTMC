"""The trading brain — event-driven, no timers deciding when to look.

  on_tweet       a watched celebrity posts -> (a) a coin in the tweet: verify, check, buy;
                 (b) any post: the celebrity is "hot", their words feed the narrative radar.
  on_kol_trade   a tracked KOL / celebrity wallet buys or sells -> size, enter radar
                 coins on a smart-money cluster, decode their sells into exit actions.
  on_new_token   a new pump.fun coin launches -> does its name fit a hot narrative?
  on_price       every price tick (about 1s) -> exit ladder, crash backstop, leader guard.
  on_metrics     every few seconds per held coin -> rug watch, volume death, sell pressure.

Rule profiles switch on market cap: EARLY (small caps, catching it before the millions:
smaller bets, early-coin thresholds) vs ESTABLISHED (the main rules).
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
    kind: str        # BUY | SELL | SKIP | HALT | TWEET | KOL | RADAR
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
    rugwatch: object = None
    smart: object = None          # ctmc.kol.signals.SmartMoney
    narrative: object = None      # ctmc.signals.narrative.Narrative
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
        self._buying: set[str] = set()                    # mints with a buy in flight
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

    # ── profiles ─────────────────────────────────────────────
    def profile_for(self, s: PairSnapshot) -> tuple[str, dict]:
        early = (self.cfg.get("profiles") or {}).get("early")
        if early and s.market_cap and early.get("min_mcap_usd", 0) <= s.market_cap <= early.get("max_mcap_usd", 0):
            return "early", early
        return "established", {}

    # ── 1. tweets ────────────────────────────────────────────
    async def on_tweet(self, t: Tweet) -> None:
        ok, why = self.celebs.accept(t)
        if not ok:
            return
        who = f"@{t.handle}"
        coins = extract_coins(t.text)
        if self.narrative:
            words = self.narrative.on_tweet(t.handle, t.text)
            if words and self.cfg.get("narrative", {}).get("search_on_tweet", True):
                asyncio.create_task(self.scan_narrative(words, t.handle))
        if not coins:
            if self.narrative:
                self.log("TWEET", who, f"no coin in post — watching for coins named: {', '.join(self.narrative.hot.get(t.handle, (0, set()))[1]) or '-'}")
            return
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

    # ── narrative radar ──────────────────────────────────────
    async def scan_narrative(self, words: list[str], handle: str) -> None:
        """Search DEXes for coins named after what a celebrity just said."""
        search = getattr(self.market, "search_name", None)
        if not search:
            return
        for w in words[:3]:
            try:
                found = await search(w)
            except Exception:
                continue
            for s in found:
                self._radar_add(s.token, s.name or s.symbol, s.symbol,
                                s.created_at_ms / 1000 if s.created_at_ms else None, "dex search")

    async def on_new_token(self, tok) -> None:
        """PumpPortal: a coin just launched on pump.fun."""
        if self.narrative:
            self._radar_add(tok.mint, tok.name, tok.symbol, tok.ts, "new launch")

    def _radar_add(self, mint: str, name: str, symbol: str, created: float | None, source: str) -> None:
        if not self.narrative or mint in self.narrative.radar:
            return
        coin = self.narrative.consider(mint, name, symbol, created, source)
        if coin:
            self.log("RADAR", symbol or mint[:6],
                     f"fits @{coin.handle}'s '{coin.keyword}' narrative ({source}) — waiting for smart money")

    async def check_radar(self) -> None:
        """Buy a radar coin once proven wallets cluster into it."""
        if not (self.narrative and self.smart):
            return
        for mint, coin in list(self.narrative.radar.items()):
            if mint in self.positions or mint in self._buying:
                continue
            c = self.smart.cluster(mint)
            if self.smart.is_cluster(c):
                self.narrative.radar.pop(mint, None)
                await self.try_buy(mint, reason=f"@{coin.handle} '{coin.keyword}' narrative + {c.describe()}")

    # ── 2. smart-money wallets ───────────────────────────────
    async def on_kol_trade(self, t) -> None:
        kol, sym = t.kol, (self.last_snap[t.mint].symbol if t.mint in self.last_snap else t.mint[:6])
        if t.side == "buy":
            if t.mint in self.positions and self.smart:
                self.smart.joined(t.mint, t.wallet, kol)
                self.log("KOL", sym, f"{kol.name} [{kol.tier(self.smart.r)}] bought in — smart money adding")
                return
            if kol.role == "celebrity":
                self.backed[t.mint] = (time.time(), kol.name)
                await self.try_buy(t.mint, reason=f"{kol.name}'s own wallet bought it")
                return
            if self.narrative and t.mint in self.narrative.radar:
                await self.check_radar()
            elif self.smart and self.cfg.get("kol", {}).get("allow_cluster_entries_without_celebrity"):
                c = self.smart.cluster(t.mint)
                if self.smart.is_cluster(c):
                    await self.try_buy(t.mint, reason=f"smart-money cluster: {c.describe()}")
            return

        pos = self.positions.get(t.mint)
        if not pos or not self.smart:
            return
        call = self.smart.decode_sell(t)
        self.log("KOL", pos.symbol, f"{call.action.upper()}: {call.reason}")
        if call.action == "urgent":
            await self.close(pos, f"smart money: {call.reason}", urgent=True)
        elif call.action == "exit":
            await self.close(pos, f"smart money: {call.reason}", urgent=True)
        elif call.action == "trim":
            await self.trim(pos, 0.5, f"smart money: {call.reason}")

    # ── 3. checks & buying ───────────────────────────────────
    async def _confluence(self, mint: str):
        if not self.smart:
            return None
        try:
            return await asyncio.wait_for(self.smart.confluence(mint),
                                          self.cfg["checks"]["timeout_seconds"] * 0.75)
        except Exception:
            return None

    async def vet(self, mint: str) -> tuple[Verdict, PairSnapshot | None, object, str]:
        """Market, safety, smart money and bubble map — in parallel, under one time budget."""
        budget = self.cfg["checks"]["timeout_seconds"]
        started = time.monotonic()
        try:
            snap, rep, conf = await asyncio.wait_for(asyncio.gather(
                self.market.snapshot(mint), self.safety.report(mint), self._confluence(mint),
                return_exceptions=True), budget)
        except asyncio.TimeoutError:
            return Verdict(False, [f"checks took over {budget}s — skipped"]), None, None, ""
        conf = None if isinstance(conf, Exception) else conf
        if isinstance(snap, Exception) or snap is None:
            return Verdict(False, ["no tradable pool found"]), None, conf, ""
        if isinstance(rep, Exception):
            return Verdict(False, [f"rugcheck unavailable ({rep.__class__.__name__}) — fail closed"]), snap, conf, ""
        if hasattr(self.safety, "last_report"):
            self.safety.last_report[mint] = rep

        name, prof = self.profile_for(snap)
        u = self.passes_universe(snap, prof.get("universe"))
        if not u:
            return u, snap, conf, name
        overrides = {**(prof.get("safety") or {}), **({"lp_exempt_market_types": prof["lp_exempt_market_types"]}
                                                        if prof.get("lp_exempt_market_types") else {})}
        v = self.safety.evaluate_solana(rep, overrides)
        if not v:
            return v, snap, conf, name
        try:
            remaining = max(0.5, budget - (time.monotonic() - started))
            cv = await asyncio.wait_for(self.safety.clusters_check(rep), remaining)
        except asyncio.TimeoutError:
            return Verdict(False, ["bubble-map check ran out of time — skipped"]), snap, conf, name
        if not cv:
            return cv, snap, conf, name
        if self.hype is not None and self.cfg["hype"].get("confirm_with_grok"):
            h = await self.hype.check(snap.symbol, mint)
            if not h:
                return h, snap, conf, name
        return Verdict(True, v.reasons + cv.reasons), snap, conf, name

    def passes_universe(self, s: PairSnapshot, overrides: dict | None = None) -> Verdict:
        u, why = {**self.cfg["universe"], **(overrides or {})}, []
        if s.liquidity_usd < u["min_liquidity_usd"]:
            why.append(f"liquidity ${s.liquidity_usd:,.0f} (all pools) too thin")
        if s.volume_h1 < u["min_volume_1h_usd"]:
            why.append(f"1h volume ${s.volume_h1:,.0f} too low")
        if s.age_minutes < u.get("min_pair_age_minutes", 0):
            why.append(f"pool only {s.age_minutes:.0f} min old")
        ratio = s.market_cap / s.liquidity_usd if s.liquidity_usd else float("inf")
        if s.market_cap and ratio > u.get("max_mcap_to_liquidity", float("inf")):
            why.append(f"market cap is {ratio:.0f}x its liquidity — too thin to exit")
        return Verdict(not why, why)

    async def try_buy(self, mint: str, reason: str, tweet: Tweet | None = None) -> None:
        b = self.cfg["bankroll"]
        if self.halted or mint in self.positions or mint in self.leader_mints or mint in self._buying:
            return
        if self.cfg["leader_guard"].get("enabled", True) and self.leaders.entries_blocked():
            lead, chg = self.leaders.worst()
            self.log("SKIP", mint[:6], f"leaders falling ({lead} {chg:+.1f}%) — not buying into that")
            return
        self._buying.add(mint)
        try:
            verdict, snap, conf, profile = await self.vet(mint)
            sym = snap.symbol if snap else mint[:6]
            if not verdict:
                self.log("SKIP", sym, "; ".join(verdict.reasons[:3]))
                return
            prof = (self.cfg.get("profiles") or {}).get(profile) or {}
            mult = self.smart.size_multiplier(conf) if (self.smart and conf) else 1.0
            async with self.lock:
                if mint in self.positions:
                    return
                if len(self.positions) >= b["max_open_positions"]:
                    self.log("SKIP", sym, "all position slots full")
                    return
                equity = self.equity()
                risk = prof.get("risk_per_trade_pct", b["risk_per_trade_pct"])
                size = min(equity * risk / 100 * mult, self.broker.cash - equity * b["reserve_pct"] / 100)
                if size < 5:
                    self.log("SKIP", sym, "no free cash outside the reserve")
                    return
                try:
                    fill = await self.broker.buy(mint, size, snap.price_usd, snap.liquidity_usd)
                except TradeError as exc:
                    self.log("SKIP", sym, f"buy failed: {exc}")
                    return
                pos = Position(mint, sym, fill.price, fill.qty, fill.usd, source=reason)
                pos.profile = profile
                self.positions[mint] = pos
                self.last_px[mint] = snap.price_usd
                self.last_snap[mint] = snap
        finally:
            self._buying.discard(mint)
        lag = f" · {time.time() - tweet.created_at:.1f}s after the tweet" if tweet else ""
        mcap = f" · mcap ${snap.market_cap:,.0f} ({profile})" if snap.market_cap else ""
        smart = f" · {conf.describe()} → size x{mult:g}" if conf is not None else ""
        self.log("BUY", sym, f"{reason} — ${fill.usd:,.0f} @ {fill.price:.6g}, impact {fill.impact_pct:.2f}%{lag}{mcap}{smart}")
        self.on_watch(mint)
        if self.smart:
            self.smart.opened(mint, conf)
        if self.rugwatch:
            report = getattr(self.safety, "last_report", {}).get(mint)
            asyncio.create_task(self.rugwatch.start(mint, report))

    # ── 4. prices ────────────────────────────────────────────
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

    # ── 5. flow metrics ──────────────────────────────────────
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
            if self.rugwatch:
                await self.rugwatch.on_metrics(s)          # may fire on_rug_alert -> urgent exit
                if s.token not in self.positions:
                    return
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
    async def on_rug_alert(self, mint: str, reason: str) -> None:
        pos = self.positions.get(mint)
        if pos:
            await self.close(pos, f"RUG ALERT — {reason}", urgent=True)

    async def trim(self, pos: Position, fraction: float, why: str) -> None:
        """Sell a fraction of what's left (keeps the position open)."""
        async with self.lock:
            if pos.token not in self.positions or pos.qty <= 0:
                return
            qty = pos.qty * fraction
            px = self.last_px.get(pos.token, pos.entry_price)
            liq = self.last_snap[pos.token].liquidity_usd if pos.token in self.last_snap else 0
            try:
                fill = await self.broker.sell(pos.token, qty, px, liq, urgent=True)
            except TradeError as exc:
                self.log("HALT", pos.symbol, f"trim failed — {exc}")
                return
            pos.qty -= qty
            pos.realized_usd += -fill.usd
            self.log("SELL", pos.symbol, f"{why} (sold {fraction:.0%}, +${-fill.usd:,.0f})")

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
        if self.rugwatch:
            self.rugwatch.stop(pos.token)
        if self.smart:
            self.smart.closed(pos.token)
