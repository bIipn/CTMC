"""Scenario tests — the strategy behaves the way the plan says, no network needed."""
from pathlib import Path

import pytest

from ctmc import config
from ctmc.execution.broker import PaperBroker
from ctmc.models import PairSnapshot, Verdict
from ctmc.safety.checker import SafetyChecker
from ctmc.signals.hype import HypeChecker
from ctmc.strategy.engine import Engine

CFG = config.load(Path(__file__).parents[1] / "config.yaml")
OLD = 1_600_000_000_000  # pair launched in 2020 -> passes the age filter


class Pass:
    def check(self, *a):
        return Verdict(True)


def snap(price, *, h1=0.0, m5=0.0, buys=50, sells=50, vol_h1=600_000, vol_m5=50_000, token="MEME"):
    return PairSnapshot(token, token, "pair", "raydium", price, 400_000, 5_000_000, vol_h1, vol_m5,
                        m5, h1, buys, sells, 600, 600, OLD)


def engine():
    return Engine(CFG, broker=PaperBroker(1000, slippage_pct=0, fee_pct=0), safety=Pass(), hype=Pass())


def calm_leaders():
    return {"SOL": 200.0, "WIF": 2.0}


def flush_then_buy(e):
    e.tick(calm_leaders(), [snap(1.0, h1=-20, buys=20, sells=80)])   # panic flush
    assert not e.positions, "must not catch the falling knife"
    e.tick(calm_leaders(), [snap(1.0, m5=1, buys=60, sells=50)])     # sellers exhausted
    assert "MEME" in e.positions


def test_buys_only_after_flush_exhausts():
    e = engine()
    e.tick(calm_leaders(), [snap(1.0, m5=1, buys=60, sells=50)])
    assert not e.positions, "no flush, no entry"
    flush_then_buy(e)
    assert e.positions["MEME"].cost_usd == pytest.approx(100)  # 10% of equity


def test_takes_initial_at_2x_and_keeps_runner():
    e = engine()
    flush_then_buy(e)
    e.tick(calm_leaders(), [snap(2.0)])
    p = e.positions["MEME"]
    assert p.initial_taken
    assert p.qty == pytest.approx(50)            # half sold -> cost recovered
    assert e.broker.cash == pytest.approx(1000)  # whole stake back in cash


def test_runner_trailing_stop():
    e = engine()
    flush_then_buy(e)
    e.tick(calm_leaders(), [snap(2.0)])
    e.tick(calm_leaders(), [snap(4.0)])
    e.tick(calm_leaders(), [snap(2.9)])          # -27.5% from peak
    assert not e.positions
    assert e.events[-1].pnl_usd == pytest.approx(100 + 145 - 100)


def test_exit_when_buys_dry_up_and_heavy_sells():
    e = engine()
    flush_then_buy(e)
    e.tick(calm_leaders(), [snap(1.3, buys=10, sells=60, vol_m5=120_000)])
    assert not e.positions
    assert "distribution" in e.events[-1].detail


def test_leader_dump_front_runs_exit():
    e = engine()
    flush_then_buy(e)
    e.tick({"SOL": 190.0, "WIF": 2.0}, [snap(1.05)])   # SOL -5% in window
    assert not e.positions
    assert "leader SOL" in e.events[-1].detail


def test_failed_safety_blocks_entry():
    class Fail:
        def check(self, *a):
            return Verdict(False, ["mint authority NOT revoked"])
    e = Engine(CFG, broker=PaperBroker(1000), safety=Fail(), hype=Pass())
    e.tick(calm_leaders(), [snap(1.0, h1=-20, buys=20, sells=80)])
    e.tick(calm_leaders(), [snap(1.0, m5=1, buys=60, sells=50)])
    assert not e.positions
    assert e.events[-1].kind == "SKIP"


def test_rugcheck_evaluation_catches_bundles_and_authorities():
    sc = SafetyChecker(CFG["safety"])
    bad = {
        "mintAuthority": "Dev111", "freezeAuthority": None, "score_normalised": 10,
        "markets": [{"pubkey": "POOL", "lp": {"lpLockedPct": 40}}],
        "topHolders": [{"owner": "POOL", "pct": 40}, {"owner": "w1", "pct": 9, "insider": True},
                       {"owner": "w2", "pct": 9, "insider": True}],
    }
    v = sc.evaluate_solana(bad)
    text = " ".join(v.reasons)
    assert not v.ok
    for needle in ("mint authority", "LP locked", "single wallet", "bundled"):
        assert needle in text
    good = {"mintAuthority": None, "freezeAuthority": None, "score_normalised": 5,
            "markets": [{"pubkey": "POOL", "lp": {"lpLockedPct": 100}}],
            "topHolders": [{"owner": "POOL", "pct": 40}] + [{"owner": f"w{i}", "pct": 2} for i in range(10)]}
    assert sc.evaluate_solana(good).ok


def test_hype_needs_organic_and_credible_names():
    h = HypeChecker(CFG["hype"])
    assert not h.evaluate({"hype_score": 90, "organic": False, "credible_accounts": ["@a", "@b"]})
    assert not h.evaluate({"hype_score": 90, "organic": True, "credible_accounts": ["@a"]})
    assert h.evaluate({"hype_score": 80, "organic": True, "credible_accounts": ["@a", "@b"]})
