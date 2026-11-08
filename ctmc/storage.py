"""Crash-safe state: positions, fills, journal, locks and equity — in SQLite.

Separate files for paper and live (data/paper.sqlite, data/live.sqlite), so a test
run can never pollute real history. Every write commits immediately (WAL mode),
so a crash or restart loses nothing: open positions come back with their ladder
progress, peak price and cost basis, and a fill is recorded as PENDING before a
live swap is sent so a restart can tell what happened to it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ctmc.models import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  token TEXT PRIMARY KEY, symbol TEXT, entry_price REAL, qty REAL, cost_usd REAL,
  opened_at REAL, peak_price REAL, initial_taken INTEGER, realized_usd REAL,
  ladder_hits TEXT, source TEXT, profile TEXT, signal TEXT, low_volume_streak INTEGER,
  status TEXT DEFAULT 'open', closed_at REAL, exit_reason TEXT, exit_code TEXT,
  pnl_usd REAL, peak_multiple REAL, exit_multiple REAL, hit_2x INTEGER, entry_latency_s REAL,
  fees_usd REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, token TEXT, symbol TEXT, side TEXT,
  qty REAL, usd REAL, price REAL, impact_pct REAL, signature TEXT, reason TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, symbol TEXT, detail TEXT, pnl REAL);
CREATE TABLE IF NOT EXISTS locks (key TEXT PRIMARY KEY, until REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS equity (ts REAL, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ticks (token TEXT, ts REAL, price REAL);
CREATE INDEX IF NOT EXISTS ix_ticks ON ticks(token, ts);
CREATE INDEX IF NOT EXISTS ix_pos_closed ON positions(status, closed_at);
CREATE INDEX IF NOT EXISTS ix_fills_ts ON fills(ts);
"""

# exit reason text -> short code (for attribution and protections)
EXIT_CODES = [
    ("RUG ALERT", "rug"), ("leader ", "leader"), ("hard stop", "hard_stop"), ("crash backstop", "crash_backstop"),
    ("profit floor", "profit_floor"), ("smart money", "smart_money"), ("volume died", "volume_death"),
    ("buys dried up", "distribution"), ("laddered", "ladder"), ("max hold", "max_hold"), ("manual", "manual"),
    ("reconcile", "reconcile"),
]
BAD_EXITS = {"rug", "leader", "hard_stop", "crash_backstop"}
# what the stop-out guard counts: anomalies, not the ordinary -30% stops a fat-tailed strategy expects
ANOMALY_EXITS = {"rug", "leader"}
QUICK_STOP_SECONDS = 120          # a stop hit this soon after buying = bought the top / got sandwiched


def exit_code(reason: str) -> str:
    for needle, code in EXIT_CODES:
        if needle.lower() in reason.lower():
            return code
    return "other"


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        try:                                              # older journals: add the fees column
            self.db.execute("ALTER TABLE positions ADD COLUMN fees_usd REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass

    @classmethod
    def for_mode(cls, mode: str, folder: str = "data") -> "Store":
        return cls(Path(folder) / f"{mode}.sqlite")

    # ── positions ────────────────────────────────────────────
    def save_position(self, p: Position) -> None:
        self.db.execute(
            """INSERT INTO positions (token, symbol, entry_price, qty, cost_usd, opened_at, peak_price, initial_taken,
                 realized_usd, ladder_hits, source, profile, signal, low_volume_streak, status, entry_latency_s, fees_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)
               ON CONFLICT(token) DO UPDATE SET symbol=excluded.symbol, entry_price=excluded.entry_price,
                 qty=excluded.qty, cost_usd=excluded.cost_usd, peak_price=excluded.peak_price,
                 initial_taken=excluded.initial_taken, realized_usd=excluded.realized_usd,
                 ladder_hits=excluded.ladder_hits, low_volume_streak=excluded.low_volume_streak, status='open',
                 fees_usd=excluded.fees_usd""",
            (p.token, p.symbol, p.entry_price, p.qty, p.cost_usd, p.opened_at, p.peak_price, int(p.initial_taken),
             p.realized_usd, json.dumps(p.ladder_hits), p.source, p.profile, getattr(p, "signal", ""),
             p.low_volume_streak, getattr(p, "entry_latency_s", None), getattr(p, "fees_usd", 0.0)))

    def close_position(self, p: Position, reason: str) -> None:
        peak = p.peak_price / p.entry_price if p.entry_price else 0
        fees = getattr(p, "fees_usd", 0.0)
        exit_mult = (p.realized_usd - fees) / p.cost_usd if p.cost_usd else 0          # net of fees
        self.db.execute(
            """UPDATE positions SET status='closed', closed_at=?, exit_reason=?, exit_code=?, pnl_usd=?, qty=0,
                 realized_usd=?, peak_price=?, peak_multiple=?, exit_multiple=?, hit_2x=?, ladder_hits=?,
                 initial_taken=?, fees_usd=? WHERE token=?""",
            (time.time(), reason, exit_code(reason), p.realized_usd - p.cost_usd - fees, p.realized_usd, p.peak_price,
             peak, exit_mult, int(p.initial_taken or peak >= 2), json.dumps(p.ladder_hits), int(p.initial_taken),
             fees, p.token))

    def open_positions(self) -> list[Position]:
        out = []
        for r in self.db.execute("SELECT * FROM positions WHERE status='open'"):
            p = Position(r["token"], r["symbol"], r["entry_price"], r["qty"], r["cost_usd"], opened_at=r["opened_at"],
                         peak_price=r["peak_price"], initial_taken=bool(r["initial_taken"]),
                         realized_usd=r["realized_usd"], ladder_hits=json.loads(r["ladder_hits"] or "[]"),
                         source=r["source"] or "", low_volume_streak=r["low_volume_streak"] or 0)
            p.profile, p.signal = r["profile"] or "", r["signal"] or ""
            p.fees_usd = r["fees_usd"] or 0.0
            out.append(p)
        return out

    def closed(self, since: float = 0.0) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM positions WHERE status='closed' AND closed_at >= ? ORDER BY closed_at", (since,)))

    # ── fills ────────────────────────────────────────────────
    def fill(self, token: str, symbol: str, side: str, qty: float, usd: float, price: float,
             impact_pct: float = 0.0, signature: str = "", reason: str = "", status: str = "confirmed") -> int:
        cur = self.db.execute(
            "INSERT INTO fills (ts, token, symbol, side, qty, usd, price, impact_pct, signature, reason, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), token, symbol, side, qty, usd, price, impact_pct, signature, reason, status))
        return int(cur.lastrowid)

    def set_fill_status(self, fill_id: int, status: str, signature: str = "") -> None:
        self.db.execute("UPDATE fills SET status=?, signature=COALESCE(NULLIF(?, ''), signature) WHERE id=?",
                        (status, signature, fill_id))

    def pending_fills(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM fills WHERE status='pending'"))

    def buys_since(self, since: float, signal: str | None = None) -> int:
        if signal is None:
            q = "SELECT COUNT(*) FROM positions WHERE opened_at >= ?"
            return self.db.execute(q, (since,)).fetchone()[0]
        q = "SELECT COUNT(*) FROM positions WHERE opened_at >= ? AND signal = ?"
        return self.db.execute(q, (since, signal)).fetchone()[0]

    # ── journal, locks, equity, kv ───────────────────────────
    def event(self, ts: float, kind: str, symbol: str, detail: str, pnl: float = 0.0) -> None:
        self.db.execute("INSERT INTO journal (ts, kind, symbol, detail, pnl) VALUES (?,?,?,?,?)",
                        (ts, kind, symbol, detail, pnl))

    def lock(self, key: str, until: float, reason: str) -> None:
        self.db.execute("INSERT INTO locks (key, until, reason) VALUES (?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET until=MAX(until, excluded.until), reason=excluded.reason",
                        (key, until, reason))

    def active_lock(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM locks WHERE key=? AND until > ?", (key, time.time())).fetchone()

    def locks(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM locks WHERE until > ? ORDER BY until", (time.time(),)))

    def unlock(self, key: str | None = None) -> None:
        if key:
            self.db.execute("DELETE FROM locks WHERE key=?", (key,))
        else:
            self.db.execute("DELETE FROM locks")

    def snapshot_equity(self, equity: float, cash: float) -> None:
        self.db.execute("INSERT INTO equity (ts, equity, cash) VALUES (?,?,?)", (time.time(), equity, cash))

    def equity_curve(self, since: float = 0.0) -> list[tuple[float, float]]:
        return [(r[0], r[1]) for r in self.db.execute("SELECT ts, equity FROM equity WHERE ts >= ? ORDER BY ts", (since,))]

    def tick(self, token: str, price: float) -> None:
        self.db.execute("INSERT INTO ticks (token, ts, price) VALUES (?,?,?)", (token, time.time(), price))

    def path(self, token: str, start: float, end: float) -> list[tuple[float, float]]:
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT ts, price FROM ticks WHERE token=? AND ts BETWEEN ? AND ? ORDER BY ts", (token, start, end))]

    def get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))
