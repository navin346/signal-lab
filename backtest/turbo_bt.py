"""Backtest the turbo bot (levered copies of the backtestable bots) in the real engine.

  python -m backtest.turbo_bt --data /path/to/cache"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest import run as R  # noqa: E402
from lab import engine, strategies as S, watcher as W  # noqa: E402
from lab.main import PARAMS  # noqa: E402


def simulate(path, lev, brake_dd=0.30, share=None, keep_curve=False):
    ddata, _ = R.load(path, R.DIRECTIONAL)
    rdata, _ = R.load(path, R.TOP30, prefer=("hl_4h",))
    data = dict(rdata)
    data.update(ddata)
    coins = list(data)
    fund = R.load_funding(path, coins)
    rv = {k: v for k, v in PARAMS["rel_value"].items() if k not in ("breaker_pct", "breaker_action")}
    bots = [S.MarketStructure(list(ddata), **PARAMS["ms_struct"]), S.RelativeValue(list(rdata), **rv),
            S.TATrend(list(ddata), **PARAMS["ta_trend"])]
    names = [b.name for b in bots]
    accts = {n: engine.new_account(500.0) for n in names + ["turbo"]}
    log, curve = [], []
    tb = W.Turbo(names, {n: accts[n] for n in names}, log, coins, funding=fund, lev=lev, brake_dd=brake_dd)
    tb.share = share or 1 / len(names)              # 1x = the three bots side by side, each a third
    for b in bots + [tb]:
        b.prepare(data)
    t0, t1 = R.span(ddata, 60)
    engine.run(bots + [tb], accts, data, fund, t0, t1, log, curve=curve, curve_every_h=24, context_fn=tb.context)
    mid = t0 + (t1 - t0) // 2
    res = {h: R.metrics(curve, log, "turbo", a, b) for h, (a, b) in
           {"train": (t0, mid), "test": (mid, t1), "full": (t0, t1)}.items()}
    eq = [p["eq"]["turbo"] for p in curve]
    days = len(eq) - 1
    res["per_day"] = round(((eq[-1] / eq[0]) ** (1 / days) - 1) * 100, 3) if eq[-1] > 0 else -100
    res["final"] = round(eq[-1], 2)
    res["liquidations"] = accts["turbo"]["mem"].get("liquidations", 0)
    if keep_curve:
        res["curve"] = [{"t": p["t"], "eq": p["eq"]["turbo"]} for p in curve]
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--write", type=float, help="leverage whose curve and metrics go into docs/backtest.json")
    a = ap.parse_args()
    if a.write:
        import json
        r = simulate(a.data, a.write, 0.30, keep_curve=True)
        f = Path(__file__).resolve().parent.parent / "docs" / "backtest.json"
        bt = json.loads(f.read_text())
        bt["metrics"]["turbo"] = {h: r[h] for h in ("train", "test", "full")}
        bt["curves"]["turbo"] = r["curve"]
        f.write_text(json.dumps(bt, separators=(",", ":")))
        print("written", r["final"], r["per_day"])
        return
    for lev, brake in ((4, 0.30), (6, 0.30), (8, 1.0), (8, 0.30), (10, 0.30)):
        r = simulate(a.data, lev, brake)
        print(f"lev {lev}x brake {'off' if brake >= 1 else f'{brake:.0%}'}: $500 -> ${r['final']:,.0f}  "
              f"{r['per_day']:+.2f}%/day  maxDD {r['full']['max_dd_pct']}%  TRAIN {r['train']['ret_pct']}% "
              f"TEST {r['test']['ret_pct']}%  liquidations {r['liquidations']}", flush=True)


if __name__ == "__main__":
    main()
