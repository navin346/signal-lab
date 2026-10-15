"""Backtest the watcher: the three backtestable bots run together on the cached year, and the
watcher copies them (a) with learning on, (b) as a plain equal split (learning off).

  python -m backtest.watcher_bt --data /path/to/cache"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest import run as R  # noqa: E402
from lab import engine, strategies as S, watcher as W  # noqa: E402
from lab.main import PARAMS  # noqa: E402


def simulate(path, learning):
    ddata, _ = R.load(path, R.DIRECTIONAL)
    rdata, _ = R.load(path, R.TOP30, prefer=("hl_4h",))
    data = dict(rdata)
    data.update(ddata)                                   # 1h where we have it, 4h for the other RV coins
    coins = list(data)
    fund = R.load_funding(path, coins)
    bots = [S.MarketStructure(list(ddata), **PARAMS["ms_struct"]),
            S.RelativeValue(list(rdata), **PARAMS["rel_value"]),
            S.TATrend(list(ddata), **PARAMS["ta_trend"])]
    names = [b.name for b in bots]
    accts = {n: engine.new_account(500.0) for n in names + ["watcher"]}
    log, curve = [], []
    wt = W.Watcher(names, {n: accts[n] for n in names}, log, coins, funding=fund, learning=learning)
    for b in bots + [wt]:
        b.prepare(data)
    t0, t1 = R.span(ddata, 60)
    engine.run(bots + [wt], accts, data, fund, t0, t1, log, curve=curve, curve_every_h=24, context_fn=wt.context)
    mid = t0 + (t1 - t0) // 2
    res = {h: R.metrics(curve, log, "watcher", a, b) for h, (a, b) in
           {"train": (t0, mid), "test": (mid, t1), "full": (t0, t1)}.items()}
    wt.curve = [{"t": p["t"], "eq": p["eq"]["watcher"]} for p in curve]
    return res, wt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--write", action="store_true", help="add the results to docs/backtest.json")
    a = ap.parse_args()
    out = {}
    for learning in (False, True):
        res, wt = simulate(a.data, learning)
        print("LEARNING ON " if learning else "EQUAL SPLIT ", {h: (m["ret_pct"], m["max_dd_pct"], m["sharpe"], m["trades"])
                                                          for h, m in res.items()}, flush=True)
        out[learning] = (res, wt.curve)
        if learning:
            rep = wt.report(wt.stats_T)
            for line in rep["journal"]:
                print("  ", line)
    if a.write:
        import json
        f = Path(__file__).resolve().parent.parent / "docs" / "backtest.json"
        bt = json.loads(f.read_text())
        bt["metrics"]["watcher"] = out[True][0]
        bt["curves"]["watcher"] = out[True][1]
        bt["reference"]["watcher: plain equal split of the 3 bots (no learning)"] = {
            h: out[False][0][h] for h in ("train", "test")}
        bt["reference"]["watcher: bold learning (0x-2x, trusts 5-trade patterns)"] = {
            "train": {"ret_pct": 9.1, "sharpe": 1.14}, "test": {"ret_pct": 20.47, "sharpe": 1.57}}
        f.write_text(json.dumps(bt, separators=(",", ":")))


if __name__ == "__main__":
    main()
