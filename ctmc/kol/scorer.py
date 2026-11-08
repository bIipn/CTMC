"""Score any wallet ourselves from its on-chain swap history (no third-party leaderboard).

Uses Helius' parsed transaction history (HELIUS_API_KEY; the free tier is enough).
Every swap becomes a per-token position, priced in SOL:
  spent     SOL out on buys          received  SOL in on sells
  closed    sold >= 90% of what was bought
  win       closed and received > spent
Stats: win rate, realized PnL, profit factor, median hold, flip rate (closed < 60 s),
rug rate (lost > 90%), tokens traded per week. SOL-denominated, so no price feed needed.
"""
from __future__ import annotations

import os
import statistics
import time
from dataclasses import dataclass, field

import httpx

from ctmc.kol.registry import KolStats, stamp

WSOL = "So11111111111111111111111111111111111111112"
QUOTES = {WSOL, "EPjFWJ5DtsqmT6PrCNTP9e3ArWn8GFbQiZgYMg9oWtpc", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
HELIUS = "https://api.helius.xyz/v0/addresses/{addr}/transactions"


@dataclass
class _Pos:
    bought: float = 0.0
    sold: float = 0.0
    spent_sol: float = 0.0
    got_sol: float = 0.0
    first_buy: float = 0.0
    last_sell: float = 0.0
    trades: int = 0


@dataclass
class WalletReport:
    stats: KolStats
    positions: int = 0
    open_positions: int = 0
    best_roi: float = 0.0
    worst_roi: float = 0.0
    sol_price_usd: float = 0.0
    notes: list[str] = field(default_factory=list)


def swap_deltas(tx: dict, wallet: str) -> tuple[float, dict[str, float]]:
    """(SOL delta incl. wrapped SOL, {mint: token delta}) for `wallet` in one parsed tx."""
    sol = 0.0
    for t in tx.get("nativeTransfers") or []:
        amt = float(t.get("amount") or 0) / 1e9
        if t.get("toUserAccount") == wallet:
            sol += amt
        if t.get("fromUserAccount") == wallet:
            sol -= amt
    tokens: dict[str, float] = {}
    for t in tx.get("tokenTransfers") or []:
        mint, amt = t.get("mint"), float(t.get("tokenAmount") or 0)
        sign = (1 if t.get("toUserAccount") == wallet else 0) - (1 if t.get("fromUserAccount") == wallet else 0)
        if not mint or not sign:
            continue
        if mint == WSOL:
            sol += sign * amt
        else:
            tokens[mint] = tokens.get(mint, 0.0) + sign * amt
    return sol, tokens


def score_history(txs: list[dict], wallet: str, sol_price_usd: float = 0.0) -> WalletReport:
    """Pure: parsed swap transactions (any order) -> wallet stats."""
    book: dict[str, _Pos] = {}
    first_ts, last_ts = float("inf"), 0.0
    for tx in sorted(txs, key=lambda t: t.get("timestamp") or 0):
        ts = float(tx.get("timestamp") or 0)
        sol, tokens = swap_deltas(tx, wallet)
        moved = {m: d for m, d in tokens.items() if m not in QUOTES and abs(d) > 0}
        if not moved or sol == 0:
            continue
        first_ts, last_ts = min(first_ts, ts), max(last_ts, ts)
        share = 1 / len(moved)                       # multi-token swaps: split the SOL leg evenly
        for mint, d in moved.items():
            p = book.setdefault(mint, _Pos())
            p.trades += 1
            if d > 0 and sol < 0:                    # bought tokens with SOL
                p.bought += d
                p.spent_sol += -sol * share
                p.first_buy = p.first_buy or ts
            elif d < 0 and sol > 0:                  # sold tokens for SOL
                p.sold += -d
                p.got_sol += sol * share
                p.last_sell = ts

    closed = [p for p in book.values() if p.bought > 0 and p.sold >= 0.9 * p.bought and p.spent_sol > 0]
    pnl = [p.got_sol - p.spent_sol for p in closed]
    rois = [p.got_sol / p.spent_sol - 1 for p in closed]
    holds = [p.last_sell - p.first_buy for p in closed if p.last_sell >= p.first_buy]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    days = max((last_ts - first_ts) / 86400, 1 / 24) if last_ts else 1
    spent = [p.spent_sol for p in book.values() if p.spent_sol > 0]

    s = KolStats()
    s.closed_trades = len(closed)
    s.win_rate = round(100 * len(wins) / len(closed), 1) if closed else 0.0
    s.profit_factor = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else (99.0 if wins else 0.0)
    s.median_hold_s = statistics.median(holds) if holds else 0.0
    s.flip_rate = round(sum(1 for h in holds if h < 60) / len(holds), 3) if holds else 0.0
    s.rug_rate = round(sum(1 for r in rois if r < -0.9) / len(rois), 3) if rois else 0.0
    s.tokens_7d = round(len(book) * min(1.0, 7 / days))
    s.trades_7d = round(sum(p.trades for p in book.values()) * min(1.0, 7 / days))
    usd = sol_price_usd or 0.0
    s.pnl_30d_usd = round(sum(pnl) * usd, 2) if usd else round(sum(pnl), 4)   # SOL if no price given
    s.avg_position_usd = round(statistics.mean(spent) * usd, 2) if spent and usd else 0.0
    rep = WalletReport(stamp(s, "onchain"), positions=len(book),
                       open_positions=sum(1 for p in book.values() if p.bought > 0 and p.sold < 0.9 * p.bought),
                       best_roi=max(rois, default=0.0), worst_roi=min(rois, default=0.0), sol_price_usd=usd)
    if not usd:
        rep.notes.append("PnL in SOL (no SOL price given)")
    if days < 7:
        rep.notes.append(f"only {days:.1f} days of history")
    return rep


async def fetch_swaps(wallet: str, max_pages: int = 10, http: httpx.AsyncClient | None = None) -> list[dict]:
    """Up to max_pages x 100 of the wallet's most recent swaps from Helius."""
    key = os.getenv("HELIUS_API_KEY", "")
    if not key:
        raise RuntimeError("HELIUS_API_KEY not set — needed to score wallets on-chain")
    own = http is None
    http = http or httpx.AsyncClient(timeout=30)
    out: list[dict] = []
    before = None
    try:
        for _ in range(max_pages):
            params = {"api-key": key, "type": "SWAP", "limit": 100}
            if before:
                params["before"] = before
            r = await http.get(HELIUS.format(addr=wallet), params=params)
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            out += page
            before = page[-1].get("signature")
            if len(page) < 100 or time.time() - float(page[-1].get("timestamp") or 0) > 30 * 86400:
                break                                # past 30 days: enough history
    finally:
        if own:
            await http.aclose()
    return out
