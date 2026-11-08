"""Who the smart money is.

A local registry of KOL and celebrity wallets with their track record. Filled by
`python -m ctmc kols import` (kolexplorer.com) and/or `python -m ctmc kols score`
(our own on-chain scoring), and by hand for celebrity wallets you know.

Two separate questions decide how much a wallet matters:
  quality        do they actually make money? (win rate, realized PnL, sample size)
  followability  can we act on their moves in time? A wallet that holds for
                 16 seconds is a bot: by the time we see the buy, they're out.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("data/kols.json")


@dataclass
class KolStats:
    win_rate: float = 0.0           # % of closed positions in profit (30d preferred, else 7d)
    pnl_30d_usd: float = 0.0        # realized
    pnl_7d_usd: float = 0.0
    trades_7d: int = 0
    tokens_7d: int = 0              # distinct tokens traded in 7d
    closed_trades: int = 0
    avg_position_usd: float = 0.0
    median_hold_s: float = 0.0
    median_entry_mcap_usd: float = 0.0
    profit_factor: float = 0.0      # gross wins / gross losses (own scoring)
    flip_rate: float = 0.0          # share of positions closed < 60s (own scoring)
    rug_rate: float = 0.0           # share of positions that lost > 90% (own scoring)
    source: str = ""
    updated_at: float = 0.0


@dataclass
class Kol:
    name: str
    wallets: list[str]
    x_handle: str = ""
    role: str = "kol"               # "kol" | "celebrity"
    stats: KolStats = field(default_factory=KolStats)
    notes: str = ""

    # ── scoring ──────────────────────────────────────────────
    def quality(self, rules: dict) -> float:
        """0..1: proven profitability. 0 = ignore this wallet."""
        if self.role == "celebrity":
            return 1.0
        s = self.stats
        if s.win_rate < rules.get("min_win_rate", 55):
            return 0.0
        if s.pnl_30d_usd < rules.get("min_pnl_30d_usd", 5000):
            return 0.0
        if s.closed_trades and s.closed_trades < rules.get("min_closed_trades", 20):
            return 0.0
        if s.rug_rate > rules.get("max_rug_rate", 0.35):
            return 0.0
        wr = min(1.0, (s.win_rate - 50) / 25)                       # 50% -> 0, 75% -> 1
        pnl = min(1.0, s.pnl_30d_usd / rules.get("pnl_for_full_score", 100_000))
        return round(max(0.05, 0.6 * wr + 0.4 * pnl), 3)

    def followability(self, rules: dict) -> float:
        """0..1: how long they hold relative to what we need to react."""
        if self.role == "celebrity":
            return 1.0
        s = self.stats
        if s.trades_7d > rules.get("max_trades_7d", 1500):         # thousands a week = a bot
            return 0.0
        if s.flip_rate > rules.get("max_flip_rate", 0.6):
            return 0.0
        hold = s.median_hold_s
        need = rules.get("min_median_hold_s", 300)
        if hold <= 0 or hold < need:
            return 0.0
        return round(min(1.0, hold / rules.get("full_follow_hold_s", 3600)), 3)

    def weight(self, rules: dict) -> float:
        return round(self.quality(rules) * max(self.followability(rules), 0.0), 3)

    def tier(self, rules: dict) -> str:
        if self.role == "celebrity":
            return "CELEB"
        q, f = self.quality(rules), self.followability(rules)
        if q == 0:
            return "no edge"
        if f == 0:
            return "unfollowable"
        return "A" if q >= 0.7 else "B" if q >= 0.4 else "C"


class Registry:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self.kols: dict[str, Kol] = {}
        self.by_wallet: dict[str, Kol] = {}

    def load(self) -> "Registry":
        if self.path.exists():
            for raw in json.loads(self.path.read_text()):
                stats = KolStats(**{k: v for k, v in (raw.pop("stats", {}) or {}).items()
                                    if k in KolStats.__dataclass_fields__})
                self.add(Kol(**{k: v for k, v in raw.items() if k in Kol.__dataclass_fields__}, stats=stats))
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted((asdict(k) for k in self.kols.values()), key=lambda r: -r["stats"]["pnl_30d_usd"])
        self.path.write_text(json.dumps(rows, indent=2))

    def add(self, kol: Kol) -> None:
        key = kol.name.lower()
        old = self.kols.get(key)
        if old:                                   # merge: keep wallets from both, newest stats win
            kol.wallets = list(dict.fromkeys(old.wallets + kol.wallets))
            if kol.stats.updated_at < old.stats.updated_at:
                kol.stats = old.stats
            kol.role = "celebrity" if "celebrity" in (old.role, kol.role) else kol.role
            kol.x_handle = kol.x_handle or old.x_handle
        self.kols[key] = kol
        for w in kol.wallets:
            self.by_wallet[w] = kol

    def tracked(self, rules: dict) -> list[Kol]:
        """Wallets worth streaming: celebrities + KOLs with a real, followable edge."""
        return [k for k in self.kols.values() if k.role == "celebrity" or k.weight(rules) > 0]

    def wallets(self, rules: dict) -> list[str]:
        return [w for k in self.tracked(rules) for w in k.wallets]


def parse_money(text: str) -> float:
    """'+$72.3K' -> 72300, '$-4,587' -> -4587, '$1.4M' -> 1_400_000."""
    t = str(text).replace(",", "").replace("$", "").replace("+", "").strip()
    neg = t.startswith("-") or t.startswith("−")
    t = t.lstrip("-−")
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(t[-1:].upper(), 1)
    try:
        v = float(t[:-1] if mult != 1 else t) * mult
    except ValueError:
        return 0.0
    return -v if neg else v


def parse_duration(text: str) -> float:
    """'16 s' -> 16, '12 min' -> 720, '4.2 h' -> 15120, '3 d' -> 259200."""
    parts = str(text).strip().split()
    if not parts:
        return 0.0
    try:
        n = float(parts[0])
    except ValueError:
        return 0.0
    unit = parts[1].lower() if len(parts) > 1 else "s"
    return n * (86400 if unit.startswith("d") else 3600 if unit.startswith("h")
                else 60 if unit.startswith("m") else 1)


def stamp(stats: KolStats, source: str) -> KolStats:
    stats.source, stats.updated_at = source, time.time()
    return stats
