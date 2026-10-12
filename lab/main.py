"""One run = one hourly tick. GitHub Actions calls this every hour and commits docs/data.

  python -m lab.main            # normal tick
  python -m lab.main --decide   # force a fresh daily decision now (testing)
  python -m lab.main --reset    # wipe paper state and start over"""
import argparse
import csv
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from lab import config as C
from lab import learn, market, news, paper, signals

OUT = Path(__file__).resolve().parent.parent / "docs" / "data"
HOUR = 3_600_000


BACKUPS = OUT / "backups"
KEEP_BACKUPS = 30          # one snapshot per day


def load_state():
    """Current state; if it is missing or corrupt, fall back to the newest good daily backup."""
    candidates = [OUT / "state.json"] + sorted(BACKUPS.glob("state-*.json"), reverse=True)
    for f in candidates:
        try:
            if f.exists():
                st = json.loads(f.read_text())
                if f.name != "state.json":
                    print(f"state.json unreadable, restored from backup {f.name}")
                return st
        except (json.JSONDecodeError, OSError):
            continue
    return {"created": int(time.time() * 1000), "last_tick": None, "last_decision": None,
            "accounts": {s: paper.new_account() for s in signals.DESCRIPTIONS},
            "trades": [], "curve": [], "events": [], "matrix": {}, "seen_news": [],
            "last_signals": {}, "last_features": {}, "weights": {}, "news_feed": [], "errors": {}}


def stats(state):
    out = {}
    for s, acct in state["accounts"].items():
        tr = [t for t in state["trades"] if t["strategy"] == s]
        curve = [p["eq"][s] for p in state["curve"] if s in p["eq"]]
        peak, dd = C.START_EQUITY, 0.0
        for e in curve:
            peak = max(peak, e)
            dd = min(dd, e / peak - 1)
        eq = curve[-1] if curve else C.START_EQUITY
        out[s] = {"equity": round(eq, 2), "return_pct": round((eq / C.START_EQUITY - 1) * 100, 2),
                  "trades": len(tr), "win_pct": round(sum(t["pnl"] > 0 for t in tr) / len(tr) * 100, 1) if tr else None,
                  "avg_pnl": round(sum(t["pnl"] for t in tr) / len(tr), 3) if tr else None,
                  "max_dd_pct": round(dd * 100, 2), "open": len(acct["positions"]),
                  "description": signals.DESCRIPTIONS[s]}
    return out


def tick(force_decide=False):
    now = int(time.time() * 1000)
    state = load_state()
    errors = {}
    coins = {v["coin"]: k for k, v in C.ASSETS.items()}

    ctx = market.contexts(set(coins))
    marks = {coins[c]: v["mark"] for c, v in ctx.items()}
    funding = {coins[c]: v["funding"] for c, v in ctx.items()}
    bars, fund_hist = {}, {}
    for a, cfg in C.ASSETS.items():
        try:
            bars[a] = market.candles(cfg["coin"], 24 * 8 + 2)
            fund_hist[a] = market.funding_history(cfg["coin"], 30)
        except Exception as e:
            errors[f"market:{a}"] = str(e)[:120]
            bars[a], fund_hist[a] = [], []

    # ---- news: new headlines -> themes -> event log; fill in reactions of older events
    try:
        items, nerr = news.fetch_headlines()
        errors.update({f"news:{k}": v for k, v in nerr.items()})
    except Exception as e:
        items, errors["news"] = [], str(e)[:120]
    seen = set(state["seen_news"])
    fresh = [it for it in items if it["id"] not in seen]
    themed = []
    for it in fresh:
        it["themes"] = news.tag(it["title"])
        if it["themes"]:
            themed.append(it)
    learn.log_events(state, themed, marks, now)
    learn.fill_returns(state, bars, now)
    state["seen_news"] = (state["seen_news"] + [it["id"] for it in fresh])[-6000:]
    state["news_feed"] = (sorted(themed, key=lambda x: -x["ts"]) + state["news_feed"])[:150]

    # ---- risk: stops / targets on the hourly path, then funding since the last tick
    last = state["last_tick"] or now
    hours = max(0.0, (now - last) / HOUR)
    for s, acct in state["accounts"].items():
        for a in list(acct["positions"]):
            since = [b for b in bars.get(a, []) if b["t"] + HOUR > last]
            paper.check_stops(acct, a, since, state["trades"], s)
            if a in acct["positions"] and a in marks:
                paper.accrue_funding(acct, a, funding.get(a, 0.0), hours, marks[a])

    # ---- daily decision
    dt = datetime.fromtimestamp(now / 1000, tz=timezone.utc)
    day_key = dt.strftime("%Y-%m-%d") if dt.hour >= C.DECISION_HOUR_UTC else None
    recent = [e for e in state["events"] if now - e["seen"] <= C.NEWS_LOOKBACK_H * HOUR]
    nscore = signals.news_scores(recent, state["matrix"])
    feat = {a: signals.features(a, bars.get(a, []), fund_hist.get(a, []), nscore) for a in C.ASSETS}
    if force_decide or (day_key and day_key != state["last_decision"]):
        weights = signals.learn_weights(state["trades"])
        decision = signals.decide(feat, weights, day_key or dt.strftime("%Y-%m-%d"))
        for s, targets in decision.items():
            paper.rebalance(state["accounts"][s], targets, marks, now, state["trades"], s)
        state["last_decision"] = day_key
        state["last_signals"] = decision
        state["weights"] = weights
        state["decided_at"] = now
    state["last_features"] = {a: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in f.items()}
                              for a, f in feat.items()}

    # ---- bookkeeping
    state["curve"].append({"t": now, "eq": {s: round(paper.equity(acct, marks), 2)
                                            for s, acct in state["accounts"].items()}})
    state["curve"] = state["curve"][-24 * 400:]
    state["trades"] = state["trades"][-5000:]
    state["last_tick"] = now
    state["errors"] = errors
    state["marks"] = marks
    write(state)
    return state


def _p(x):
    return f"{x:.6g}" if isinstance(x, (int, float)) else x


def _utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def write_csv(state):
    """Every trade with UTC timestamps and prices, for cross-checking on the exchange."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["strategy", "asset", "hyperliquid_market", "side", "opened_utc", "entry_price", "closed_utc",
                "exit_price", "move_pct", "pnl_usd", "funding_usd", "reason"])
    for t in state["trades"]:
        w.writerow([t["strategy"], t["asset"], C.ASSETS[t["asset"]]["coin"], t["side"], _utc(t["opened"]), _p(t["entry"]),
                    _utc(t["closed"]), _p(t["exit"]), t["move_pct"], t["pnl"], t["funding"], t["reason"]])
    (OUT / "trades.csv").write_text(buf.getvalue())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["strategy", "asset", "hyperliquid_market", "side", "opened_utc", "entry_price", "stop_price",
                "target_price", "mark_now", "funding_usd_so_far"])
    for s, acct in state["accounts"].items():
        for a, p in acct["positions"].items():
            w.writerow([s, a, C.ASSETS[a]["coin"], "long" if p["side"] > 0 else "short", _utc(p["opened"]),
                        _p(p["entry"]), _p(p["stop"]), _p(p["target"]), _p(state.get("marks", {}).get(a)),
                        round(p["funding"], 4)])
    (OUT / "positions.csv").write_text(buf.getvalue())


def _atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)                    # never leaves a half-written file behind


def write(state):
    OUT.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(state, separators=(",", ":"))
    _atomic(OUT / "state.json", blob)
    BACKUPS.mkdir(exist_ok=True)
    day = datetime.fromtimestamp(state["last_tick"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    _atomic(BACKUPS / f"state-{day}.json", blob)     # latest snapshot of each day
    for old in sorted(BACKUPS.glob("state-*.json"))[:-KEEP_BACKUPS]:
        old.unlink()
    write_csv(state)
    matrix = {th: {a: {k: v for k, v in c.items() if k in ("n", "mean_24h", "sd_24h", "hits")}
                   for a, c in cells.items()} for th, cells in state["matrix"].items()}
    summary = {
        "updated": state["last_tick"], "decided_at": state.get("decided_at"), "config": {
            "start_equity": C.START_EQUITY, "leverage": C.LEVERAGE, "stop_pct": C.STOP_PCT,
            "target_pct": C.TARGET_PCT, "assets": {a: v["label"] for a, v in C.ASSETS.items()}},
        "stats": stats(state), "curve": state["curve"][::max(1, len(state["curve"]) // 1500)],
        "positions": {s: acct["positions"] for s, acct in state["accounts"].items()},
        "signals": state["last_signals"], "weights": state["weights"], "features": state["last_features"],
        "marks": state.get("marks", {}), "trades": state["trades"][-400:][::-1],
        "markets": {a: v["coin"] for a, v in C.ASSETS.items()},
        "news": state["news_feed"][:80], "matrix": matrix,
        "priors": {th: v["prior"] for th, v in news.THEMES.items()},
        "events_logged": len(state["events"]), "errors": state["errors"],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, separators=(",", ":")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decide", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    if a.reset and (OUT / "state.json").exists():
        (OUT / "state.json").unlink()
    s = tick(force_decide=a.decide)
    st = stats(s)
    print(f"tick {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC | " +
          " | ".join(f"{k} ${v['equity']:.2f} ({v['trades']}t)" for k, v in st.items()))
    if s["errors"]:
        print("errors:", s["errors"])


if __name__ == "__main__":
    main()
