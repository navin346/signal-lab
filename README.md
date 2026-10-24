# Signal Lab

**Which trading ideas actually beat a coin flip?** A paper-trading research lab that runs on GitHub Actions
for free and publishes a live dashboard on GitHub Pages. **Paper only: no exchange keys, no real orders.**

**Live dashboard:** https://navin346.github.io/signal-lab/

## The bots (v2)
Each starts with a virtual **$500**; fees, slippage and Hyperliquid funding are charged.

| Bot | How it trades | Markets |
|---|---|---|
| **Market structure** | EmperorBTC method (the five PDFs). Trade only with the daily and 4H structure (BOS, protected HL/LH). After a break, wait for the pullback, then enter on the 1H change of character. Stop beyond the pullback plus 1 ATR, target the previous swing (min 2R), 1% risk. Strong opposite news vetoes the entry. | 12 big crypto perps + gold, WTI oil, Nasdaq-100 |
| **Relative value** | Market-neutral. The top-30 coins by market cap that trade on Hyperliquid; daily long the 5 strongest and short the 5 weakest over 3 days. | top 30 (CoinPaprika, refreshed daily) |
| **News + regime** | The AI news analyst decides whether and which way to trade. The ADX regime (trend / range) sets the stop, target and holding time, ATR-scaled. Never trades on technicals alone. | same as market structure |
| **TA trend** | 4H Donchian breakout (about 33 days) with an ADX trend filter, 2 ATR trailing stop, 1% risk. The backtest picked it over 4H EMA crossovers. | same |
| **Watcher (self-learning)** | Records the conditions around every trade the four bots take (trend/range, with/against the daily structure, volatility, news, funding, time of day), learns which bot wins where, and copies their trades into its own account with a 0.5x–1.5x size tilt. It starts as an equal split. Free: statistics, no AI service. | whatever the bots trade |
| **Funding carry** | No price bet. For coins on both Hyperliquid and Aster, short where funding is higher and buy where it is lower, equal dollars, so price moves cancel and the funding gap is collected. Holds the 5 biggest 7-day gaps (20%+ a year), re-checked every 3 days, 3x margin. Both legs close together if either moves 25% against itself or the two prices drift 15% apart. | the 40 most liquid coins on both exchanges |
| **Turbo (growth)** | Copies the four bots, each on a third of its money at 4x, plus the carry bot on 50% of its money (no leverage, never trimmed). Risk controls: shrinks every directional position when its own daily swings exceed 1.25%, and below 12% from its peak keeps shrinking toward 20% size by a 25% drawdown. Liquidation is modelled. Backtest: +0.44%/day, worst drawdown -12.8%. | whatever the bots trade |
| Coin flip, Always long | **controls**: a bot matters only if it beats both over 100+ trades | BTC, ETH, SOL, gold, oil, Nasdaq |

How each was chosen, what lost, and the biases: **[BACKTEST.md](BACKTEST.md)**.

**Engine.** `lab/engine.py` is shared by the backtest and the live bot. Every run replays each hourly candle since the last run, so GitHub's late scheduler never changes a decision or a fill. Stops and targets are checked on candle highs and lows, and the stop wins when a candle touches both.

**AI news analyst** (`lab/analyst.py`) reads fresh headlines and returns, for each one that matters, the markets it should move, the direction, a confidence and a one-line reason. It runs on Claude when the optional `ANTHROPIC_API_KEY` repository secret is set; otherwise it falls back to keyword themes.
- To switch the model, set the repository variable `NEWS_MODEL` (default `claude-opus-5-5`; `claude-haiku-5-5` is far cheaper).
- Requests use the API's server-side refusal fallback.

## Run locally
```bash
pip install -r requirements.txt
python tests/test_core.py && python tests/test_engine.py
python -m backtest.run --data CACHE --bot ta_trend --grid   # cache layout in backtest/run.py
python -m lab.main --reset         # fresh state, first tick
cd docs && python -m http.server   # open http://localhost:8000
```
Live bot settings: `PARAMS` in `lab/main.py`; strategy rules: `lab/strategies.py`.

## Deploy (already wired)
* `.github/workflows/tick.yml` runs hourly and force-pushes `docs/` (dashboard + `data/`) to the
  `gh-pages` branch (one commit, so the repo never grows).
* Settings -> Pages -> Source: **Deploy from a branch**, branch **gh-pages**, folder **/ (root)**.
* GitHub pauses scheduled workflows after 60 days without activity on the repo; if the dashboard stops
  updating, re-enable it in the Actions tab or push any commit.

## Honest limits
- Backtests cover one year and use today's coin list (survivor bias).
- News can't be backtested.
- Stops are checked on hourly highs/lows, not ticks.
- Early live results are noise: judge only after 100+ trades per bot.

Not investment advice.
