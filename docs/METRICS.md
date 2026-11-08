# CTMC metrics catalogue

Every metric found while reading 16 trading repos, and what CTMC does with it.

**Repos read:** TradingAgents · day-trader-skill · trader-skill · last30days-skill · Vibe-Trading · Kronos · freqtrade · daily_stock_analysis · OpenBB · tick-stock-panel · everything-claude-trading · Hummingbot (+ Gateway) · Jesse · CCXT · NautilusTrader · agiprolabs/claude-trading-skills. Also kolexplorer.com and PumpPortal.

✅ built · ◐ partly built · ✗ not built (reason given)

---

## 1. The signal: celebrity posts

| Metric | How | Used for | | Source |
|---|---|---|---|---|
| Verified author | Numeric X user ID (handles can be renamed or impersonated) | Only real accounts trigger | ✅ | CTMC |
| Exact post time | Snowflake ID: `(id >> 22) + 1288834974657` ms | Latency; reject stale or replayed tweets | ✅ | last30days |
| Contract address in the post | Text, plus pump.fun / DexScreener / Birdeye / Solscan / Jupiter / GMGN / Photon links | The coin to buy | ✅ | CTMC |
| Ticker resolution | `$TICKER` must belong to one coin with ≥10x the liquidity of any copycat | Avoid copycats | ✅ | CTMC |
| Narrative keywords | #tags, $tags, quoted names, Capitalised Phrases ("Top G", "Kekius Maximus") | Celebrity-name coin radar | ✅ | CTMC |
| Engagement at about 1 min | `.55·ln(1+likes) + .25·ln(1+reposts) + .15·ln(1+replies) + .05·ln(1+quotes)`, against that celebrity's own median | Post not catching on → sell half | ✅ | last30days |
| Grok hype read | Organic or not, credible accounts, red flags (live X search) | After the buy, reduce only | ✅ | CTMC / Vibe-Trading |
| Mention velocity, cross-source rank (Reddit, YouTube, HN, Polymarket money) | Log-weighted engagement, reciprocal-rank fusion, freshness decay | Hype breadth | ✗ minutes-scale, too slow for entries; a daily-review candidate | last30days |
| KOL influence / bot-account scores | Followers, engagement rate, account age; new accounts posting >50/day | Weighting X chatter | ✗ needs X timeline data per account | Vibe-Trading |

## 2. Smart money: KOL and celebrity wallets

| Metric | How | Used for | | Source |
|---|---|---|---|---|
| Win rate, realized PnL (7d / 30d) | kolexplorer import, or our own scoring from Helius swap history | Quality score | ✅ | kolexplorer / claude-trading-skills |
| Profit factor, rug rate, flip rate | Own on-chain scoring | Quality and followability | ✅ | freqtrade / claude-trading-skills |
| Median hold, trades and tokens per week | A wallet flipping in seconds or trading thousands a week is a bot | Followability | ✅ | kolexplorer / claude-trading-skills |
| Median entry market cap | kolexplorer | Who buys early | ◐ stored and shown, not yet weighted | kolexplorer |
| Confluence at entry | Proven wallets holding the coin when the celebrity posts (live balance check) | Position size ×1 to ×2 | ✅ | trader-skill (13F diff idea) |
| Cluster | ≥3 proven wallets, or total weight ≥1.5, buying within 15 min | Entry for narrative coins | ✅ | claude-trading-skills |
| Sell decoding | Share of bag sold, held time vs their usual, share of smart money out | Ignore / trim / exit / urgent | ✅ | CTMC |
| Celebrity's own wallet | Registry role `celebrity` | Buy = entry; sell = urgent exit | ✅ | CTMC |
| copy_score, bot probability (CV of trade gaps) | Weighted sample size, win rate, PF, consistency, recency | Wallet ranking | ◐ quality × followability is a simpler version | claude-trading-skills |

## 3. Price action

| Metric | How | Used for | | Source |
|---|---|---|---|---|
| 5m / 1h change | DexScreener | Flush and late-entry detection | ✅ | — |
| Late-entry size cut | Up ≥300% this hour → ½ size; ≥1000% → ¼ | Not chasing | ✅ | freqtrade RangeStability / DSA |
| Chase guard | Live quote price >25% above the checked price → no buy | Not chasing | ✅ | Hummingbot activation bounds |
| Panic-flush exhaustion | −12% in 1h with sells ≥1.8× buys, then sells back to even and a green 5m | Dip re-entry (celebrity-backed coins only) | ✅ | CTMC |
| ATR / Supertrend / Donchian / Rogers–Satchell volatility | 1-minute OHLC formulas (OpenBB) | Volatility-scaled stops | ✗ no candle feed yet; GeckoTerminal OHLCV is the next step | OpenBB |
| RSI, MACD, MA stacks | Classic indicators | — | ✗ stay "overbought" through a pump; wrong timescale | OpenBB / DSA |
| Kronos candle forecast | Foundation model | — | ✗ needs ≥64 bars; flattens sub-cent prices; seconds-old coins have no history | Kronos |
| Beta / tail dependence to leaders | Rolling OLS of coin vs SOL | Scaling leader-crash exits | ✗ needs candles | OpenBB / claude-trading-skills |

## 4. Volume, liquidity and execution cost

| Metric | How | Used for | | Source |
|---|---|---|---|---|
| Liquidity and volume across all pools | Sum over every DEX pool | Universe filter | ✅ | CTMC |
| Market cap ÷ liquidity | Too high = a few sells crater it | Universe filter | ✅ | CTMC |
| Round-trip cost | Quote buy $50 → sell it straight back; cost = 1 − back/in | Honeypots, taxes, fees, impact | ✅ | freqtrade SpreadFilter (adapted) |
| Price impact cap | Constant-product `dx = x·p/(1−p)`, then real Jupiter `priceImpactPct` | Order size | ✅ | — |
| Buy/sell ratio 5m, heavy-sell volume | Txn counts, sell share × 5m volume vs normal | Distribution exit | ✅ | CTMC |
| Volume death | 5m volume <25% of normal for 6 checks | Exit | ✅ | CTMC |
| Slippage curve `bps = a·q^b` | Several Jupiter quotes | Routable depth | ✗ round trip covers most of it | claude-trading-skills |
| Wash-trading score, trade-size entropy, unique-trader trend, VPIN | Trade-level data | Fake volume | ✗ needs a per-trade stream (Birdeye websocket is paid) | claude-trading-skills / Vibe-Trading |

## 5. Token safety (before buying)

| Metric | How | | Source |
|---|---|---|---|
| Mint and freeze authority revoked | rugcheck | ✅ | — |
| Token-2022 traps (permanent delegate, transfer hook, non-transferable, frozen by default) | rugcheck extensions | ✅ | CTMC |
| Transfer fee (tax) | rugcheck `transferFee.pct` | ✅ | — |
| LP locked or burned % (bonding curves exempt) | rugcheck markets | ✅ | — |
| Single wallet / top-10 share (pools, lockers and exchanges excluded by label) | rugcheck holders + knownAccounts | ✅ | — |
| Total holders | rugcheck | ✅ | — |
| Dev holding %, serial launcher (tokens created) | rugcheck creator | ✅ | claude-trading-skills |
| Insider / bundle networks | rugcheck | ✅ | — |
| Mutable metadata | rugcheck (warning only) | ✅ | — |
| Honeypot | Round-trip sell quote must exist | ✅ | — |
| Insider points (bundler 3, sniper 3, creator 2, …), first-buyer share | SolanaTracker `/first-buyers`, `/bundlers` | ✗ paid API; rugcheck plus bubble map cover most of it | claude-trading-skills |

## 6. Holders and wallet connections (bubble map)

| Metric | How | | Source |
|---|---|---|---|
| Same-funder bubbles | First SOL sender of each top-20 holder; exchange hot wallets excluded | ✅ | CTMC |
| Dev-linked share | Holders funded by, or funding, the creator | ✅ | CTMC |
| Sybil score 0–100 (cluster, co-trading, bundle ratio, funder uniqueness, density, wash pairs) | — | ◐ cluster and dev-link parts built; 2-hop tracing too slow for 4 s | claude-trading-skills |
| Gini / HHI / Nakamoto coefficient | Holder concentration | ✗ top-10 share already gates it | claude-trading-skills |
| Holder cost distribution (share in profit) | Per-holder entry cost | ✗ needs every holder's buy history | DSA |

## 7. After the buy: rug watch (real time)

| Metric | How | | Source |
|---|---|---|---|
| Liquidity pulled | `removed = 1 − (L_now/L_then)/√(P_now/P_then)` (price-adjusted) | ✅ | CTMC |
| Dev sells | Creator token account over websocket (`accountSubscribe`) | ✅ | claude-trading-skills |
| Whales dump | Top holders' combined share of supply sold | ✅ | claude-trading-skills |
| Smart money leaving | Sell decoding (section 2) | ✅ | CTMC |
| Transfers to exchanges, dormant wallets waking | Labelled counterparties | ✗ needs a wallet-label database | claude-trading-skills / Vibe-Trading |

## 8. Exits

| Metric | Rule | | Source |
|---|---|---|---|
| Cost-out | 2x → sell exactly the stake | ✅ | your design |
| Halving ladder | Half at 10/30/50/100x, then every 2x; final exit 1000x | ✅ | your design |
| Profit floors | Peak 10x → keep ≥6x; 30x → 21x; 100x → 75x; 300x → 240x | ✅ | freqtrade custom_stoploss |
| Crash backstop | −50% from peak after 2x | ✅ | Hummingbot trailing |
| Hard stop | −30% before 2x | ✅ | — |
| Time barrier | No 2x within 24h → out | ✅ | Hummingbot triple barrier |
| Volume death, distribution, leader crash, rug watch, smart-money exit | Sections 4, 7 and 2 | ✅ | — |
| Stops checked against a real sell quote | Sellable price, not the reference price | ✗ next step for thin pools | Hummingbot |

## 9. Risk and protections

| Metric | Rule | | Source |
|---|---|---|---|
| Kill switch | `data/HALT` file; Telegram `/pause` | ✅ | Vibe-Trading |
| Anomaly guard | 3 rugs / leader crashes / stops within 2 min in 24h → pause 12h. Ordinary stops don't count | ✅ | freqtrade StoplossGuard, calibrated |
| Rolling drawdown | Equity −30% from its 72h peak → pause 24h | ✅ | freqtrade MaxDrawdown |
| Coin cooldown | 60 min after selling a coin | ✅ | freqtrade CooldownPeriod |
| Daily caps | 12 buys/day, 3 per signal source | ✅ | Vibe-Trading |
| Signal-source scorecard | Share of trades reaching 2x (≥15 trades): ≤12% → ½ size, ≤5% → benched 7 days | ✅ | freqtrade LowProfitPairs / DSA / TSP |
| Daily loss limit, cash reserve, SOL fee reserve | 15%, 30%, 0.1 SOL | ✅ | Hummingbot Gateway |
| Losing-streak sizing | Off by default (7+ losses in a row is normal at a 75% loss rate) | ◐ available | TSP |
| Leader guard with hysteresis and staleness | Block at −2.5%, release at −1.25%; stale >90 s → no buys | ✅ | Vibe-Trading regime / Nautilus |
| Market temperature (share of trending coins up, leader basket) | Cached size multiplier | ✗ next step | DSA / TSP |
| CUSUM edge decay | `S = max(0, S + r − μ − k)` | ✗ scorecard covers it for now | everything-claude-trading |

## 10. Execution quality (live)

| Metric | How | | Source |
|---|---|---|---|
| Tweet → fill latency | Stored per trade | ✅ | — |
| Send → confirm time | Per swap | ✅ | Nautilus |
| Realized vs quoted output | Fill from on-chain balance change vs quote | ✅ | Gateway |
| Swap outcome | CONFIRMED / FAILED / DROPPED (proven expired) / UNRESOLVED, saved as pending before sending | ✅ | Gateway / Nautilus |
| Priority fee per trade, land rate, fees burned on failed transactions | — | ◐ fee capped; per-trade fee not stored yet | Hummingbot |
| Post-trade sandwich check (same signer before and after in the slot) | — | ✗ next step; use Jito tips meanwhile | claude-trading-skills |

## 11. Performance (`python -m ctmc report`)

| Metric | Why | | Source |
|---|---|---|---|
| Trades, PnL, expectancy, win rate | Basics (win rate alone misleads) | ✅ | freqtrade / Jesse |
| Reached-2x rate | The ladder's first rung | ✅ | DSA |
| Median vs mean return | The gap shows the skew | ✅ | freqtrade |
| Profit factor, and without the best trade | Is it one lucky trade? | ✅ | freqtrade research |
| Best trade's share of profit | Same question | ✅ | freqtrade research |
| Capture ratio (banked ÷ peak) | Did the exits keep the gains? | ✅ | freqtrade research |
| Omega(0), CVaR 95% | Tail-aware alternatives to Sharpe | ✅ | Jesse / OpenBB |
| Mark-to-market max drawdown | From equity snapshots | ✅ | freqtrade / Jesse |
| Longest losing streak, median hold | — | ✅ | — |
| Breakdowns by signal source, exit rule, profile | Attribution | ✅ | freqtrade |
| Sharpe, Sortino, Calmar, SQN, Kelly, Serenity | — | ✗ on purpose: misleading with few, fat-tailed trades | freqtrade / Jesse research |

## 12. Time machine (`python -m ctmc simulate`)

| Metric | How | | Source |
|---|---|---|---|
| Replay of the real exit rules over price paths | Recorded paths (automatic), or labelled synthetic paths | ✅ | day-trader-skill |
| Monte Carlo of whole trades | 10,000 runs × N trades: chance of profit, 5/50/95% equity, drawdown distribution, chance of a −50% drawdown | ✅ | Jesse / everything-claude-trading |
| Rule variants side by side | Floors, backstop, ladder, time barrier | ✅ | TSP robust optimizer (simplified) |
| Walk-forward, deflated Sharpe, PBO | — | ✗ needs 100+ recorded trades first | TSP / Vibe-Trading |

---

**Deliberately left out:**
- equity fundamentals, 13F and Congress filings (30–45+ days late), and A-share rules
- futures features: funding, leverage, liquidation
- hyperparameter optimisation (overfits a few dozen trades)
- LLM debate graphs on the buy path (minutes of latency)
- browser-cookie X scraping (a terms-of-service and credential risk)
