# CTMC · Crypto Trade Meme Coins

A Solana meme-coin trader driven by **celebrity hype that real money backs up**.

It buys the moment a watched celebrity posts a coin, or when a coin named after them gets bought by
**proven, high-win-rate wallets**. It runs a full rug, bundle and wallet-connection screen in seconds,
takes its stake back at 2x, and lets the rest ride a profit ladder until the volume or the smart money leaves.

> **Paper mode is the default.** Celebrity coins are the most-rugged category in crypto. Paper-trade for weeks,
> read `python -m ctmc report`, run `python -m ctmc simulate`, and only then risk money you can afford to lose.

---

## How it trades

### Entries: three triggers, all celebrity-driven

| Trigger | What happens |
|---|---|
| **A celebrity posts a coin** | **41 accounts** in `celebrities.yaml` (add freely), pushed live from X. Verified by numeric user ID, so a fake or renamed account can't trigger it. The contract address comes from the text or any pump.fun / DexScreener / Birdeye / Solscan / Jupiter / GMGN link. |
| **A celebrity's own wallet buys** | As strong as a tweet. Add their known wallets with `kols add --celebrity`. |
| **Celebrity-name narrative** (the Andrew Tate effect) | Any post from a watched celebrity puts them "hot" for 12h. New pump.fun launches and DEX search results named after them or their words ("Top G", "Kekius Maximus") go on a **radar**. A radar coin is bought only once **3+ proven wallets** pile in. A name match alone is never enough. |

### Checks: all at once, 4-second limit, no data = no trade

| Check | What it rejects |
|---|---|
| **Market (every DEX pool summed)** | Thin liquidity, low volume, market cap far above liquidity |
| **Round-trip quote** | Coins you can't sell (honeypots), hidden taxes, losing >15% buying and selling straight back |
| **Rug screen** (rugcheck) | Mint or freeze authority live, Token-2022 traps, LP not locked, whale or top-10 concentration, big dev bag, serial launchers, bundles |
| **Bubble map** | Top holders funded by the same wallet (>12%), wallets linked to the dev (>5%) |
| **The crowd** (insurance) | A **copy-trade wave**: fast-flip KOLs just bought and it's already +25% in 5 min, so we'd be their exit liquidity. Or **proven wallets selling into the hype**. |
| **Costs** | Fees + slippage would eat >8% of the position (size is raised within a cap if that fixes it) |
| **Leader guard** | SOL / WIF / FARTCOIN dumping, or their price feed gone quiet |
| **Protections** | Kill switch on, paused after anomalies, coin cooldown, daily caps, a celebrity benched for a poor record |

### Position size

10% of equity, then adjusted:
- ×1 to ×2 for proven wallets already holding it
- ×½ or ×¼ if it already pumped 300%+ or 1000%+ this hour
- ×½ for a celebrity with a weak record
- **Early coins** (under $1.5M market cap) start at 4% and use early-coin rules. pump.fun bonding curves are exempt from the LP-lock check, because a bonding curve can't be pulled.

### Exits: your ladder plus protection

| When | Action |
|---|---|
| **2x** | Sell exactly what you put in. The rest is free. |
| **10x · 30x · 50x · 100x · then every 2x** | Sell half of what's left |
| **1000x** | Sell the rest |
| **Profit floors** | Peaked 10x → never below 6x · 30x → 21x · 100x → 75x · 300x → 240x |
| **Volume dies** / **buys dry up + heavy sells** | Sell everything |
| **Smart money decoded** | A proven wallet bailing early → sell half · most smart money gone → sell all · a celebrity's wallet sells → sell all now. Scalpers' sells are ignored. |
| **Rug watch** (live) | Liquidity pulled, dev selling, or whales dumping → sell all now |
| **Leader crash** | SOL / WIF / FARTCOIN −4% in 5 min → sell all |
| **Backstops** | −30% before 2x · −50% from peak after 2x · no 2x within 24h |
| **After-buy checks** | The tweet isn't catching on (engagement vs that celebrity's usual), or Grok calls the hype fake → sell half |

Exits that must land retry with rising slippage, widening only when the failure really was slippage.

### Fees & slippage: counted everywhere

- Every buy and sale pays its **network fee** (priority + base + any Jito tip). Live fees are read from the chain; paper charges an estimate.
- **Every profit figure is net**: positions, the journal, per-celebrity scorecards, the report.
- **The 2x sale recovers your stake *plus every fee paid***, so the free bag is actually free.
- A ladder half worth **under $4 is skipped**, because the fee would eat it.
- Before buying, the **buy-then-sell round trip** must cost ≤8%, and **fees + slippage over the trade's life** must stay ≤8% of the position.

## Smart money (KOLs): insurance, not copy-trading

The bot never copies a KOL. It reads **the whole crowd**, hundreds to thousands of wallets, as insurance on a
celebrity trade: how many proven wallets hold the coin, who bought and sold in the last hour, and the net money flow.

- **Fast-flippers** (hold for seconds, thousands of trades a week) farm their copy traders. They're streamed only as a
  **warning sign**. If they just bought and the coin is already pumping, it's a copy-trade wave and the bot sits it out.
- **Discovery grows the list automatically:** `kols discover` mines today's trending winners for wallets that made money
  on **several different** coins, scores each on its full history, and keeps the real ones. Run it daily; it compounds.
- **Scales:** with a Helius plan that supports enhanced websockets, one subscription covers thousands of wallets.
  Otherwise standard subscriptions are split across connections. What the crowd holds is saved, so it survives restarts.

```bash
python -m ctmc kols import            # pull KOL wallets + stats from kolexplorer.com
python -m ctmc kols discover          # find repeat-winner wallets from trending coins (Helius)
python -m ctmc kols list              # who counts, and why
python -m ctmc kols score <wallet> --name X   # score any wallet from its own on-chain swaps
python -m ctmc kols add "Name" <wallet> --x handle --celebrity
```

Every wallet is judged on two questions:
- **Do they actually make money?** Win rate, realized PnL, sample size.
- **Can we follow them?** Median hold time. Most leaderboard KOLs hold for **seconds**, which makes them bots to us, so they're marked *unfollowable* and ignored. Fame counts for nothing: a famous name with a 39% win rate is marked *no edge*.

## Setup

```bash
pip install -r requirements.txt
pip install solders                    # live trading only
cp .env.example .env                   # fill in your keys
```

| Key | For | Cost |
|---|---|---|
| `X_BEARER_TOKEN` | Instant tweets (filtered stream), ID lookups, engagement check | X API plan with filtered stream |
| `WEBHOOK_SECRET` | Cheaper: a tweet-alert service POSTs to the bot | Depends on the service |
| `SOLANA_RPC_URL` | Speed, bubble maps, live wallet tracking (Helius / Triton) | Free tier possible |
| `HELIUS_API_KEY` | Wallet discovery + scoring; enhanced websocket tracks thousands of wallets on plans that include it | Free tier works |
| `JUPITER_API_KEY` | Prices, quotes, swaps (`api.jup.ag`; free tier is 60/min) | Free |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Alerts + remote control from your phone | Free |
| `XAI_API_KEY` | Grok's after-buy hype review | Paid |
| `WALLET_PRIVATE_KEY` | Live only. Use a dedicated hot wallet. | — |

## Commands

```bash
python -m ctmc run                                    # paper trading, live dashboard
python -m ctmc run --live --i-understand-the-risk     # real money
python -m ctmc check <mint>                           # every metric + YES/NO for one coin
python -m ctmc test-tweet "<text>" --as elonmusk      # what the bot would do with a tweet
python -m ctmc report [--days 30]                     # performance by celebrity, exit rule, profile
python -m ctmc simulate --compare                     # time machine: replay your exits 10,000x
python -m ctmc pause | resume                         # kill switch (also /pause, /resume on Telegram)
python -m pytest -q                                   # 79 scenario tests
```

**Telegram:** `/status` `/profit` `/pause` `/resume` `/sellall` `/sell SYMBOL`. Only your chat ID is accepted.

## Crash-safe

Everything lives in `data/paper.sqlite` or `data/live.sqlite`: positions with ladder progress, every fill, the
event journal, protection locks, equity snapshots and recorded price paths. A restart picks up exactly where it
left off. In live mode the wallet wins: positions are reconciled against on-chain balances before any buy.
Live swaps are recorded before sending, re-broadcast until they land or provably expire, and never re-sent on a
mere timeout, which is how bots sell the same bag twice.

## Honest limits

- **Snipers are faster.** Bots with no checks buy in under a second. This bot spends 2–4 seconds checking, on purpose.
- **Most celebrity-launched coins get rejected.** Their teams usually hold too much supply. That's the safety rules working.
- **The time machine is only as good as its paths.** Synthetic paths show how the rules interact, not what you'll earn.
  Real paths are recorded automatically while you trade.
- **Long losing streaks are normal.** A few big winners pay for many small losses. The protections pause on anomalies
  (rugs, crashes, instant stop-outs), not on ordinary losing streaks.
- **Speed is your setup.** A paid RPC and a server near Solana validators matter more than anything in the code.

## Where things live

```
ctmc/
  feeds/       twitter (X stream, webhook) · pumpportal (new launches) · cex (Binance leader prices)
  data/        market (all DEX pools, Jupiter prices, round-trip quotes)
  safety/      checker (rugcheck rules) · clusters (bubble map) · rugwatch (post-buy, real time)
  kol/         registry · importer (kolexplorer) · scorer (on-chain) · tracker (live wallets) · signals
  signals/     narrative · flow · leader · hype (Grok) · postbuy
  strategy/    engine (the brain) · ladder
  execution/   broker (paper, Jupiter live)
  storage.py · protections.py · notify.py · report.py · simulate.py · runner.py
docs/METRICS.md   every metric from the 16 researched repos: built, partly built, or not built and why
```
