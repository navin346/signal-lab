"""Re-run the live configurations (lab/main.py PARAMS) plus reference variants on the cached
year and write BACKTEST_RESULTS.json (metrics) and docs/backtest.json (daily equity curves for
the dashboard).

  python -m backtest.report --data /path/to/cache"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest import run as R  # noqa: E402
from lab import strategies as S  # noqa: E402
from lab.main import PARAMS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

REFERENCE = {   # what was asked for / first designs, kept for comparison
    "ta_trend: 4H EMA 21/55 cross + ADX 20 (the original ask)": ("ta_trend", dict(mode="ema", fast=21, slow=55,
                                                                                    adx_min=20.0, atr_mult=3.0)),
    "ta_trend: 4H EMA 9/21 cross, no filter": ("ta_trend", dict(mode="ema", fast=9, slow=21, adx_min=0, atr_mult=2.0)),
    "ms_struct: CHoCH + SFP, runner (first design)": ("ms_struct", dict()),
    "ms_struct: SFP only": ("ms_struct", dict(use_choch=False, use_sfp=True, runner=False, min_rr=2.0, risk_pct=1.0,
                                              max_open=4)),
    "rel_value: BTC-beta residual reversal 3d (first design)": ("rel_value", dict(mode="residual", lookback_d=3)),
    "rel_value: funding carry 7d": ("rel_value", dict(mode="carry", lookback_d=7, n_side=5, hold_band=3)),
    "rel_value: momentum 7d": ("rel_value", dict(mode="momentum", lookback_d=7, n_side=5, hold_band=3)),
}


def make(bot, coins, funding, kw):
    kw = dict(kw)
    if bot == "ta_trend":
        return lambda: S.TATrend(coins, **kw)
    if bot == "ms_struct":
        return lambda: S.MarketStructure(coins, **kw)
    if bot == "rel_value":
        return R.maker("rel_value", coins, funding, **kw)      # handles the funding-carry score
    raise ValueError(bot)


def buy_hold(data, coin, t0, t1):
    bars = [b for b in data[coin] if t0 <= b["t"] + R.HOUR <= t1 and (b["t"] + R.HOUR) % R.DAY == 0]
    return [{"t": b["t"] + R.HOUR, "eq": round(500 * b["c"] / bars[0]["c"], 2)} for b in bars]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    a = ap.parse_args()
    ddata, _ = R.load(a.data, R.DIRECTIONAL)
    dfund = R.load_funding(a.data, R.DIRECTIONAL)
    rdata, _ = R.load(a.data, R.TOP30, prefer=("hl_4h",))
    rfund = R.load_funding(a.data, R.TOP30)
    out, curves = {"live": {}, "reference": {}}, {}
    for bot in ("ms_struct", "ta_trend", "rel_value"):
        data, fund = (rdata, rfund) if bot == "rel_value" else (ddata, dfund)
        res, log, curve = R.evaluate(make(bot, list(data), fund, PARAMS[bot]), data, fund)
        out["live"][bot] = {"params": PARAMS[bot], **res}
        curves[bot] = [{"t": p["t"], "eq": p["eq"][bot]} for p in curve]
        t0, mid, t1 = res["period"]
        print(bot, "TRAIN", res["train"], "\n   TEST", res["test"], "\n   FULL", res["full"], flush=True)
    for label, (bot, kw) in REFERENCE.items():
        data, fund = (rdata, rfund) if bot == "rel_value" else (ddata, dfund)
        res, _, _ = R.evaluate(make(bot, list(data), fund, kw), data, fund)
        out["reference"][label] = {"params": kw, **res}
        print(label, "TRAIN", res["train"]["sharpe"], res["train"]["ret_pct"], "TEST", res["test"]["sharpe"],
              res["test"]["ret_pct"], flush=True)
    t0, mid, t1 = out["live"]["ta_trend"]["period"]
    curves["BTC buy & hold"] = buy_hold(ddata, "BTC", t0, t1)
    (ROOT / "backtest" / "BACKTEST_RESULTS.json").write_text(json.dumps(out, indent=1))
    (ROOT / "docs" / "backtest.json").write_text(json.dumps(
        {"period": [t0, mid, t1], "curves": curves,
         "metrics": {k: {h: v[h] for h in ("train", "test", "full")} for k, v in out["live"].items()},
         "reference": {k: {h: v[h] for h in ("train", "test")} for k, v in out["reference"].items()}},
        separators=(",", ":")))


if __name__ == "__main__":
    main()
