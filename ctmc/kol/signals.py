"""Turning smart-money activity into decisions.

ENTRY
  confluence   when a celebrity tweets a coin: which proven, followable wallets already
               hold it? More (and better) wallets in -> bigger position, up to a cap.
  cluster      several proven wallets buying the same coin within minutes -> candidate
               entry (used for celebrity-name narrative coins that have no CA tweet).
  celebrity    a celebrity's own wallet buying = as strong as their tweet.

EXIT — "is this sell a signal or not?"
  ignore   wallet has no proven edge or flips in seconds (its exits carry no information);
           or a small trim (profit taking, which our own ladder does too)
  trim     a meaningful share of the smart money that was in has left, or a strong
           wallet exits far sooner than it normally holds -> sell half of what's left
  exit     most of the smart money is out -> sell everything
  urgent   a celebrity's own wallet sells -> sell everything now
Every call carries a plain-English reason so you can see why.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ctmc.kol.registry import Registry
from ctmc.kol.tracker import KolTrade, KolTracker


@dataclass
class Confluence:
    wallets: dict[str, float] = field(default_factory=dict)    # wallet -> weight
    names: list[str] = field(default_factory=list)
    celebrity: bool = False

    @property
    def count(self) -> int:
        return len(self.wallets)

    @property
    def weight(self) -> float:
        return round(sum(self.wallets.values()), 3)

    def describe(self) -> str:
        if not self.wallets:
            return "no tracked smart money in yet"
        return f"{self.count} smart wallet{'s' if self.count != 1 else ''} in ({', '.join(self.names[:4])})"


@dataclass
class Consensus:
    """What the whole tracked crowd is doing with one coin — more wallets, closer to the truth."""
    holders: int = 0              # proven wallets holding it now
    holder_weight: float = 0.0
    buyers: int = 0               # proven wallets that bought in the window
    sellers: int = 0              # proven wallets that sold in the window
    net_sol: float = 0.0          # SOL in (buys) minus SOL out (sells) by proven wallets in the window
    flippers: int = 0             # fast-flip / copy-trade-farming wallets that bought in the bait window

    def describe(self) -> str:
        flow = f"net {'+' if self.net_sol >= 0 else '-'}{abs(self.net_sol):.1f} SOL"
        return (f"crowd: {self.holders} proven holders · 1h {self.buyers} buying / {self.sellers} selling ({flow})"
                + (f" · {self.flippers} fast-flippers just in" if self.flippers else ""))


@dataclass
class SellCall:
    action: str      # ignore | trim | exit | urgent
    reason: str


@dataclass
class _Watch:
    holders: dict[str, float] = field(default_factory=dict)     # wallet -> weight
    exited: set[str] = field(default_factory=set)
    trimmed: bool = False


class SmartMoney:
    def __init__(self, registry: Registry, tracker: KolTracker, rules: dict):
        self.reg = registry
        self.tracker = tracker
        self.r = rules
        self.watch: dict[str, _Watch] = {}

    # ── entry ────────────────────────────────────────────────
    async def confluence(self, mint: str, live_check: bool = True) -> Confluence:
        c = Confluence()
        if live_check and self.r.get("check_balances_on_entry", True):
            top = sorted(self.reg.tracked(self.r), key=lambda k: -k.weight(self.r))[: self.r.get("balance_check_top_n", 25)]
            await self.tracker.balances_of(mint, [w for k in top for w in k.wallets])
        for wallet, bal in self.tracker.holdings.get(mint, {}).items():
            kol = self.reg.by_wallet.get(wallet)
            w = kol.weight(self.r) if kol else 0.0
            if kol and bal > 0 and w > 0:
                c.wallets[wallet] = w
                c.names.append(f"{kol.name} [{kol.tier(self.r)}]")
                c.celebrity |= kol.role == "celebrity"
        return c

    def size_multiplier(self, c: Confluence) -> float:
        """1.0 with no confirmation, rising with total smart-money weight, capped."""
        step = self.r.get("size_step_per_weight", 0.5)
        return round(min(self.r.get("max_size_multiplier", 2.0), 1.0 + step * c.weight), 2)

    def cluster(self, mint: str) -> Confluence:
        """Proven wallets that bought this coin within the cluster window."""
        c = Confluence()
        for t in self.tracker.buyers(mint, within_s=self.r.get("cluster_window_s", 900)):
            w = t.kol.weight(self.r)
            if w > 0:
                c.wallets[t.wallet] = w
                c.names.append(f"{t.kol.name} [{t.kol.tier(self.r)}]")
                c.celebrity |= t.kol.role == "celebrity"
        return c

    def consensus(self, mint: str, window_s: float | None = None) -> Consensus:
        """Aggregate every tracked wallet's activity in this coin (in-memory, instant)."""
        now, window = time.time(), window_s or self.r.get("consensus_window_s", 3600)
        bait_window = self.r.get("bait_window_s", 120)
        c = Consensus()
        for wallet, bal in self.tracker.holdings.get(mint, {}).items():
            kol = self.reg.by_wallet.get(wallet)
            w = kol.weight(self.r) if kol else 0.0
            if bal > 0 and w > 0:
                c.holders += 1
                c.holder_weight += w
        buyers, sellers, flippers = set(), set(), set()
        for t in reversed(self.tracker.recent):
            if now - t.ts > window:
                break
            if t.mint != mint:
                continue
            if t.kol.weight(self.r) > 0 or t.kol.role == "celebrity":
                (buyers if t.side == "buy" else sellers).add(t.wallet)
                c.net_sol += t.sol if t.side == "buy" else -t.sol
            elif t.side == "buy" and self.is_flipper(t.kol) and now - t.ts <= bait_window:
                flippers.add(t.wallet)
        c.buyers, c.sellers, c.flippers = len(buyers), len(sellers), len(flippers)
        c.holder_weight = round(c.holder_weight, 3)
        c.net_sol = round(c.net_sol, 3)
        return c

    def is_flipper(self, kol) -> bool:
        """Profitable but flips in seconds: farms its copy-traders. Information, never confirmation."""
        return kol.role != "celebrity" and kol.quality(self.r) > 0 and kol.followability(self.r) == 0

    def entry_warning(self, mint: str, change_m5: float) -> str:
        """Reasons NOT to buy right now, from the crowd. '' = no objection."""
        c = self.consensus(mint)
        if c.flippers >= self.r.get("bait_min_wallets", 1) and change_m5 >= self.r.get("bait_pump_pct_5m", 25):
            return (f"copy-trade wave: {c.flippers} fast-flip KOL(s) bought in the last {self.r.get('bait_window_s', 120)}s "
                    f"and it's already +{change_m5:.0f}% in 5 min — we'd be their exit liquidity")
        if (self.r.get("skip_if_smart_selling", True) and c.sellers >= self.r.get("smart_selling_min_wallets", 3)
                and c.sellers > 2 * c.buyers and c.net_sol < 0):
            return f"smart money is selling into this hype: {c.sellers} proven wallets out vs {c.buyers} in this hour"
        return ""

    def is_cluster(self, c: Confluence) -> bool:
        return c.count >= self.r.get("cluster_min_wallets", 3) or c.weight >= self.r.get("cluster_min_weight", 1.5)

    # ── position bookkeeping ─────────────────────────────────
    def opened(self, mint: str, c: Confluence | None) -> None:
        self.watch[mint] = _Watch(holders=dict(c.wallets) if c else {})

    def joined(self, mint: str, wallet: str, kol) -> None:
        """A proven wallet bought into a coin we hold: it now counts in the exit math."""
        w = kol.weight(self.r)
        if w > 0:
            self.watch.setdefault(mint, _Watch()).holders.setdefault(wallet, w)

    def closed(self, mint: str) -> None:
        self.watch.pop(mint, None)

    # ── "is this sell a signal?" ────────────────────────────
    def decode_sell(self, t: KolTrade, now: float | None = None) -> SellCall:
        now = now or time.time()
        kol, frac = t.kol, t.fraction_sold
        if kol.role == "celebrity":
            return SellCall("urgent", f"celebrity wallet {kol.name} sold {frac:.0%} of their bag")
        w = kol.weight(self.r)
        if w <= 0:
            why = "flips in seconds" if kol.quality(self.r) > 0 else "no proven edge"
            return SellCall("ignore", f"{kol.name} sold, but they {why} — their exits don't mean much")

        watch = self.watch.setdefault(t.mint, _Watch())
        watch.holders.setdefault(t.wallet, w)
        bought_at = self.tracker.first_buy.get((t.wallet, t.mint))
        held = now - bought_at if bought_at else None
        typical = kol.stats.median_hold_s
        early = held is not None and typical > 0 and held < self.r.get("early_exit_fraction", 0.5) * typical
        if frac >= self.r.get("full_exit_fraction", 0.9) or t.balance_after <= 0:
            watch.exited.add(t.wallet)

        total = sum(watch.holders.values()) or w
        gone = sum(watch.holders.get(x, 0) for x in watch.exited)
        share = gone / total
        held_txt = f" after {held / 60:.0f} min (they usually hold {typical / 60:.0f} min)" if held is not None and typical else ""

        # a full exit needs real consensus: 2+ proven wallets out, or every one of them gone
        if share >= self.r.get("exit_share", 0.5) and (len(watch.exited) >= 2 or share >= 0.99):
            return SellCall("exit", f"{share:.0%} of the smart money is out — last: {kol.name} sold {frac:.0%}{held_txt}")
        if frac >= 0.9 and early and w >= self.r.get("early_exit_min_weight", 0.4):
            if not watch.trimmed:
                watch.trimmed = True
                return SellCall("trim", f"{kol.name} dumped everything early{held_txt} — taking half off")
        if share >= self.r.get("trim_share", 0.25) and not watch.trimmed:
            watch.trimmed = True
            return SellCall("trim", f"{share:.0%} of the smart money has left ({kol.name} sold {frac:.0%}) — taking half off")
        if frac < 0.5:
            return SellCall("ignore", f"{kol.name} trimmed {frac:.0%}{held_txt} — normal profit taking")
        return SellCall("ignore", f"{kol.name} sold {frac:.0%}{held_txt}; {share:.0%} of smart money out so far — holding")
