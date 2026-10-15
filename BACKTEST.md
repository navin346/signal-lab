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
| rel_value | train | +29.7% | -33.1% | 1.26 | 583 | 46.5% | 1.09 |
| rel_value | test | +36.0% | -36.5% | 1.31 | 587 | 44.6% | 1.06 |
| rel_value | full | +77.0% | -36.5% | 1.29 | 1170 | 45.6% | 1.07 |
| BTC buy & hold | full | -17.3% | | | | | |

**Error bars.** A 95% block-bootstrap range on the full-period return shows how much luck is in these numbers:

| bot | 95% range | chance the result is really a loss |
|---|---|---|
| ms_struct | -17% to +270% | 7% |
| ta_trend | -37% to +303% | 22% |
| rel_value | -45% to +560% | 16% |

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

**Learning did not beat a simple equal split this year.** The patterns it found were mostly noise, e.g. "the TA bot works best during Asian hours".

What did help is combining the bots: an equal split had about half the drawdown of any single bot (-9.5% vs -20% to -36%).

The live watcher therefore stays close to the equal split, and only tilts when a pattern has 30+ trades behind it. It records the conditions from day one, so in a few months there will be enough live data to judge whether learning adds anything. Reproduce with `python -m backtest.watcher_bt --data CACHE`.

## Known biases (read before trusting any number)
1. **Universe chosen with today's information.** The directional list and the top-30 are today's biggest coins. That includes some of last year's big winners (ZEC, HYPE). This flatters trend and momentum strategies.
2. **Spot prices for perps.** The directional backtest uses Binance spot candles; live trades Hyperliquid perps. Funding is real Hyperliquid funding.
3. **Small samples.** ms_struct made 84 trades in 11 months. A few big winners carry it (24% win rate, about 2R average winner).
4. **One year, one market regime.** BTC fell 17% over the period; trend and momentum systems like that.
5. **Costs are modelled, not measured.** The live paper run uses the same costs, so live vs backtest is comparable. Real fills on thin books would be worse.
