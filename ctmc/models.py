"""Shared data shapes."""
from __future__ import annotations

from dataclasses import dataclass, field
from time import time


@dataclass
class PairSnapshot:
    """One point-in-time read of a DEX pair (from DexScreener)."""

    token: str                 # mint / contract address
    symbol: str
    pair_address: str
    dex: str
    price_usd: float
    liquidity_usd: float
    volume_24h: float
    volume_h1: float
    volume_m5: float
    change_m5: float           # percent
    change_h1: float
    buys_m5: int
    sells_m5: int
    buys_h1: int
    sells_h1: int
    created_at_ms: int
    ts: float = field(default_factory=time)
    pools: dict[str, float] = field(default_factory=dict)   # dex -> liquidity USD
    name: str = ""
    market_cap: float = 0.0
    holders: int = 0
    unique_wallets_1h: int = 0

    @property
    def age_hours(self) -> float:
        return (self.ts * 1000 - self.created_at_ms) / 3_600_000 if self.created_at_ms else 0.0

    @property
    def age_minutes(self) -> float:
        return self.age_hours * 60

    @property
    def sell_ratio_m5(self) -> float:
        """sells per buy over the last 5 minutes."""
        return self.sells_m5 / max(self.buys_m5, 1)

    @property
    def buy_ratio_m5(self) -> float:
        return self.buys_m5 / max(self.sells_m5, 1)


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    score: float | None = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class Position:
    token: str
    symbol: str
    entry_price: float
    qty: float
    cost_usd: float
    opened_at: float = field(default_factory=time)
    peak_price: float = 0.0
    initial_taken: bool = False   # True once the cost basis has been pulled out
    realized_usd: float = 0.0
    ladder_hits: list[float] = field(default_factory=list)   # multiples already sold at
    source: str = ""              # e.g. "@elonmusk tweet 1789..."
    profile: str = ""             # "early" | "established"
    signal: str = ""              # who/what triggered it: "@elonmusk", "narrative:@Cobratate", ...
    entry_latency_s: float | None = None   # tweet -> fill
    low_volume_streak: int = 0

    def __post_init__(self) -> None:
        self.peak_price = self.peak_price or self.entry_price

    def value(self, price: float) -> float:
        return self.qty * price

    def multiple(self, price: float) -> float:
        return price / self.entry_price
