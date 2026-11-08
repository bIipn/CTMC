# CTMC · Crypto Trade Meme Coins

A meme-coin trader for Solana that trades **order flow and leader tokens**, not hype alone.
It holds 3–4 vetted, high-volume coins at a time, buys after panic sell-offs run out,
pulls its original stake out at 2x, and lets the rest ride until buyers dry up.

> **Paper mode is the default.** Run it on paper for at least a couple of weeks before you put money in.
> Meme coins can go to zero in minutes. Only trade money you can afford to lose.

---

## How it trades

| # | Rule | Where |
|---|------|-------|
| 1 | **Only liquid coins.** ≥ $1M daily volume, ≥ $150k liquidity, pair older than 24h. | `config.yaml → universe` |
| 2 | **Safe or skip.** Mint and freeze authority revoked, ≥ 90% of LP locked or burned, no wallet over 8%, top 10 wallets ≤ 30%, bundled or insider wallets ≤ 15%. Uses rugcheck.xyz. If the check can't run, the bot doesn't trade. | `ctmc/safety/checker.py` |
| 3 | **Real hype only.** Grok (xAI API with live X search) scores the X chatter. It must be organic, have at least 2 credible big accounts behind it, and have no rug or dev-dump red flags. | `ctmc/signals/hype.py` |
| 4 | **Follow the leaders.** Watches SOL, WIF and FARTCOIN. If any falls 2.5% inside 5 minutes, the bot sells *before* the followers' holders panic. New buys pause at −1.5%. | `ctmc/signals/leader.py` |
| 5 | **Buy the scared seller.** Waits for a panic flush (−12% in an hour with sells swamping buys), then buys only once the selling dries up and a green 5-minute candle prints. It never buys while the flush is still going. | `ctmc/signals/flow.py` |
| 6 | **Take your stake back.** At 2x it sells enough to recover the full cost, so the rest is a free runner. | `ctmc/strategy/engine.py` |
| 7 | **Sell when buys dry up.** The runner exits when buys fall off **and** heavy sells come in, or when it drops 25% from its peak. | `ctmc/strategy/engine.py` |
| 8 | **Guardrails.** −18% hard stop, 72h max hold, 30% cash reserve, 15% daily loss limit, 30-minute cooldown before buying back a coin it just sold. | `config.yaml` |

Profits compound because each new position is sized off current equity.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # add XAI_API_KEY (and a wallet key only for live)
```

## Use

```bash
python -m ctmc check <mint>   # full YES/NO safety + hype report on one coin
python -m ctmc scan           # screen trending Solana coins through every filter
python -m ctmc run            # paper trading with a live dashboard
python -m ctmc run --live --i-understand-the-risk   # real swaps through Jupiter
python -m pytest -q           # strategy scenario tests
```

## About "running in Grok"

Grok can't hold a wallet or run a loop around the clock, so it isn't the bot. Here it's the bot's **X research analyst**:
the bot calls the xAI API, Grok searches X live and returns a strict JSON verdict.
The xAI API is **paid** (separate from a free X/Grok account). Get a key at console.x.ai.
If no key is set, the hype check fails closed and the bot won't buy.

## Backtesting with TextToQuant

TextToQuant (connected to Claude) backtests on Binance spot candles. Use it to stress-test the
*leader-follow / buy-the-flush* logic on listed memes such as `WIFUSDT`.
It can't see brand-new DEX-only coins or 5-minute buy/sell counts, so treat its results as a sanity check, not proof.
A ready prompt:

> Backtest WIFUSDT on the 1h over the last year: buy when the 1h close is down more than 12% from the high of the last 6 bars and RSI(14) crosses back above 30; sell half at +100%, trailing stop 25% on the rest, stop loss 18%.

## Live trading checklist

- [ ] Paper-traded for 2+ weeks and the numbers held up
- [ ] Dedicated hot wallet holding only what you'd lose
- [ ] Paid RPC (Helius / Triton) in `SOLANA_RPC_URL`
- [ ] `pip install solders`
- [ ] Never commit `.env`
