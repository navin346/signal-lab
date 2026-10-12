"""Backtest the research bots on cached history with the same engine the live bot uses.

  python -m backtest.run --data /path/to/cache --bot ta_trend --grid
  python -m backtest.run --data /path/to/cache --all

Data cache (made by backtest/fetch.py): bn_1h_<COIN>.json (Binance spot 1h, 400 days),
hl_1h_<COIN>.json (Hyperliquid 1h, last ~208 days), hl_4h_<COIN>.json (Hyperliquid 4h, 400 days),
fund_<COIN>.json (Hyperliquid hourly funding). Candle rows are [t, o, h, l, c, v].

Walk-forward discipline: the year is split in two halves. Parameters are picked on the first
half (TRAIN) only; the second half (TEST) is reported untouched."""
import argparse
import itertools
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lab import engine, strategies as S, universe  # noqa: E402

HOUR, DAY = engine.HOUR, 24 * engine.HOUR
DIRECTIONAL = universe.DIRECTIONAL                      # coins the directional bots trade
TOP30 = universe.FALLBACK_TOP30


def load(path, coins, prefer=("bn_1h", "hl_1h")):
    data, src = {}, {}
    for c in coins:
        safe = c.replace(":", "_")
        for p in prefer:
            f = Path(path) / f"{p}_{safe}.json"
            if f.exists():
                rows = json.loads(f.read_text())
                if len(rows) > 500:
                    data[c] = [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4]} for r in rows]
                    src[c] = p
                    break
    return data, src


def load_funding(path, coins):
    out = {}
    for c in coins:
        f = Path(path) / f"fund_{c.replace(':', '_')}.json"
        if f.exists():
            out[c] = {r[0] // HOUR * HOUR: r[1] for r in json.loads(f.read_text())}
    return lambda c, t: out.get(c, {}).get(t // HOUR * HOUR, 0.0)


def metrics(curve, trades, name, t0, t1, start_eq=None):
    pts = [p for p in curve if t0 <= p["t"] <= t1]
    if len(pts) < 2:
        return None
    eqs = [p["eq"][name] for p in pts]
    e0 = start_eq or eqs[0]
    rets = [eqs[i] / eqs[i - 1] - 1 for i in range(1, len(eqs)) if eqs[i - 1] > 0]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / max(1, len(rets) - 1))
    peak, dd = eqs[0], 0.0
    for e in eqs:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    tr = [t for t in trades if t["strategy"] == name and t0 < t["closed"] <= t1]
    wins = [t["pnl"] for t in tr if t["pnl"] > 0]
    losses = [-t["pnl"] for t in tr if t["pnl"] <= 0]
    rs = [t["r"] for t in tr if t.get("r") is not None]
    return {"ret_pct": round((eqs[-1] / e0 - 1) * 100, 2), "max_dd_pct": round(dd * 100, 2),
            "sharpe": round(mu / sd * math.sqrt(365), 2) if sd else 0.0, "trades": len(tr),
            "win_pct": round(len(wins) / len(tr) * 100, 1) if tr else None,
            "pf": round(sum(wins) / sum(losses), 2) if losses and sum(losses) else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "fees_funding": round(sum(t["funding"] for t in tr), 2)}


def simulate(make, data, funding, t0, t1, ctx=None):
    strat = make()
    strat.prepare(data)
    accts = {strat.name: engine.new_account(500.0)}
    log, curve = [], []
    engine.run([strat], accts, data, funding, t0, t1, log, ctx=ctx, curve=curve, curve_every_h=24)
    return log, curve, strat.name


def span(data, warm_days):
    t_end = min(b[-1]["t"] for b in data.values()) // DAY * DAY
    t_start = t_end - 365 * DAY
    first = min(b[0]["t"] for b in data.values())
    return max(t_start, first + warm_days * DAY), t_end


def evaluate(make, data, funding, warm_days=60, ctx=None):
    t0, t1 = span(data, warm_days)
    mid = t0 + (t1 - t0) // 2
    log, curve, name = simulate(make, data, funding, t0, t1, ctx)
    return {"train": metrics(curve, log, name, t0, mid), "test": metrics(curve, log, name, mid, t1),
            "full": metrics(curve, log, name, t0, t1), "period": [t0, mid, t1]}, log, curve


GRIDS = {
    "ta_trend": dict(
        fast_slow=[(9, 21), (21, 55), (50, 200)], adx_min=[0, 20, 25], atr_mult=[2.0, 3.0, 4.0],
        mode=["ema", "donchian"]),
    "ms_struct": dict(
        min_rr=[1.0, 1.5, 2.0], runner=[True, False], sfp_bias=["daily", "none"],
        setups=[(True, True), (True, False), (False, True)], atr_buf=[0.25, 0.5, 1.0]),
    "ms_struct2": dict(                       # round 2: CHoCH only (SFP lost alone in TRAIN), HTF bias
        choch_bias=["none", "daily"], runner=[True, False], min_rr=[1.5, 2.0], atr_buf=[0.5, 1.0],
        risk_pct=[1.0], max_open=[4], setups=[(True, False)]),
    "news_regime": dict(
        adx=[(25, 20), (30, 20), (20, 15)], trail_mult=[2.0, 3.0], stop_mult=[1.5, 2.0, 3.0],
        legs=[(True, True), (True, False), (False, True)]),
    "rel_value": dict(
        mode=["residual", "momentum", "reversal", "carry"], lookback_d=[1, 3, 7, 14, 28], n_side=[3, 5, 7],
        hold_band=[0, 3]),
}


def maker(bot, coins, funding, **kw):
    if bot == "ta_trend":
        f, s = kw.pop("fast_slow", (21, 55))
        return lambda: S.TATrend(coins, fast=f, slow=s, **kw)
    if bot in ("ms_struct", "ms_struct2"):
        ch, sfp = kw.pop("setups", (True, True))
        return lambda: S.MarketStructure(coins, use_choch=ch, use_sfp=sfp, **kw)
    if bot == "news_regime":
        tr, rg = kw.pop("adx", (25, 20))
        ut, ur = kw.pop("legs", (True, True))
        return lambda: S.NewsRegime(coins, adx_trend=tr, adx_range=rg, use_trend=ut, use_range=ur, **kw)
    if bot == "rel_value":
        mode = kw.get("mode", "residual")
        lb = kw.get("lookback_d", 3)

        def carry_score(c, T, _f=funding, _lb=lb):
            vals = [_f(c, T - k * HOUR) for k in range(1, 24 * _lb + 1)]
            return sum(vals) / len(vals)
        return lambda: S.RelativeValue(coins, funding=carry_score if mode == "carry" else None, **kw)
    raise ValueError(bot)


def grid(bot, data, funding, coins, top=12):
    keys = list(GRIDS[bot])
    rows = []
    for combo in itertools.product(*(GRIDS[bot][k] for k in keys)):
        kw = dict(zip(keys, combo))
        if bot == "rel_value" and kw["mode"] == "carry" and kw["lookback_d"] > 7:
            continue
        res, _, _ = evaluate(maker(bot, coins, funding, **dict(kw)), data, funding, ctx=None)
        rows.append((kw, res))
        tr, te = res["train"], res["test"]
        print(f"{json.dumps(kw):95s} TRAIN ret {tr['ret_pct']:7.2f}% dd {tr['max_dd_pct']:6.2f} sh {tr['sharpe']:5.2f} "
              f"n {tr['trades']:4d} | TEST ret {te['ret_pct']:7.2f}% dd {te['max_dd_pct']:6.2f} sh {te['sharpe']:5.2f} "
              f"n {te['trades']:4d}", flush=True)
    rows.sort(key=lambda r: -r[1]["train"]["sharpe"])
    print("\nBest by TRAIN Sharpe (TEST shown for honesty):")
    for kw, res in rows[:top]:
        print(json.dumps(kw), "TRAIN", res["train"], "\n      TEST ", res["test"])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--bot", choices=list(GRIDS))
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.bot == "rel_value":
        coins = TOP30
        data, src = load(a.data, coins, prefer=("hl_4h",))
    else:
        coins = DIRECTIONAL
        data, src = load(a.data, coins)
    funding = load_funding(a.data, coins)
    print("data:", {c: (src[c], len(v)) for c, v in data.items()})
    if a.grid:
        rows = grid(a.bot, data, funding, list(data))
        if a.out:
            Path(a.out).write_text(json.dumps([{"params": k, **r} for k, r in rows], default=str))


if __name__ == "__main__":
    main()
