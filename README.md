# CTMC · Crypto Trade Meme Coins

A Solana meme-coin trader that only buys **when a watched celebrity tweets a coin**.
It reacts the moment the tweet arrives, checks the coin for rugs, bundles and connected
wallets in a few seconds, buys at a size the pool can handle, then takes profit on a fixed ladder.

> **Paper mode is the default.** Celebrity coins are the most-rugged category in crypto
> (fake accounts, hacked accounts, insider launches). Run on paper for weeks before using real money,
> and only risk money you can afford to lose.

---

## How it trades

### Buying: instant, but only if it's safe

| Step | What happens |
|---|---|
| **Tweet arrives** | Pushed live from the X filtered stream (or any alert service via webhook). Nothing waits on a timer. |
| **Real account?** | Checked by numeric **X user ID**, not the handle, so a fake or renamed "@elonmusk" can't trigger a buy. Replies and tweets older than 20s are ignored. |
| **Which coin?** | Reads the contract address from the tweet text or from pump.fun / DexScreener / Birdeye / Solscan / Jupiter / GMGN / Photon links. A bare `$TICKER` is only used if one big coin clearly owns that name; copycats are skipped. |
| **Checks, all at once** | Run in parallel with a **4-second limit**. Too slow means no trade. |
| ↳ Market | Liquidity and volume **added up across every DEX pool** (Raydium, Orca, Meteora, Pump AMM, …). |
| ↳ Rug safety | rugcheck.xyz: mint and freeze authority revoked, ≥ 90% LP locked or burned, no wallet over 8%, top 10 ≤ 30%, bundles ≤ 15%. |
| ↳ Bubble map | Traces who funded each top-20 holder. Wallets sharing a funder count as one "bubble" (≤ 12% allowed); wallets linked to the dev ≤ 5%. Exchange hot wallets are excluded. |
| ↳ Leaders | No buys while SOL / WIF / FARTCOIN are dropping. |
| **Buy** | Routed by Jupiter across every Solana DEX. The order shrinks until price impact is ≤ 3%. Max slippage 3%, very-high priority fee, waits for on-chain confirmation. |

### Selling: the ladder

| When | Action |
|---|---|
| **2x** | Sell exactly what you put in. The rest is free. |
| **10x** | Sell half of what's left |
| **30x** | Sell half again |
| **50x** | Sell half again |
| **100x** | Sell half again |
| **200x, 400x, …** | Keep halving at every doubling |
| **1000x** | Sell everything left. Halving at each doubling keeps the bag's dollar value flat, so it needs a final rung. |
| **Volume dies** | 5-minute volume under 25% of normal for 6 checks in a row → sell everything |
| **Buys dry up + heavy sells** | Sell everything |
| **Leaders crash** | SOL / WIF / FARTCOIN fall 4% in 5 min → sell everything |
| **Backstops** | −30% before reaching 2x; −50% from the peak after 2x |

Exits that have to land (crashes, stops) retry with rising slippage: 5% → 8% → 15% → 25%.

**Dip re-entry:** if a coin a celebrity backed in the last 72h panic-dumps and the sellers run out, the bot can buy back in. Coins without a celebrity tweet are never bought.

## Setup

```bash
pip install -r requirements.txt
pip install solders           # only for live trading
cp .env.example .env          # fill in your keys
```

| Key | What it's for | Cost |
|---|---|---|
| `X_BEARER_TOKEN` | Instant tweets via the official X filtered stream | X API plan with filtered stream (Pro tier, expensive) |
| `WEBHOOK_SECRET` | Cheaper option: a tweet-alert service POSTs tweets to the bot | Depends on the service |
| `SOLANA_RPC_URL` | Speed and bubble-map tracing. Use Helius or Triton, not the free public RPC | Free tier possible |
| `BIRDEYE_API_KEY` | Holder counts, unique wallets (optional) | Free tier possible |
| `XAI_API_KEY` | Grok second opinion on hype. Adds 10–30s, so off on the instant path | Paid |
| `WALLET_PRIVATE_KEY` | Live trading only. Use a dedicated hot wallet | — |

Add or remove celebrities in `config.yaml → celebrities` (handle + numeric ID from tweeterid.com).

## Use

```bash
python -m ctmc check <mint>                          # every metric + YES/NO on one coin
python -m ctmc test-tweet "<tweet text>" --as elonmusk   # what the bot would do with that tweet
python -m ctmc run                                   # paper trading, live dashboard
python -m ctmc run --live --i-understand-the-risk    # real money
python -m pytest -q                                  # 21 scenario tests
```

## Honest limits

- **Snipers are faster.** Bots with no safety checks buy within a second. This bot spends 2–4 seconds checking first, on purpose. That's the price of not buying rugs.
- **Most celebrity-launched coins will be rejected.** Coins launched by a celebrity's own team usually hold huge insider supply, which fails the holder rules. That's by design.
- **Speed depends on your setup.** A paid RPC and running near Solana validators (US-East / Frankfurt) matter more than anything in the code.
