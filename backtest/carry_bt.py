"""Backtest the funding-carry bot (Hyperliquid x Aster) in the live engine, alone and inside Turbo.

  python -m backtest.carry_bt --carry PATH/backtest365 --data PATH/cache [--turbo]

--carry: per-symbol files from FundingrateArbitrage `backtest/fetch.py --days 365` (hourly funding on
both exchanges, Aster hourly prices, Hyperliquid hourly prices for the last ~208 days).
--data: the signal-lab cache (hl_4h_<COIN>.json fills Hyperliquid prices before that)."""
import argparse
import bisect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest import run as R  # noqa: E402
from lab import engine, strategies as S, watcher as W  # noqa: E402
from lab.carry import FundingBook, FundingCarry, norm_sym  # noqa: E402

HOUR = engine.HOUR


def _flat(t, c):
    return {"t": t, "o": c, "h": c, "l": c, "c": c}


def load_carry(carry_dir, cache_dir):
    """Returns pairs, FundingBook, candles {instrument: [...]}, funding {instrument: {hour: rate}}."""
    pairs, book, candles, fund = [], FundingBook(), {}, {}
    for f in sorted(Path(carry_dir).glob("*.json")):
        r = json.loads(f.read_text())
        if not (r["hl_funding"] and r["ast_funding"] and r["ast_px"]):
            continue
        sym = norm_sym(r["ast"])[0]
        hl_key, ast_key = "hlc:" + r["hl"], "ast:" + r["ast"]
        # Hyperliquid hourly closes, earlier hours filled from 4h candles
        hourly = {t + HOUR: c for t, c in r["hl_px"]}
        f4 = Path(cache_dir) / f"hl_4h_{r['hl'].replace(':', '_')}.json"
        if f4.exists():
            for row in json.loads(f4.read_text()):            # a 4h close is known only once the candle ends
                for k in range(4):
                    hourly.setdefault(row[0] + 4 * HOUR + k * HOUR, row[4])
        if len(hourly) < 500:
            continue
        candles[hl_key] = [_flat(t - HOUR, hourly[t]) for t in sorted(hourly)]
        candles[ast_key] = [_flat(t, c) for t, c in r["ast_px"]]
        book.hl[sym] = {t // HOUR * HOUR: x for t, x in r["hl_funding"]}
        ev, prev = [], None
        for t, x in sorted(r["ast_funding"]):
            ih = (t - prev) / HOUR if prev else r["ast_interval_h"]
            prev = t
            ev.append((t, x, max(1.0, ih)))
        book.ast[sym] = ev
        fund[hl_key] = book.hl[sym]
        fund[ast_key] = {t // HOUR * HOUR: x for t, x, _ in ev}       # paid once, at the settlement hour
        pairs.append({"sym": sym, "hl": hl_key, "ast": ast_key, "hl_mult": r["hl_mult"], "ast_mult": r["ast_mult"]})
    return pairs, book, candles, fund


def run_carry_alone(carry_dir, cache_dir):
    pairs, book, candles, fund = load_carry(carry_dir, cache_dir)
    bot = FundingCarry(pairs, book)
    accts = {"carry": engine.new_account(500.0)}
    log, curve = [], []
    t_end = min(c[-1]["t"] for c in candles.values() if len(c) > 5000) // R.DAY * R.DAY
    t0 = t_end - 335 * R.DAY
    engine.run([bot], accts, candles, lambda c, t: fund.get(c, {}).get(t, 0.0), t0, t_end, log, curve=curve,
               curve_every_h=24)
    return log, curve, t0, t_end


def turbo_with_carry(carry_dir, cache_dir, lev, vt, carry_share=0.5):
    from lab.main import PARAMS
    pairs, book, ccandles, cfund = load_carry(carry_dir, cache_dir)
    ddata, _ = R.load(cache_dir, R.DIRECTIONAL)
    rdata, _ = R.load(cache_dir, R.TOP30, prefer=("hl_4h",))
    data = dict(rdata)
    data.update(ddata)
    coins = list(data)
    f_dir = R.load_funding(cache_dir, coins)
    data.update(ccandles)

    def funding(c, t):
        return cfund[c].get(t, 0.0) if c in cfund else f_dir(c, t)
    rv = {k: v for k, v in PARAMS["rel_value"].items() if k not in ("breaker_pct", "breaker_action")}
    bots = [S.MarketStructure(list(ddata), **PARAMS["ms_struct"]), S.RelativeValue(list(rdata), **rv),
            S.TATrend(list(ddata), **PARAMS["ta_trend"])]
    names = [b.name for b in bots]
    if carry_share:
        bots.append(FundingCarry(pairs, book))
    srcs = [b.name for b in bots]
    accts = {n: engine.new_account(500.0) for n in srcs + ["turbo"]}
    log, curve = [], []
    tb = W.Turbo(srcs, {n: accts[n] for n in srcs}, log, list(data), funding=funding, lev=lev, brake_dd=1.0,
                 vol_target=vt, dd_start=0.12, dd_budget=0.25, min_scale=0.2)
    tb.share = 1 / 3
    if carry_share:
        tb.fixed_share = {"carry": carry_share}
    for b in bots + [tb]:
        b.prepare(data)
    t0, t1 = R.span(ddata, 60)
    engine.run(bots + [tb], accts, data, funding, t0, t1, log, curve=curve, curve_every_h=24, context_fn=tb.context)
    mid = t0 + (t1 - t0) // 2
    res = {h: R.metrics(curve, log, "turbo", a, b) for h, (a, b) in
           {"train": (t0, mid), "test": (mid, t1), "full": (t0, t1)}.items()}
    eq = [p["eq"]["turbo"] for p in curve]
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    res.update(final=eq[-1], worst_day=min(rets) * 100, per_day=((eq[-1] / eq[0]) ** (1 / len(rets)) - 1) * 100,
               liquidations=accts["turbo"]["mem"].get("liquidations", 0), curve=[{"t": p["t"], "eq": p["eq"]["turbo"]} for p in curve],
               carry=({h: R.metrics(curve, log, "carry", a, b) for h, (a, b) in
                       {"train": (t0, mid), "test": (mid, t1), "full": (t0, t1)}.items()} if carry_share else None),
               carry_curve=[{"t": p["t"], "eq": p["eq"]["carry"]} for p in curve] if carry_share else None)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--carry", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--turbo", action="store_true")
    ap.add_argument("--write", help="lev,vt of the Turbo+carry curve to store in docs/backtest.json")
    a = ap.parse_args()
    if not a.turbo and not a.write:
        log, curve, t0, t1 = run_carry_alone(a.carry, a.data)
        m = R.metrics(curve, log, "carry", t0, t1)
        mid = t0 + (t1 - t0) // 2
        print("carry alone:", m, "| halves", R.metrics(curve, log, "carry", t0, mid)["ret_pct"],
              R.metrics(curve, log, "carry", mid, t1)["ret_pct"])
        reasons = {}
        for t in log:
            reasons[t["reason"].split(" (")[0]] = reasons.get(t["reason"].split(" (")[0], 0) + 1
        print("exits:", reasons)
        return
    if a.write:
        lev, vt = (float(x) for x in a.write.split(","))
        r = turbo_with_carry(a.carry, a.data, lev, vt)
        f = Path(__file__).resolve().parent.parent / "docs" / "backtest.json"
        bt = json.loads(f.read_text())
        bt["metrics"]["turbo"] = {h: r[h] for h in ("train", "test", "full")}
        bt["curves"]["turbo"] = r["curve"]
        bt["metrics"].pop("carry_note", None)
        if r["carry"]:
            bt["metrics"]["carry"] = r["carry"]
            bt["curves"]["carry"] = r["carry_curve"]
        f.write_text(json.dumps(bt, separators=(",", ":")))
        print("written", round(r["final"]), r["full"])
        return
    for label, lev, vt, cs in (("today's Turbo (3x, vol 1.5%), no carry", 3.0, 0.015, 0),
                               ("A: 5x, vol 1.25% + carry", 5.0, 0.0125, 0.5),
                               ("B: 4x, vol 1.25% + carry", 4.0, 0.0125, 0.5),
                               ("C: 4x, vol 1.0% + carry", 4.0, 0.010, 0.5)):
        r = turbo_with_carry(a.carry, a.data, lev, vt, cs)
        print(f"{label:40s} ${r['final']:,.0f} {r['per_day']:+.2f}%/day maxDD {r['full']['max_dd_pct']}% worst day "
              f"{r['worst_day']:.1f}% TRAIN {r['train']['ret_pct']}%/{r['train']['max_dd_pct']} TEST {r['test']['ret_pct']}%/"
              f"{r['test']['max_dd_pct']} liq {r['liquidations']}" + (f" | carry bot {r['carry']['full']['ret_pct']}% DD {r['carry']['full']['max_dd_pct']}%" if r["carry"] else ""), flush=True)


if __name__ == "__main__":
    main()
