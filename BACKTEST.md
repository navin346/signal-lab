# Backtest: how the four bots were chosen

**Period:** 2025-11-06 → 2026-10-12, about 11 months after indicator warm-up.
- **TRAIN** = first half (to 2026-04-25).
- **TEST** = second half.

**Engine:** the same `lab/engine.py` the live bot runs. It replays hourly candles with:
- stops checked on every candle's high and low, and a stop wins when a candle touches both stop and target;
- gaps filled at the open;
- taker fee 4.5 bps (9 bps on trade.xyz markets) plus 2 bps slippage per side;
- maker fee on targets;
- real Hyperliquid hourly funding.

Every bot starts with $500.

**Data:**
- **Directional bots:** Binance spot 1h candles for crypto. HYPE, gold, oil and the Nasdaq-100 use Hyperliquid 1h data, which only covers the last ~208 days.
- **Relative value:** Hyperliquid 4h candles for the top 30.

Reproduce with:
```
python -m backtest.run --data CACHE --bot ta_trend --grid
python -m backtest.report --data CACHE
```

## Results of the live configurations

| bot | half | return | max drawdown | Sharpe | trades | win % | profit factor |
|---|---|---|---|---|---|---|---|
| ms_struct | train | +45.6% | -16.6% | 1.99 | 29 | 20.7% | 1.94 |
| ms_struct | test | +11.7% | -18.1% | 0.83 | 55 | 25.5% | 1.34 |
| ms_struct | full | +63.5% | -20.0% | 1.48 | 84 | 23.8% | 1.52 |
| ta_trend | train | +12.0% | -16.3% | 0.77 | 56 | 39.3% | 1.43 |
| ta_trend | test | +28.0% | -19.3% | 1.30 | 122 | 32.8% | 1.40 |
| ta_trend | full | +43.3% | -21.4% | 1.05 | 178 | 34.8% | 1.41 |
| rel_value (since Oct 17) | train | +73.2% | -19.7% | 2.66 | 330 | 44.8% | 1.24 |
| rel_value (since Oct 17) | test | +67.6% | -23.4% | 2.08 | 352 | 41.8% | 1.20 |
| rel_value (since Oct 17) | full | +192.2% | -23.4% | 2.33 | 682 | 43.3% | 1.21 |
| rel_value (Oct 12-17: 3-day, no filter) | full | +77.0% | -36.5% | 1.29 | 1170 | 45.6% | 1.07 |
| BTC buy & hold | full | -17.3% | | | | | |

**Error bars.** A 95% block-bootstrap range on the full-period return shows how much luck is in these numbers:

| bot | 95% range | chance the result is really a loss |
|---|---|---|
| ms_struct | -17% to +270% | 7% |
| ta_trend | -37% to +303% | 22% |
| rel_value (Oct 12-17 version) | -45% to +560% | 16% |

So these numbers are encouraging, not proof. **news_regime cannot be backtested**: there is no archive of timestamped headlines. It is judged live only.

## What was tested, what lost, and what was picked

| variant | TRAIN | TEST |
|---|---|---|
| TA: 4H EMA 21/55 cross + ADX 20 (your original ask) | -33.7% (Sharpe -2.89) | +9.3% (0.65) |
| TA: 4H EMA 9/21 cross, no filter | -10.1% (0.09) | +19.3% (0.84) |
| Market structure: CHoCH + SFP entries with trailed runner (first design) | +119.5% (1.98) | -33.9% (-0.57) |
| Market structure: SFP entries only | +2.1% (0.43) | -58.0% (-2.70) |
| RV: BTC-beta residual reversal, 3d (first design) | -61.5% (-2.96) | -55.7% (-2.18) |
| RV: funding carry, 7d | +30.6% (1.69) | -6.7% (-0.06) |
| RV: cross-sectional momentum, 7d | +41.0% (1.62) | +60.1% (1.94) |
| Regime rules alone (ADX trend pullbacks + Bollinger range fades) | -34.2% (-1.39) | -45.4% (-1.99) |

### ta_trend
The grid tested 54 combinations: EMA 9/21, 21/55 and 50/200, or Donchian 21/55/200 breakouts; ADX filter off, 20 or 25; ATR trail 2, 3 or 4.

- 4H EMA crossovers lost in TRAIN for every setting.
- The slow Donchian breakout (200 four-hour candles, about 33 days) was the only family positive in TRAIN.
- The train-best setting was Donchian 200, ADX ≥ 20, 2 ATR trail. It held up in TEST.

So the "TA play" is a breakout system, not an EMA cross. The EMA code is still there (`mode="ema"`).

### ms_struct
This bot implements the rules from the five PDFs:
- deep swings, BOS on candle closes, the protected HL/LH, and the trend changing only when that level closes beyond;
- HTF bias with LTF entries: wait for the pullback after a BOS, then enter on the 1H CHoCH back in trend direction;
- stop beyond the pullback extreme plus an ATR buffer (Trader Dante's ATR trick);
- target the previous swing high/low, with a minimum R:R;
- 1% risk per trade, never above 5x;
- SFPs at key highs/lows, skipping doji candles.

Round 1 (108 combinations) showed SFP entries alone lose in TRAIN and TEST, so they are off. Round 2 (16 combinations) was CHoCH entries only. The train-best was:
- minimum R:R 2;
- stop 1 ATR beyond the pullback;
- no runner (the trailed runner is in the code but off);
- 1% risk;
- at most 4 open positions.

The daily-bias filter tied with no filter in TRAIN. I kept it because the PDFs say so ("HTF bias + LTF confirmation"). It was also better in TEST, but that was not the reason.

### rel_value
Each day the bot ranks the top-30 coins and goes long the 5 cheapest and short the 5 most expensive by the score, with equal dollars per side and 2x gross. The grid tested 120 combinations of residual-vs-BTC reversal, plain reversal, momentum and funding carry, over 1, 3, 7, 14 and 28-day lookbacks.

- Short-horizon reversal and residual reversal lost in both halves, including my first design.
- Momentum (3-7 days) was positive in both halves.
- **Live: momentum over 3 days**, keeping a coin while it stays in the top/bottom 8 to cut turnover.

The single train-best setting (28d residual reversal) was positive in only one of the two TRAIN quarters and lost in TEST. 3-day momentum was positive in both TRAIN quarters and in TEST. This choice was made after seeing TEST, so the TEST number for rel_value is optimistic.

#### Oct 17 change: skip extreme movers, 7-day lookback
**What happened live.** On Oct 15 the market bounced. The bot was short the coins that had crashed the most over 3 days:
- NEAR -19%, then bounced +18% in five hours;
- ENA -19.5%, then +10%;
- PUMP, WLD and HBAR, then +5 to +8%.

Its longs were the calm coins (BTC, BNB, XMR), which rose only about 2%. Equal dollars on each side, but very unequal "bounciness". The book lost about $43 in an afternoon.

This is the textbook way momentum strategies get hurt: the biggest losers snap back hardest.

**Fixes tested on the same year (TRAIN / TEST return, Sharpe):**

| variant | TRAIN | TEST |
|---|---|---|
| 3-day, as it was | +29.7% (1.26) | +36.1% (1.31) |
| 3-day + skip extreme movers (|z| > 2) | +31.4% (1.31) | +64.5% (1.88) |
| 7-day, no filter | +41.0% (1.62) | +60.1% (1.94) |
| **7-day + skip extreme movers (live)** | **+73.2% (2.66)** | **+67.6% (2.08)** |
| beta-weighted legs (smaller shorts in wild coins) | worse in TEST for every lookback | |
| inverse-volatility legs | worse in TRAIN | |

**Skip extreme movers:** don't open a new leg in a coin whose move is more than 2 standard deviations from the other 29. It improved both halves at both lookbacks, and gave positive or flat results in all four quarters.

Using real Hyperliquid prices, it would have blocked exactly the two worst Oct 15 shorts: ENA (z = -2.25) and NEAR (z = -2.14).

The move to a 7-day lookback was picked with the whole year in view, so the TEST number is optimistic. The Oct 12 version's last quarter was -11.7%, so recent conditions were already hard for it.

#### Oct 18: protections tested on top (7-day + skip-extremes)
| variant | TRAIN | TEST (max drawdown) | worst quarter | worst day |
|---|---|---|---|---|
| 7-day + skip extremes (Oct 17) | +73.2% | +67.6% (-23.4%) | -0.3% | -9.3% |
| + **volatility targeting (live from Oct 18)** | +71.0% | +56.3% (-18.6%) | 0.0% | -7.5% |
| + stop-loss on each leg at 3x its daily volatility | +64.9% | +62.0% (-23.7%) | -1.3% | -9.4% |
| + stop-loss at 2x daily volatility | +51.0% | +35.8% (-25.2%) | -4.2% | -9.3% |
| for reference: 7-day without skip-extremes | +41.0% | +60.1% (-22.2%) | -7.9% | -13.9% |

- **Per-leg stops:** they cut returns and did not reduce the worst day. A squeeze moves fast, and a stopped leg misses the reversal.
- **Volatility targeting:** when the book's last-20-day swings exceed its usual level, new legs shrink, down to one third. It never sizes up. It costs some return but lowers the drawdown and the worst day.

Live, it needs about 40 days of the bot's own history before it can act. Until then the skip-extremes rule is the main protection.

#### Oct 19: hourly circuit breaker
**Rule:** the book is checked every hour. If it has lost more than X% since the day's rebalance, it is cut (in half, or closed) until the next rebalance.

This test used hourly prices: Binance spot for 26 of the 30 coins, Hyperliquid 4h for HYPE, XMR, CC and SHIB. Compare rows only within this table.

| breaker | TRAIN (Sharpe) | TEST (Sharpe) | full-year max drawdown | worst day | times it fired |
|---|---|---|---|---|---|
| none | +50.0% (2.69) | +25.8% (1.70) | -21.9% | -7.4% | 0 |
| 3%, cut half | +37.4% (2.43) | +34.6% (2.46) | -23.9% | -4.6% | ~23 |
| 4%, cut half | +38.8% (2.38) | +42.1% (2.56) | -23.5% | -5.8% | ~10 |
| **5%, cut half (live)** | **+55.9% (3.04)** | **+38.8% (2.38)** | -21.5% | -6.4% | ~4 |
| 5%, close everything | +57.0% (3.05) | +35.0% (2.18) | -21.5% | -5.8% | ~5 |

- **5%, cut half** was the only setting better than no breaker in both halves.
- **It stops sudden one-day crashes but not slow bleeds.** The max drawdown barely changed.
- **Tighter breakers** fire more often and give up returns in the TRAIN half.

### news_regime
The regime rules alone lost in both halves (table above). So the bot never trades on them:
- every entry needs the AI news analyst's view to agree;
- the ADX regime only decides the stop, target and holding time.

Whether the news adds an edge is exactly what the live paper run will show.

### watcher (self-learning)
The watcher copies the other bots into its own $500 account, a 25% share each. It learns which bot does well in which conditions:
- trend or range;
- with or against the daily structure;
- volatility;
- news;
- funding;
- time of day.

It sizes each copy up or down from there. This backtest uses the three backtestable bots together. Results:

| watcher variant | TRAIN | TEST | full year | max drawdown |
|---|---|---|---|---|
| plain equal split (no learning) | +14.2% (Sharpe 1.53) | +20.4% (1.91) | **+37.6%** | -9.5% |
| bold learning (0x–2x, trusts 5-trade patterns) | +9.1% (1.14) | +20.5% (1.57) | +31.6% | -11.3% |
| **live setting: cautious learning** (0.5x–1.5x, trusts 30-trade patterns) | +12.0% (1.38) | +20.8% (1.75) | +35.4% | -10.3% |

These numbers are with the Oct 12 relative-value settings. With the Oct 17 settings:
- equal split: +68.3% (max drawdown -7.7%);
- cautious learning: +73.4% (max drawdown -9.8%).

Learning was slightly ahead in TEST and behind in TRAIN: still no clear evidence it adds anything.

**Learning did not beat a simple equal split this year.** The patterns it found were mostly noise, e.g. "the TA bot works best during Asian hours".

What did help is combining the bots: an equal split had about half the drawdown of any single bot (-9.5% vs -20% to -36%).

The live watcher therefore stays close to the equal split, and only tilts when a pattern has 30+ trades behind it. It records the conditions from day one, so in a few months there will be enough live data to judge whether learning adds anything. Reproduce with `python -m backtest.watcher_bt --data CACHE`.

## Known biases (read before trusting any number)
1. **Universe chosen with today's information.** The directional list and the top-30 are today's biggest coins. That includes some of last year's big winners (ZEC, HYPE). This flatters trend and momentum strategies.
2. **Spot prices for perps.** The directional backtest uses Binance spot candles; live trades Hyperliquid perps. Funding is real Hyperliquid funding.
3. **Small samples.** ms_struct made 84 trades in 11 months. A few big winners carry it (24% win rate, about 2R average winner).
4. **One year, one market regime.** BTC fell 17% over the period; trend and momentum systems like that.
5. **Costs are modelled, not measured.** The live paper run uses the same costs, so live vs backtest is comparable. Real fills on thin books would be worse.
