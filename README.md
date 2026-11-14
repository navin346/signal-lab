# Signal Lab

**Which trading ideas actually beat a coin flip?** A paper-trading research lab that runs on GitHub Actions
for free and publishes a live dashboard on GitHub Pages. **Paper only: no exchange keys, no real orders.**

**Live dashboard:** https://navin346.github.io/signal-lab/

## The bots (v2)
Retired Nov 3 (history kept on the dashboard): market structure (-78% over six years), news + regime (never traded
without the paid AI analyst), watcher (re-sized copies; learning added nothing).

Each starts with a virtual **$500**; fees, slippage and Hyperliquid funding are charged.

| Bot | How it trades | Markets |
|---|---|---|
| **Relative value** | Market-neutral. The top-30 coins by market cap that trade on Hyperliquid; daily long the 5 strongest and short the 5 weakest over 3 days. | top 30 (CoinPaprika, refreshed daily) |
| **TA trend** | 4H Donchian breakout (about 33 days) with an ADX trend filter, 2 ATR trailing stop, 1% risk. The backtest picked it over 4H EMA crossovers. | same |
| **Trend 2x (growth, Oct 30)** | The TA trend rules at 2% risk per trade instead of 1%. Six-year backtest (top-5 crypto + gold, 2020-2026): +37% a year, every year positive, worst drop -43%. | same as TA trend |
| **Rotation (Oct 30)** | Once a day: hold BTC while it is above its 100-day average, else gold while gold is above its own, else cash. No leverage. Six-year backtest: +45% a year, worst drop -47% (holding BTC: +34% a year, -77%). | BTC, gold |
| **Chart reader (Nov 14)** | Reads each chart four ways (price vs the 100/200 EMA on 4H; 9/21 EMA on 4H and 12H; 20/50 EMA on the daily) and trades, long or short, when at least three agree; up to five markets; each stop costs 1% of the account. Six-year backtest: +38% a year, every year positive, worst drop -56%. | same as TA trend |
| **Fast lane: squeeze fade (Nov 14, live test)** | Always on (`lab/fast.py`, workflow `fast`). When a coin jumps within an hour while open interest falls, sells within seconds of the hour and buys back an hour later at real Hyperliquid bid/ask prices. Backtest: +16 to +18 bps a trade if sold within a minute, nothing after four minutes. Tests whether the edge survives real costs. | Hyperliquid perps with $1M+ daily volume |
| **Funding carry** | No price bet. For coins on both Hyperliquid and Aster, short where funding is higher and buy where it is lower, equal dollars, so price moves cancel and the funding gap is collected. Holds the 5 biggest 7-day gaps (20%+ a year), re-checked every 3 days, 3x margin. Both legs close together if either moves 25% against itself or the two prices drift 15% apart. | the 40 most liquid coins on both exchanges |
| **Turbo** | Copies relative value and TA trend, each on a third of its money at 5x, plus the carry bot on 50% of its money (no leverage, never trimmed). Risk controls: shrinks every directional position when its own daily swings exceed 1.25%, and below 12% from its peak keeps shrinking toward 20% size by a 25% drawdown. Liquidation is modelled. Backtest: +0.46%/day, worst drawdown -14.3%. | whatever the bots trade |
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
