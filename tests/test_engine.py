"""Scenario tests — no network. Everything external is faked."""
import asyncio
import time
from pathlib import Path

import pytest

from ctmc import config
from ctmc.data.market import aggregate_pairs
from ctmc.execution.broker import PaperBroker, max_size_for_impact
from ctmc.feeds.twitter import CelebrityFilter, Tweet, extract_coins
from ctmc.models import PairSnapshot, Position, Verdict
from ctmc.safety.checker import SafetyChecker
from ctmc.safety.clusters import ClusterAnalyzer
from ctmc.signals.hype import HypeChecker
from ctmc.strategy.engine import Trader
from ctmc.strategy.ladder import ladder_sells

CFG = config.load(Path(__file__).parents[1] / "config.yaml")
ELON = "44196397"
MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
OLD = 1_600_000_000_000
SOL = CFG["leaders"]["SOL"]


def run(coro):
    return asyncio.run(coro)


def snap(price=1.0, *, liq=2_000_000, vol_h1=600_000, vol_m5=50_000, buys=50, sells=50, h1=0.0, m5=0.0, token=MINT):
    return PairSnapshot(token, "MEME", "pair", "raydium", price, liq, 5_000_000, vol_h1, vol_m5,
                        m5, h1, buys, sells, 600, 600, OLD, pools={"raydium": liq * 0.7, "meteora": liq * 0.3})


class FakeMarket:
    def __init__(self, s=None, tickers=None):
        self.s = s or snap()
        self.tickers = tickers or {}

    async def snapshot(self, mint):
        return self.s

    async def search_ticker(self, ticker, min_liq):
        return self.tickers.get(ticker)


class FakeSafety:
    def __init__(self, ok=True, delay=0.0):
        self.ok, self.delay = ok, delay

    async def check(self, mint):
        await asyncio.sleep(self.delay)
        return Verdict(self.ok, [] if self.ok else ["mint authority NOT revoked"])


def trader(market=None, safety=None, cash=1000):
    ex = dict(CFG["execution"], paper_fee_pct=0)
    return Trader(CFG, PaperBroker(cash, ex), market or FakeMarket(), safety or FakeSafety())


def elon_tweet(text=f"this is going to be huge pump.fun/coin/{MINT}", **kw):
    return Tweet(id=str(time.time_ns()), author_id=ELON, text=text, created_at=time.time(), **kw)


# ── tweet parsing & who counts ────────────────────────────────
def test_extracts_addresses_from_links_and_text_and_tickers():
    c = extract_coins(f"gm $DOGE and $SOL https://dexscreener.com/solana/{MINT} also {MINT}")
    assert c.mints == [MINT]
    assert c.tickers == ["DOGE"]                       # SOL is never a target
    assert not extract_coins("just a normal tweet about rockets")


def test_only_real_celebrity_ids_fresh_original_posts():
    f = CelebrityFilter(CFG["celebrities"], CFG["twitter"])
    assert f.accept(elon_tweet())[0]
    impostor = Tweet(id="1", author_id="999", text="x", created_at=time.time())
    assert not f.accept(impostor)[0]                   # a fake @elonmusk has a different ID
    assert not f.accept(elon_tweet(is_reply=True))[0]
    stale = Tweet(id="2", author_id=ELON, text="x", created_at=time.time() - 300)
    assert not f.accept(stale)[0]


# ── entries ──────────────────────────────────────────────────
def test_celebrity_tweet_buys_instantly():
    t = trader()
    run(t.on_tweet(elon_tweet()))
    assert MINT in t.positions
    buy = [e for e in t.events if e.kind == "BUY"][0]
    assert "@elonmusk tweeted it" in buy.detail and "after the tweet" in buy.detail
    assert t.positions[MINT].cost_usd == pytest.approx(100)


def test_unknown_author_never_buys():
    t = trader()
    run(t.on_tweet(Tweet(id="1", author_id="123", text=MINT, created_at=time.time())))
    assert not t.positions and not t.events


def test_same_tweet_is_only_traded_once():
    t = trader()
    tw = elon_tweet()
    run(t.on_tweet(tw))
    run(t.on_tweet(tw))
    assert len([e for e in t.events if e.kind == "BUY"]) == 1


def test_failed_safety_blocks_buy():
    t = trader(safety=FakeSafety(ok=False))
    run(t.on_tweet(elon_tweet()))
    assert not t.positions
    assert "mint authority" in t.events[-1].detail


def test_slow_checks_mean_no_trade():
    t = trader(safety=FakeSafety(delay=CFG["checks"]["timeout_seconds"] + 1))
    t.cfg = dict(CFG, checks={"timeout_seconds": 0.05})
    run(t.on_tweet(elon_tweet()))
    assert not t.positions and "took over" in t.events[-1].detail


def test_ticker_without_address_needs_one_dominant_coin():
    t = trader(market=FakeMarket(tickers={}))
    run(t.on_tweet(elon_tweet("buy $FROG")))
    assert not t.positions and "ambiguous" in t.events[-1].detail
    t2 = trader(market=FakeMarket(tickers={"FROG": snap()}))
    run(t2.on_tweet(elon_tweet("buy $FROG")))
    assert MINT in t2.positions


def test_buy_shrinks_to_fit_price_impact_cap():
    t = trader(market=FakeMarket(snap(liq=4000)), cash=100_000)
    t.cfg = dict(CFG, universe=dict(CFG["universe"], min_liquidity_usd=0, min_volume_1h_usd=0))
    run(t.on_tweet(elon_tweet()))
    cap = max_size_for_impact(4000, CFG["execution"]["max_price_impact_pct"])
    assert t.positions[MINT].cost_usd == pytest.approx(cap)
    assert "impact 3.00%" in [e for e in t.events if e.kind == "BUY"][0].detail


# ── exit ladder ──────────────────────────────────────────────
def walk(prices):
    t = trader(market=FakeMarket(snap(liq=1e12)))   # bottomless pool: no price impact, exact math
    run(t.on_tweet(elon_tweet()))
    for px in prices:
        run(t.on_price(MINT, px))
    return t


def test_ladder_2x_then_halves_at_10_30_50_100_and_beyond():
    t = walk([2.001])
    p = t.positions[MINT]
    assert p.initial_taken and p.qty == pytest.approx(50, rel=1e-3)    # stake back, 50 free tokens left
    assert t.broker.cash == pytest.approx(1000, rel=1e-6)
    for px, left in [(10.01, 25), (30.01, 12.5), (50.01, 6.25), (100.01, 3.125), (200.01, 1.5625)]:
        run(t.on_price(MINT, px))
        assert t.positions[MINT].qty == pytest.approx(left, rel=1e-3), px
    assert t.positions[MINT].ladder_hits == [10, 30, 50, 100, 200]


def test_gap_straight_to_35x_hits_every_rung_passed():
    p = Position(MINT, "MEME", 1.0, 100, 100)
    sells = ladder_sells(p, 35.0, CFG["exits"])
    assert [s.initial for s in sells] == [True, False, False]
    assert [s.rung for s in sells] == [None, 10, 30]


def test_halving_ends_with_nothing_left():
    t = walk([2.001])
    px = 10
    while MINT in t.positions:
        run(t.on_price(MINT, px))
        px *= 2
    assert px / 2 < 2000                              # closed at the first tick past 1000x
    assert "final exit" in [e for e in t.events if e.kind == "SELL"][-2].detail


# ── exits on flow / risk ─────────────────────────────────────
def test_volume_death_sells_everything():
    t = walk([2.001, 3.0])
    dead = snap(3.0, vol_m5=1_000)
    for _ in range(CFG["exits"]["volume_death_checks"]):
        run(t.on_metrics(dead))
    assert MINT not in t.positions and "volume died" in t.events[-1].detail


def test_buys_dry_up_and_heavy_sells_exit():
    t = walk([1.4])
    run(t.on_metrics(snap(1.4, buys=10, sells=60, vol_m5=120_000)))
    assert MINT not in t.positions and "heavy sells" in t.events[-1].detail


def test_leader_crash_front_runs_exit():
    t = trader()
    run(t.on_price(SOL, 200))
    run(t.on_tweet(elon_tweet()))
    run(t.on_price(SOL, 190))           # SOL -5% inside the window
    assert MINT not in t.positions and "leader SOL" in t.events[-1].detail


def test_hard_stop_before_2x():
    t = walk([0.69])
    assert MINT not in t.positions and "hard stop" in t.events[-1].detail


def test_crash_backstop_on_runner_only():
    t = walk([2.001, 4.0, 2.5])           # -37.5% from peak: runner keeps riding
    assert MINT in t.positions
    run(t.on_price(MINT, 1.9))          # -52.5%: backstop
    assert MINT not in t.positions


# ── data & safety pieces ─────────────────────────────────────
def test_pools_summed_across_every_dex():
    def pair(dex, liq, vol):
        return {"chainId": "solana", "dexId": dex, "priceUsd": "1", "liquidity": {"usd": liq},
                "volume": {"h1": vol, "m5": vol / 12, "h24": vol * 24},
                "txns": {"m5": {"buys": 5, "sells": 3}}, "baseToken": {"address": MINT, "symbol": "MEME"}}
    s = aggregate_pairs([pair("raydium", 300_000, 60_000), pair("meteora", 100_000, 40_000),
                         pair("orca", 50_000, 10_000)])
    assert s.liquidity_usd == 450_000 and s.volume_h1 == 110_000
    assert s.buys_m5 == 15 and set(s.pools) == {"raydium", "meteora", "orca"}
    assert s.dex == "raydium"


class FakeRpc:
    """wallet -> funder, via one fake signature + transaction each."""

    def __init__(self, funders):
        self.f = funders

    async def call(self, method, params):
        w = params[0]
        if method == "getSignaturesForAddress":
            return [{"signature": f"sig-{w}"}]
        w = params[0][4:]
        return {"transaction": {"message": {"instructions": [
            {"parsed": {"type": "transfer", "info": {"source": self.f.get(w, "lonely"), "destination": w}}}]}}}


def test_bubble_map_catches_same_funder_and_dev_links():
    holders = [{"owner": f"w{i}", "pct": 5} for i in range(4)] + [{"owner": "solo", "pct": 3}]
    rpc = FakeRpc({"w0": "BOSS", "w1": "BOSS", "w2": "BOSS", "w3": "DEV"})
    v = run(ClusterAnalyzer(CFG["clusters"], rpc).analyze(holders, creator="DEV", pools=set()))
    assert not v.ok
    text = " ".join(v.reasons)
    assert "bubble holds 15.0%" in text and "dev-linked wallets hold 5.0%" not in text  # 5% == limit
    exchange_funded = {f"w{i}": "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9" for i in range(4)}
    assert run(ClusterAnalyzer(CFG["clusters"], FakeRpc(exchange_funded)).analyze(holders, None, set())).ok


def test_rugcheck_rules():
    sc = SafetyChecker(CFG["safety"])
    bad = {"mintAuthority": "Dev111", "freezeAuthority": None, "score_normalised": 10,
           "markets": [{"pubkey": "POOL", "lp": {"lpLockedPct": 40}}],
           "topHolders": [{"owner": "POOL", "pct": 40}, {"owner": "w1", "pct": 9, "insider": True},
                          {"owner": "w2", "pct": 9, "insider": True}]}
    text = " ".join(sc.evaluate_solana(bad).reasons)
    for needle in ("mint authority", "LP locked", "single wallet", "bundled"):
        assert needle in text
    good = {"mintAuthority": None, "freezeAuthority": None, "score_normalised": 5,
            "markets": [{"pubkey": "POOL", "lp": {"lpLockedPct": 100}}],
            "topHolders": [{"owner": "POOL", "pct": 40}] + [{"owner": f"w{i}", "pct": 2} for i in range(10)]}
    assert sc.evaluate_solana(good).ok


def test_hype_rules():
    h = HypeChecker(CFG["hype"])
    assert not h.evaluate({"hype_score": 90, "organic": False, "credible_accounts": ["@a", "@b"]})
    assert h.evaluate({"hype_score": 80, "organic": True, "credible_accounts": ["@a", "@b"]})
