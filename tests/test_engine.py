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
        self.last_report = {}

    async def report(self, mint):
        await asyncio.sleep(self.delay)
        return {"creator": None}

    def evaluate_solana(self, rep, overrides=None):
        self.overrides = overrides
        return Verdict(self.ok, [] if self.ok else ["mint authority NOT revoked"])

    async def clusters_check(self, rep):
        return Verdict(True, [])


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


# ── deeper Solana safety ─────────────────────────────────────
def _clean_report(**over):
    rep = {"mintAuthority": None, "freezeAuthority": None, "score_normalised": 5, "totalHolders": 5000,
           "markets": [{"pubkey": "POOL", "lp": {"lpLockedPct": 100}}],
           "topHolders": [{"owner": "POOL", "pct": 40}] + [{"owner": f"w{i}", "pct": 2} for i in range(10)]}
    rep.update(over)
    return rep


@pytest.mark.parametrize("over, needle", [
    ({"transferFee": {"pct": 5}}, "transfer fee"),
    ({"token_extensions": {"permanentDelegate": {"delegate": "X"}}}, "permanent delegate"),
    ({"token_extensions": {"transferHook": {"programId": "Y"}}}, "transfer hook"),
    ({"creator": "DEV", "topHolders": [{"owner": "DEV", "pct": 7}]}, "dev wallet still holds 7.0%"),
    ({"creatorTokens": [{"mint": str(i)} for i in range(25)]}, "serial launcher"),
    ({"totalHolders": 40}, "only 40 holders"),
])
def test_solana_traps_are_rejected(over, needle):
    v = SafetyChecker(CFG["safety"]).evaluate_solana(_clean_report(**over))
    assert not v.ok and needle in " ".join(v.reasons)


def test_clean_token_passes_and_mutable_metadata_is_only_a_note():
    v = SafetyChecker(CFG["safety"]).evaluate_solana(_clean_report(tokenMeta={"mutable": True}))
    assert v.ok and "mutable" in v.reasons[0]


def test_labelled_pools_excluded_but_random_addresses_still_counted():
    # an ordinary wallet whose address happens to contain "lp" / "pump" must still be counted
    holders = [{"owner": "Ab3lpQ9pumpZ", "pct": 12}, {"owner": "LOCKERADDR", "pct": 50}]
    rep = _clean_report(topHolders=holders, knownAccounts={"LOCKERADDR": {"name": "Streamflow", "type": "LOCKER"}})
    v = SafetyChecker(CFG["safety"]).evaluate_solana(rep)
    assert not v.ok and "single wallet holds 12.0%" in " ".join(v.reasons)
    assert "50.0%" not in " ".join(v.reasons)


def test_market_cap_far_above_liquidity_is_rejected():
    s = snap(liq=100_000)
    s.market_cap = 10_000_000          # 100x its liquidity
    v = trader().passes_universe(s)
    assert not v.ok and "100x its liquidity" in " ".join(v.reasons)


# ── post-buy rug watch ───────────────────────────────────────
from ctmc.safety.rugwatch import RugWatch, liquidity_removed  # noqa: E402


def test_liquidity_math_separates_price_moves_from_lp_pulls():
    # price quarters, liquidity halves -> exactly what a constant-product pool does: nothing removed
    assert liquidity_removed((1_000_000, 1.0), (500_000, 0.25)) == pytest.approx(0)
    # price flat, liquidity -70% -> 70% removed
    assert liquidity_removed((1_000_000, 1.0), (300_000, 1.0)) == pytest.approx(0.7)


def _watch(alerts):
    async def on_alert(mint, why):
        alerts.append(why)
    return RugWatch(CFG["rugwatch"], on_alert)


def test_lp_pull_fires_but_a_plain_crash_does_not():
    alerts = []
    w = _watch(alerts)

    async def go():
        await w.start(MINT, None)
        await w.on_metrics(snap(1.0, liq=1_000_000))
        await w.on_metrics(snap(0.25, liq=500_000))      # crash, LP intact
        assert not alerts
        await w.on_metrics(snap(0.25, liq=100_000))      # LP yanked
    run(go())
    assert alerts and "liquidity pulled" in alerts[0]


def _report():
    return {"creator": "DEV", "token": {"supply": 1_000_000_000 * 10**6, "decimals": 6},
            "topHolders": [{"owner": f"whale{i}", "address": f"acct{i}", "pct": 3, "uiAmount": 30_000_000}
                           for i in range(3)]}


def test_dev_selling_fires_instantly():
    alerts = []
    w = _watch(alerts)

    async def fake_accounts(owner, mint):
        return {"devacct": 50_000_000.0}                  # dev holds 5% of supply
    w._token_accounts = fake_accounts

    async def go():
        await w.start(MINT, _report())
        await w.on_balance("devacct", 49_000_000)         # 2% of the bag: below trigger
        assert not alerts
        await w.on_balance("devacct", 40_000_000)         # 20% of the bag
    run(go())
    assert alerts and "DEV IS SELLING" in alerts[0]


def test_whales_dumping_fires():
    alerts = []
    w = _watch(alerts)

    async def no_dev(owner, mint):
        return {}
    w._token_accounts = no_dev

    async def go():
        await w.start(MINT, _report())
        for i in range(3):                                # each sells 60% of 3% of supply = 5.4% total
            await w.on_balance(f"acct{i}", 12_000_000)
    run(go())
    assert alerts and "3 top holders sold 5.4% of supply" in alerts[0]


def test_websocket_messages_drive_balance_updates():
    alerts = []
    w = _watch(alerts)

    async def fake_accounts(owner, mint):
        return {"devacct": 50_000_000.0}
    w._token_accounts = fake_accounts

    async def go():
        await w.start(MINT, _report())
        w._req[7] = "devacct"
        await w.handle_message({"jsonrpc": "2.0", "id": 7, "result": 4242})          # subscribed
        await w.handle_message({"method": "accountNotification", "params": {
            "subscription": 4242, "result": {"value": {"lamports": 0, "data": ["", "base64"]}}}})  # account closed
    run(go())
    assert alerts and "DEV IS SELLING" in alerts[0] and "100%" in alerts[0]


def test_rug_alert_dumps_the_position():
    t = trader()
    run(t.on_tweet(elon_tweet()))
    run(t.on_rug_alert(MINT, "liquidity pulled: 80% removed in 3s"))
    assert MINT not in t.positions and "RUG ALERT" in t.events[-1].detail


# ── smart money: KOL registry, scoring, tracking, decoding ───
from ctmc.feeds.pumpportal import NewToken, parse_message  # noqa: E402
from ctmc.kol.importer import parse_kol_page, parse_kol_slugs  # noqa: E402
from ctmc.kol.registry import Kol, KolStats, Registry, parse_duration, parse_money  # noqa: E402
from ctmc.kol.scorer import score_history  # noqa: E402
from ctmc.kol.signals import SmartMoney  # noqa: E402
from ctmc.kol.tracker import KolTrade, KolTracker, parse_wallet_trades  # noqa: E402
from ctmc.signals.narrative import Narrative  # noqa: E402

KR = CFG["kol"]
FIXTURES = Path(__file__).parent / "fixtures"


def kol(name, wr=65, pnl=80_000, hold=1800, trades=300, role="kol", wallet=None):
    return Kol(name, [wallet or f"W{name}"], role=role,
               stats=KolStats(win_rate=wr, pnl_30d_usd=pnl, median_hold_s=hold, trades_7d=trades, closed_trades=120))


def test_parsers_for_money_and_durations():
    assert parse_money("+$72.3K") == 72_300 and parse_money("$-4,587") == -4587 and parse_money("$1.4M") == 1_400_000
    assert parse_duration("16 s") == 16 and parse_duration("12 min") == 720 and parse_duration("4.2 h") == 15_120


def test_kol_tiers_reward_results_and_followability_not_fame():
    swing = kol("Swing", wr=68, pnl=150_000, hold=2 * 3600)
    scalper = kol("Scalper", wr=72, pnl=380_000, hold=16, trades=21_000)      # great trader, 16 s holds
    famous_loser = kol("Famous", wr=39, pnl=-57_000, hold=4 * 3600)           # big name, losing money
    celeb = kol("Celeb", wr=0, pnl=0, hold=0, role="celebrity")
    assert swing.tier(KR) == "A" and swing.weight(KR) > 0.5
    assert scalper.tier(KR) == "unfollowable" and scalper.weight(KR) == 0
    assert famous_loser.tier(KR) == "no edge" and famous_loser.weight(KR) == 0
    assert celeb.tier(KR) == "CELEB" and celeb.weight(KR) == 1.0


def test_importer_reads_wallet_and_stats_from_kol_page():
    k = parse_kol_page((FIXTURES / "kolexplorer_kol.html").read_text(), "testkol")
    assert k.wallets == ["TestWa11etAddress99999999999999999999999999"] and k.x_handle == "testkol"
    assert k.stats.win_rate == 67.0 and k.stats.median_hold_s == 16 and k.stats.trades_7d == 21630
    assert k.stats.pnl_30d_usd == 381_176 and k.stats.median_entry_mcap_usd == 8_500
    assert k.tier(KR) == "unfollowable"
    assert parse_kol_slugs('<a href="https://kolexplorer.com/kol/abc">x</a> kolexplorer.com/kol/d-e') == ["abc", "d-e"]


def test_registry_roundtrip_and_merge(tmp_path):
    reg = Registry(tmp_path / "kols.json")
    reg.add(kol("Swing", wallet="W1"))
    reg.add(Kol("swing", ["W2"], x_handle="swingx"))         # same KOL, second wallet
    reg.save()
    again = Registry(tmp_path / "kols.json").load()
    k = again.kols["swing"]
    assert k.wallets == ["W1", "W2"] and k.x_handle == "swingx" and again.by_wallet["W2"] is k


def _helius_swap(ts, wallet, mint, tokens, sol):
    """tokens > 0 = bought with `sol` SOL; tokens < 0 = sold for `sol` SOL."""
    pool = "POOLACCOUNT"
    if tokens > 0:
        return {"timestamp": ts, "tokenTransfers": [{"mint": mint, "tokenAmount": tokens, "fromUserAccount": pool, "toUserAccount": wallet}],
                "nativeTransfers": [{"amount": sol * 1e9, "fromUserAccount": wallet, "toUserAccount": pool}]}
    return {"timestamp": ts, "tokenTransfers": [{"mint": mint, "tokenAmount": -tokens, "fromUserAccount": wallet, "toUserAccount": pool}],
            "nativeTransfers": [{"amount": sol * 1e9, "fromUserAccount": pool, "toUserAccount": wallet}]}


def test_onchain_scoring_win_rate_hold_and_flips():
    W, now = "ME", 1_800_000_000
    txs = [
        _helius_swap(now, W, "A", 1000, 1.0), _helius_swap(now + 3600, W, "A", -1000, 3.0),      # +2 SOL, 1h hold
        _helius_swap(now, W, "B", 500, 2.0), _helius_swap(now + 30, W, "B", -500, 1.0),          # -1 SOL, 30s flip
        _helius_swap(now, W, "C", 800, 1.0), _helius_swap(now + 7200, W, "C", -800, 0.05),       # -95%: rug
        _helius_swap(now, W, "D", 100, 1.0),                                                     # still open
    ]
    rep = score_history(txs, W, sol_price_usd=150)
    s = rep.stats
    assert s.closed_trades == 3 and s.win_rate == pytest.approx(33.3, abs=0.1)
    assert s.pnl_30d_usd == pytest.approx((2 - 1 - 0.95) * 150)
    assert s.profit_factor == pytest.approx(2 / 1.95, abs=0.01)
    assert s.median_hold_s == 3600 and s.flip_rate == pytest.approx(1 / 3, abs=0.01)
    assert s.rug_rate == pytest.approx(1 / 3, abs=0.01) and rep.open_positions == 1


def _rpc_tx(wallet, mint, pre_tok, post_tok, pre_lamports, post_lamports, fee=5000):
    return {"meta": {"err": None, "fee": fee, "preBalances": [pre_lamports], "postBalances": [post_lamports],
                     "preTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"uiAmount": pre_tok}}],
                     "postTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"uiAmount": post_tok}}]},
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet}]}}}


def test_live_trade_parser_buys_sells_and_ignores_transfers():
    buy = parse_wallet_trades(_rpc_tx("K", MINT, 0, 5000, 3e9, 2e9), "K")
    assert buy == [{"mint": MINT, "side": "buy", "tokens": 5000, "sol": pytest.approx(0.999995), "before": 0, "after": 5000}]
    sell = parse_wallet_trades(_rpc_tx("K", MINT, 5000, 1000, 2e9, 4e9), "K")
    assert sell[0]["side"] == "sell" and sell[0]["tokens"] == 4000 and sell[0]["before"] == 5000
    airdrop = parse_wallet_trades(_rpc_tx("K", MINT, 0, 5000, 3e9, 3e9 - 5000), "K")   # tokens in, no SOL out
    assert airdrop == []


def _smart(*kols, holds=None):
    reg = Registry("/nonexistent/kols.json")
    for k in kols:
        reg.add(k)

    async def on_trade(t):
        pass
    tr = KolTracker(reg, KR, on_trade)

    async def fake_balances(mint, wallets):
        for w in wallets:
            if (holds or {}).get(w):
                tr.holdings[mint][w] = holds[w]
        return {}
    tr.balances_of = fake_balances
    return SmartMoney(reg, tr, KR)


def _sell(sm, k, frac, held_s=600):
    w = k.wallets[0]
    sm.tracker.first_buy[(w, MINT)] = time.time() - held_s
    before = 1000.0
    return KolTrade(w, k, MINT, "sell", before * frac, 1.0, before, before * (1 - frac), "sig")


def test_decoding_kol_sells():
    a, b, scalper = kol("Alpha", hold=3600), kol("Beta", hold=3600), kol("Flip", hold=10, trades=20_000)
    celeb = kol("Celeb", role="celebrity", wr=0, pnl=0, hold=0)
    sm = _smart(a, b, scalper, celeb)
    sm.opened(MINT, run(sm.confluence(MINT, live_check=False)))
    sm.watch[MINT].holders.update({a.wallets[0]: a.weight(KR), b.wallets[0]: b.weight(KR)})

    assert sm.decode_sell(_sell(sm, scalper, 1.0)).action == "ignore"                 # bots' exits mean nothing
    assert sm.decode_sell(_sell(sm, a, 0.2, held_s=2400)).action == "ignore"          # small trim = profit taking
    call = sm.decode_sell(_sell(sm, a, 1.0, held_s=300))                              # all out in 5 min vs usual 60
    assert call.action == "trim" and "early" in call.reason
    assert sm.decode_sell(_sell(sm, b, 1.0, held_s=4000)).action == "exit"            # both proven wallets gone
    assert sm.decode_sell(_sell(sm, celeb, 0.3)).action == "urgent"


def test_confluence_sizes_up_celebrity_entries():
    a, b = kol("Alpha", hold=3600, wallet="WA"), kol("Beta", hold=3600, wallet="WB")
    sm = _smart(a, b, holds={"WA": 10_000, "WB": 5_000})
    t = trader()
    t.smart = sm
    run(t.on_tweet(elon_tweet()))
    buy = [e for e in t.events if e.kind == "BUY"][0]
    mult = sm.size_multiplier(run(sm.confluence(MINT)))
    assert mult > 1.5 and "2 smart wallets in" in buy.detail
    assert t.positions[MINT].cost_usd == pytest.approx(100 * mult)


def test_kol_sell_signals_trim_then_exit_a_held_position():
    a, b = kol("Alpha", hold=3600, wallet="WA"), kol("Beta", hold=3600, wallet="WB")
    sm = _smart(a, b, holds={"WA": 1000, "WB": 1000})
    t = trader(market=FakeMarket(snap(liq=1e12)))
    t.smart = sm
    run(t.on_tweet(elon_tweet()))
    qty0 = t.positions[MINT].qty
    run(t.on_kol_trade(_sell(sm, a, 1.0, held_s=120)))          # strong wallet bails very early
    assert t.positions[MINT].qty == pytest.approx(qty0 / 2)
    run(t.on_kol_trade(_sell(sm, b, 1.0, held_s=4000)))         # the rest of the smart money leaves
    assert MINT not in t.positions and "smart money" in t.events[-1].detail


def test_celebrity_wallet_buy_triggers_entry():
    celeb = kol("Tate", role="celebrity", wr=0, pnl=0, hold=0, wallet="WTATE")
    sm = _smart(celeb)
    t = trader()
    t.smart = sm
    run(t.on_kol_trade(KolTrade("WTATE", celeb, MINT, "buy", 1e6, 50, 0, 1e6, "sig")))
    assert MINT in t.positions and "Tate's own wallet bought it" in t.positions[MINT].source


# ── early profile & narrative radar ──────────────────────────
def test_small_caps_use_early_rules_and_smaller_bets():
    s = snap(liq=200_000)
    s.market_cap = 400_000                                     # under $1.5M: early profile
    safety = FakeSafety()
    t = trader(market=FakeMarket(s), safety=safety)
    run(t.on_tweet(elon_tweet()))
    pos = t.positions[MINT]
    assert pos.profile == "early" and pos.cost_usd == pytest.approx(40)       # 4% instead of 10%
    assert safety.overrides["min_holders"] == 40 and "pump_fun" in safety.overrides["lp_exempt_market_types"]


def test_bonding_curve_is_exempt_from_lp_lock_check():
    rep = _clean_report(markets=[{"marketType": "pump_fun", "pubkey": "POOL", "lp": {"lpLockedPct": 0}}])
    early = CFG["profiles"]["early"]
    v = SafetyChecker(CFG["safety"]).evaluate_solana(rep, {"lp_exempt_market_types": early["lp_exempt_market_types"]})
    assert v.ok and "bonding curve" in " ".join(v.reasons)
    assert not SafetyChecker(CFG["safety"]).evaluate_solana(rep).ok                # main rules: still rejected


def test_narrative_coin_needs_smart_money_before_buying():
    a, b, c = (kol(n, hold=3600, wallet=f"W{n}") for n in ("A1", "B1", "C1"))
    sm = _smart(a, b, c)
    t = trader()
    t.smart, t.narrative = sm, Narrative(CFG["celebrities"], CFG["narrative"])
    t.cfg = dict(CFG, celebrities=CFG["celebrities"])
    tate = next(x for x in CFG["celebrities"] if x["handle"] == "Cobratate")
    t.narrative.on_tweet(tate["handle"], "Escape the Matrix. Top G.")
    run(t.on_new_token(NewToken(MINT, "Top G", "TOPG", "dev", 30, time.time())))
    assert MINT in t.narrative.radar and t.events[-1].kind == "RADAR"
    for k in (a, b):
        run(sm.tracker.record(KolTrade(k.wallets[0], k, MINT, "buy", 1000, 1, 0, 1000, "s")))
        run(t.on_kol_trade(sm.tracker.recent[-1]))
    assert MINT not in t.positions                              # 2 wallets: not a cluster yet
    run(sm.tracker.record(KolTrade(c.wallets[0], c, MINT, "buy", 1000, 1, 0, 1000, "s")))
    run(t.on_kol_trade(sm.tracker.recent[-1]))
    assert MINT in t.positions and "'topg' narrative + 3 smart wallets" in t.positions[MINT].source


def test_pumpportal_messages():
    kind, tok = parse_message({"txType": "create", "mint": MINT, "name": "Top G", "symbol": "TOPG",
                               "traderPublicKey": "dev", "marketCapSol": 28.5})
    assert kind == "new" and tok.symbol == "TOPG" and tok.market_cap_sol == 28.5
    assert parse_message({"txType": "buy", "mint": MINT}) is None
