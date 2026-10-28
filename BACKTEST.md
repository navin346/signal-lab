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

#### Oct 24: which coins? (universe scenario test, live rules, 4h data)
| universe | full year | Sharpe | max drawdown | worst day | TRAIN / TEST |
|---|---|---|---|---|---|
| top 10 (ranks 1-10), 3 per side | -23.8% | -0.34 | -45.2% | -12.0% | -39.9% / +27.6% |
| middle 10 (ranks 11-20), 3 per side | +123.0% | 1.79 | -31.1% | -10.9% | +30.6% / +70.0% |
| bottom 10 of the top 30 (ranks 21-30), 3 per side | +1.1% | 0.32 | -28.6% | -12.0% | -3.0% / +3.6% |
| **top 30 (live), 5 per side** | **+169.0%** | **2.36** | **-19.3%** | **-7.5%** | +71.0% / +56.3% |
| next 30 (ranks 31-59), 5 per side | -57.8% | -1.40 | -72.1% | -11.0% | +41.8% / -70.2% |
| top 60, 5 per side | -23.6% | -0.11 | -50.0% | -12.0% | +9.6% / -29.8% |
| top 60, 8 per side | +5.5% | 0.37 | -48.4% | -8.6% | +64.2% / -35.8% |

**Smaller coins as a source of asymmetry (ranks 21-30 or 31-59):**
- Every rule tried flipped between the two halves: momentum and reversal, 3-day and 7-day.
- Drawdowns reached 65-74%. Their big moves are pumps and dumps that reverse unpredictably.

**Top 10:** the majors move too much alike for "strong vs weak" to pay.

**Top 30** has enough breadth that only the true extremes get traded, and it is the only universe that stayed solid in both halves. It stays.

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

### turbo (Oct 22): can leverage get to 1% a day?
**First principles.** For an edge with average daily return m and daily variance v, long-run growth is fastest at leverage ≈ m / v (the Kelly fraction). Above that, growth falls and ruin risk rises.

The three backtestable bots side by side had:
- Sharpe about 2.6;
- daily mean +0.22% and daily volatility 1.63%;
- Kelly leverage about 8x.

**Engine backtest** (real fees, funding, every trade, a liquidation check on hourly closes). Turbo copies the three bots, each at a third, times the leverage:

| leverage | $500 became (339 days) | per day | max drawdown | liquidations |
|---|---|---|---|---|
| 1x | $966 | +0.19% | -8.6% | 0 |
| 2x | $1,714 | +0.36% | -16.6% | 0 |
| **3x + brake (live from Oct 22)** | **$2,843** | **+0.51%** | **-23.9%** | 0 |
| 4x (half Kelly) | $4,416 | +0.65% | -30.7% | 0 |
| 6x | $6,182 | +0.74% | -41.9% | 0 |
| 8x + brake (live Oct 22 for a few hours) | $10,814 | +0.91% | -49.2% | 0 |
| 8x, no brake | $17,048 | +1.05% | -53.8% | 0 |

The brake halves new copies while the account is 30%+ below its peak.

**The catch: everything depends on the edge being real.** Backtest edges usually shrink live. In a Monte Carlo where the true edge is half the backtest's:
- **at 8x:** the median year turned $500 into $649, there was a 14% chance of losing 90%+, and every path saw a 50%+ drawdown;
- **at 4x:** the median year turned $500 into about $1,090.

Live leverage is 3x, the owner's choice: about a third of the backtest Kelly. That leaves room for the real edge to come in smaller.

What 3x looked like day by day over 339 days:
- 95 days of +2% or more;
- 69 days of +0.5% to +2%;
- 50 roughly flat days;
- 125 losing days, 67 of them -2% or worse.

The best day was +22%, the worst -9%. Growth comes from good days outnumbering and outsizing bad ones, not from steady gains every day.

#### Oct 23: keeping drawdowns under 15%
Turbo at 3x, with each way of limiting the worst drawdown:

| setting | $500 became | per day | max drawdown | worst day |
|---|---|---|---|---|
| 3x, no control | $2,843 | +0.51% | -23.9% | -9.0% |
| plain 1.8x leverage | $1,537 | +0.33% | -15.0% | -5.4% |
| plain 1.5x leverage | $1,299 | +0.28% | -12.7% | -4.6% |
| 3x, drawdown-scaled from the first loss (15% budget) | $1,131 | +0.24% | -12.0% | -5.6% |
| 3x + volatility target 2%/day | $2,389 | +0.46% | -15.7% | -6.2% |
| **3x + volatility target 1.5%/day + backstop (live)** | **$1,907** | **+0.40%** | **-13.9%** | **-6.1%** |

Notes:
- **Drawdown-scaling from the first loss** keeps drawdowns low but sells near every bottom and misses the rebound. It does worse than simply using less leverage.
- **Volatility targeting** shrinks all positions, open ones included, when the account's last-20-day daily swings exceed the target. It beat plain lower leverage on both return and drawdown, and stayed under 15% in both halves (TRAIN -13.9%, TEST -12.5%).
- **The backstop** does nothing until the account is 12% below its peak, then shrinks size toward 20% by a 25% drawdown. It never fired in the backtest. It is insurance for the case where the edge stops working, which fixed leverage cannot handle.

So the live paper turbo is a test of whether the edge survives. With real money, start at 4x or less, and move up only after months of live results.

### Oct 24: can we raise return AND cut drawdown? (tested, nothing beat the live setup)
Baseline: Turbo at 3x with volatility control, $500 → $1,907 over the full year, worst drawdown -13.9%, Sharpe 3.35 (engine).

**1. Timing ("sit a bot out after a bad stretch").** Rule: trade a bot only if its last N days were positive, N from 5 to 60.
- Every version made less money, and most had deeper drawdowns.
- For example, the 20-day gate: -17.7% drawdown vs -15.3%, $1,490 vs $1,754, on a daily-return version of the same test.
- Inverse-volatility weights (give calmer bots more money) also did not help.
- Losing stretches are not predictable from a bot's recent results.

**2. More coins for the trend bot** (all top 30 instead of 15 markets): +25% vs +43%.

**3. New, unrelated edges.** The bots are already nearly independent: daily correlations -0.02 to 0.22. Two candidates were built and tested:
- **Snap-back:** 1-hour mean reversion on the 10 most liquid coins, outside strong trends. +21% at 1.5% risk, Sharpe 1.1, 56 trades, correlation with the bots about 0.
- **Crowd fade:** bet against funding-rate extremes. About 0% over the full year, but correlation about -0.5 (a hedge).

Added to Turbo, they gave $1,746 / $1,813 / $1,615 (snap-back, crowd fade, both), with drawdowns -12.9% / -12.8% / -14.1%. No real improvement, so neither went live. The code stays in `lab/strategies.py` (`SnapBack`, `CrowdFade`) for later.

*Correction:* a first blend test showed +0.60%/day with both added. It was wrong: one coin's data ended early, which cut that test to Nov-Jun. Always check the window.

**What a 1%-a-day target with a drawdown under 15% needs.** Roughly a Sharpe of 6-8; Turbo is about 3.3. The way there is more independent edges, each passing the full-year engine test. Today's two candidates did not.

### Oct 24: funding carry as a second source of profit (passes)
**Strategy:** cross-venue carry, Hyperliquid x Aster, the 60 most liquid shared symbols. Hold the 5 pairs with the largest
7-day-average funding gap, short where funding is higher and long where it is lower. Rebalance every 3 days, enter at a
20% APR gap, keep a pair while the gap is above 10%, 3x margin, 0.26% round trip.

The settings are the earlier study's defaults, not tuned on this window. Data: hourly funding on both venues; Aster hourly
prices; Hyperliquid prices from March onward, filled from 4h candles before that.

| carry setting | yearly, on margin | max drawdown | worst day | Sharpe | halves |
|---|---|---|---|---|---|
| default | +14.9% | -2.3% | -1.5% | 1.52 | +7.7% / +5.4% |
| default at 0.18% cost | +18.0% | -2.3% | -1.5% | 1.82 | +9.1% / +6.7% |
| 14-day window, 30% min | +10.7% | -1.9% | -1.5% | 1.61 | +6.6% / +2.9% |
| 8 pairs, 15% min | +11.0% | -1.7% | -1.4% | 1.53 | +6.2% / +3.6% |
| 3 pairs, 40% min | +9.1% | -2.4% | -1.1% | 1.43 | +3.8% / +4.3% |

Correlation with Turbo's daily returns: 0.08 (0.06 / 0.09 by half).

**Blended with Turbo, 335 overlapping days:**

| setup | $500 became | per day | max drawdown | worst day | Sharpe |
|---|---|---|---|---|---|
| Turbo alone | $1,992 | +0.41% | -13.9% | -6.1% | 3.48 |
| 80% Turbo / 20% carry | $1,575 | +0.34% | -10.7% | -4.8% | 3.54 |
| Turbo + carry on 50% of equity | $2,122 | +0.43% | -12.7% | -5.8% | 3.59 |
| Turbo + carry on 100% of equity | $2,255 | +0.45% | -12.0% | -5.6% | 3.66 |
| same, Turbo sized 1.1x | $2,561 | +0.49% | -13.3% | -6.2% | 3.65 |

"Turbo + carry" means the same money backs both. This works because Turbo uses only about 15-20% of equity as margin.

Caveats:
- Carry needs funds on a second exchange (Aster).
- Its profit is concentrated in thinner coins, and the universe was picked by today's volume.
- Fills are at hourly closes.
- At 3x, a ~30% move against one leg would liquidate it; a paired stop is needed.

### Oct 24: carry in the engine, and Turbo turned up (live from Oct 24)
The blend above added daily returns. This re-runs carry as a real bot in `lab/engine.py`: both legs as positions, fees,
slippage, funding paid hour by hour on Hyperliquid and at each settlement on Aster. Turbo then copies it.
Reproduce with `python -m backtest.carry_bt --carry PATH/backtest365 --data CACHE [--turbo]`.

**Carry alone, its paired stop** (close both legs if either moves X against itself, or the two prices drift Y apart):

| stop X / drift Y | return | max drawdown | note |
|---|---|---|---|
| 15% / 3% (first try) | +13.7% | -9.9% | normal noise between hourly closes kept firing the drift guard (528 trades) |
| 25% / none | negative year (-7.9% drawdown) | | one broken pair dragged the year down |
| **25% / 15% (live)** | **+20.4%** | **-4.5%** | halves +16.8% / +3.0% |

**Inside Turbo.** Turbo copies carry at a fixed 50% of its money: no leverage, and never trimmed by its risk controls, since
the two legs hedge each other. Turbo's own copies of the directional bots then get more room:

| setup | $500 became | per day | max drawdown | worst day | TRAIN | TEST |
|---|---|---|---|---|---|---|
| Turbo before (3x, volatility target 1.5%/day), no carry | $1,907 | +0.40% | -13.9% | -6.1% | +77% / -13.9% | +115% / -12.5% |
| **A: 5x, volatility target 1.25%/day + carry (live)** | **$2,343** | **+0.46%** | **-14.3%** | **-8.3%** | +96% / -14.3% | +137% / -13.9% |
| B: 4x, volatility target 1.25%/day + carry (live Oct 24, first hours) | $2,217 | +0.44% | -12.8% | -7.8% | +94% / -12.4% | +127% / -12.8% |
| C: 4x, volatility target 1.0%/day + carry | $1,992 | +0.41% | -11.7% | -7.8% | +85% / -11.3% | +114% / -11.7% |

All four keep the drawdown backstop (full size until 12% below the peak, shrinking to 20% size by 25%). None was liquidated.
Inside Turbo's window the carry bot made +20.1% with a -4.5% drawdown.

B was picked first. Against the old Turbo it makes 16% more money with a drawdown about 8% smaller (-12.8% vs -13.9%). It
improves in both halves, so it is not one lucky stretch. The cost is a harsher worst single day (-7.8% vs -6.1%): more
leverage on the directional copies means a sharp one-day move hits harder before the daily volatility control reacts.
C is the safer choice if that matters more than return; A gives more return for slightly more drawdown. Switching is
one line in `lab/main.py` (`TURBO_LEV`, `TURBO_RISK`).

**Same day: switched to A (5x).** The owner chose more return over the smaller drawdown. Against the old Turbo: 23% more
money ($2,343 vs $1,907) for a drawdown 0.4 points deeper (-14.3% vs -13.9%), still under the 15% limit, and a worst
day of -8.3% (vs -6.1%).

**Oct 28: funding timing fix.** Every carry entry is at 00:00 UTC, which is also an Aster settlement. The engine charged
the settlement at the instant a pair opened (a real account opening just after it would not pay it) and skipped the
one at the instant it closed (a real account still holding would get it). Payments now go to the candle that closes at
the payment time (`carry.charge_map`). All numbers in this section are re-run with the fix; they moved by under 1%
(option A was $2,348). Live, one ANIME settlement on Oct 25 was booked under the old rule (about -$1.60 for carry,
-$0.80 for Turbo); past bookings were left as they are.

Live, the carry bot also runs on its own $500 account, so its record can be checked separately.

### Oct 28: lower timeframes and order flow (one candidate, nothing adopted yet)
**Data:** Binance USDT-M futures 5m candles (with taker-buy volume) and 5m open interest / long-short ratios from
data.binance.vision, Oct 2025 - Oct 2026, the 12 crypto coins the directional bots trade. Train = before 2026-04-21.

**1. Costs at each timeframe.** Median candle move vs the 13 bps round trip (taker fee + slippage, both sides):

| candle | median move | cost as % of a typical move | with maker orders (3 bps) |
|---|---|---|---|
| 5m | 9 bps | 139% | 32% |
| 15m | 17 bps | 79% | 18% |
| 30m | 23 bps | 56% | 13% |
| 1h | 33 bps | 40% | 9% |
| 4h | 65 bps | 20% | 5% |
| 1d | 177 bps | 7% | 2% |

Below 1h, market orders cost about as much as a normal candle moves. Trading there needs maker (limit) orders and an
always-on machine, not the hourly GitHub run.

**2. Lower-timeframe entries for the existing bots.** Their backtest trades replayed on the 5m candles (same exits):

| entry | ms_struct train / test | ta_trend train / test |
|---|---|---|
| at the signal (baseline) | +32.7% / +8.5% | +15.4% / +41.4% |
| wait for a 15m close beyond the last four 15m bars | +31.1% / +1.2% | +11.8% / +34.4% |
| same, stop tucked behind those 15m bars | +7.0% / -8.4% | -17.7% / +40.4% |
| limit order 30% back toward the stop, 4h | +17.9% / +3.9% | +20.0% / +59.1% |

Confirmation on 15m enters later and higher, and tight 15m stops get hit by noise. The pullback limit order helped
ta_trend, so it was run in the engine: alone, ta_trend went from +43% to +77% a year (Sharpe 1.05 -> 1.39), and the
whole 20-40% / 4-8h neighbourhood helped. But inside Turbo (5x + carry), even with Turbo mirroring the limit orders,
the year fell from $2,343 to $2,179 and the drawdown deepened to -15.5%. Not adopted.

**3. Order-flow signals** (15m bars; thresholds fitted on train, signed move after the signal, before costs):
most of 15 ideas flipped sign between halves (aggressive buying/selling, absorption, crowd long/short ratio,
long-liquidation bounces, momentum). One held in both halves: a **short-covering fade**. A sharp 1h rise while open
interest falls means shorts are being forced out rather than new buyers arriving, and the move tends to give back.
On the hourly clock, one trade per event, sell at the next open, hold 4h, net of 13 bps:

| rule | train | test |
|---|---|---|
| 1h rise in top 10%, OI change in bottom 10% | 54 trades, +45 bps avg, 57% win | 48 trades, +57 bps, 60% win |
| top 20% / bottom 20% (wider) | 278 trades, +25 bps, 52% win | 241 trades, +27 bps, 54% win |

10 of 11 coins positive (strict version). The mirror image (buy after a long liquidation) loses. Live, open-interest
history is free from OKX (5m, no key; Binance and Bybit block US servers). Next step: an engine bot, alone and inside
Turbo, before any live use.

## Known biases (read before trusting any number)
1. **Universe chosen with today's information.** The directional list and the top-30 are today's biggest coins. That includes some of last year's big winners (ZEC, HYPE). This flatters trend and momentum strategies.
2. **Spot prices for perps.** The directional backtest uses Binance spot candles; live trades Hyperliquid perps. Funding is real Hyperliquid funding.
3. **Small samples.** ms_struct made 84 trades in 11 months. A few big winners carry it (24% win rate, about 2R average winner).
4. **One year, one market regime.** BTC fell 17% over the period; trend and momentum systems like that.
5. **Costs are modelled, not measured.** The live paper run uses the same costs, so live vs backtest is comparable. Real fills on thin books would be worse.
