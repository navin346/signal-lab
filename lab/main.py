"""One run = one tick. GitHub Actions calls this hourly (in practice every 1-8h, GitHub's
scheduler is often late) and publishes docs/data to GitHub Pages.

Each tick replays every hourly candle since the previous tick through the research bots
(lab/engine.py), so a late runner never changes a decision or a fill price.

  python -m lab.main            # normal tick
  python -m lab.main --reset    # wipe paper state and start over"""
import argparse
import csv
import io
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from lab import analyst, carry, engine, learn, market, news, paper, signals, universe
from lab import config as C
from lab import strategies as S
from lab import watcher as W

OUT = Path(__file__).resolve().parent.parent / "docs" / "data"
BACKUPS = OUT / "backups"
KEEP_BACKUPS = 30          # one snapshot per day
HOUR = 3_600_000
MAX_REPLAY_H = 72          # never replay more than 3 days (e.g. after a long outage)
CONTROLS = ["coin_flip", "buy_hold"]
RETIRED = ["momentum_3d", "momentum_7d", "crowd_follow", "crowd_fade", "news", "combo"]
LEGACY = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "GOLD": "xyz:GOLD", "OIL": "xyz:CL", "NASDAQ": "xyz:XYZ100"}

# Picked from the 12-month backtest (see BACKTEST.md for how, and for what failed).
PARAMS = {
    "ms_struct": dict(use_choch=True, use_sfp=False, choch_bias="daily", runner=False, min_rr=2.0, atr_buf=1.0,
                      risk_pct=1.0, max_open=4),
    "ta_trend": dict(mode="donchian", fast=50, slow=200, adx_min=20.0, atr_mult=2.0, risk_pct=1.0),
    "news_regime": dict(),
    # Oct 17: 7-day lookback + skip extreme movers (|z| > 2) after the Oct 15 short squeeze; see BACKTEST.md
    # Oct 18: + volatility targeting (shrinks new legs when the book is swinging more than usual)
    "rel_value": dict(mode="momentum", lookback_d=7, n_side=5, hold_band=3, gross_lev=2.0, max_z=2.0, vol_scale=True,
                      breaker_pct=5.0, breaker_action="half",    # Oct 19: hourly circuit breaker, see BACKTEST.md
                      long_regime_d=100),                         # Nov 3: new longs only while BTC > its 100-day average
}


def build(top30, held_rv):
    rv_coins = list(dict.fromkeys(top30 + held_rv))
    return [S.RelativeValue(rv_coins, **PARAMS["rel_value"]), S.TATrend(universe.DIRECTIONAL, **PARAMS["ta_trend"])]


BOTS = ["rel_value", "ta_trend"]
ACTIVE = BOTS + ["carry", "turbo", "trend_2x", "rotation", "confluence", "trend_pro"]
# Nov 15: Trend Pro. The trend rule made money on the biggest markets and lost on small coins, memecoins and stocks
# (BACKTEST.md), so: a fixed core plus two slots for the large caps (15 biggest by market cap) trending up most.
TREND_PRO = dict(core=["BTC", "ETH", "SOL", "xyz:GOLD", "xyz:XYZ100"], slots=2, top=15, look_d=90, risk_pct=3.0,
                 kill_dd=0.50)
# Nov 14: the chart reader. Of eight chart readings tested alone over six years, four made money in both halves
# (price vs the 100/200 EMA, and the 9/21 EMA on 4H, 12H, and 20/50 on the daily); it trades when 3 of those 4
# agree, each trade sized so that its stop costs 1% of the account. See BACKTEST.md.
CONFLUENCE = dict(need=3, votes=["trend", "fast", "half-day", "daily"], risk_pct=1.0, gross=10.0, max_open=5)
# Nov 3: retired (positions closed, history kept). Six years: ms_struct -78%; news_regime never traded without the
# paid AI analyst; the watcher only re-sized the other bots' trades and learning did not help in the backtest.
RETIRED_V2 = {"ms_struct": "lost 78% over the six-year backtest (every coin negative)",
              "news_regime": "never traded: needs the paid AI news analyst",
              "watcher": "copied the other bots at 1x; its learning added nothing in the backtest"}
# Oct 30 (six-year backtest, BACKTEST.md): market structure lost on every coin over 2020-2026, so Turbo stops copying
# it (the bot itself keeps running on paper); TA trend at 2% risk and a BTC/gold rotation run as their own accounts.
TURBO_SOURCES = ["rel_value", "ta_trend"]
# Oct 24 ("option A", BACKTEST.md): Turbo at 5x, volatility target 1.25%/day, plus the carry bot copied on half
# of Turbo's money. Engine backtest: $500 -> $2,343, max drawdown -14.3%, worst day -8.3% (Turbo alone at 3x:
# $1,907, -13.9%). Options B (4x, 1.25%: $2,217, -12.8%) and C (4x, 1.0%) are calmer points on the same curve.
TURBO_LEV = 5.0
TURBO_RISK = dict(brake_dd=1.0, vol_target=0.0125, dd_start=0.12, dd_budget=0.25, min_scale=0.2)
TURBO_CARRY_SHARE = 0.5
DESCRIPTIONS = {
    "watcher": W.Watcher.description,
    "turbo": (f"Turbo (growth): copies the relative-value and TA trend bots, each on a third of its "
              f"money at {TURBO_LEV:.0f}x, plus the funding-carry bot on {TURBO_CARRY_SHARE:.0%} of its money (no extra "
              f"leverage, never trimmed). "
              f"Shrinks the directional copies when its own daily swings exceed {TURBO_RISK['vol_target']:.2%}, and "
              f"keeps shrinking them if it falls 12%+ below its peak. Backtest: $500 -> $2,343, worst drop -14.3%"),
    "carry": carry.FundingCarry.description,
    "trend_2x": S.TrendBig.description, "rotation": S.Rotation.description, "trend_pro": S.TrendPro.description,
    "confluence": ("Chart reader: reads each chart four ways (price vs the 100/200 EMA on 4H; the 9/21 EMA on 4H and "
                   "12H; the 20/50 EMA on the daily) and trades when at least three agree, long or short, up to five "
                   "markets at once. Each trade is sized so that its stop (2.5 ATR, then trailed) costs 1% of the "
                   "account. Six-year backtest: +38% a year, every year positive, worst drop -56%"),
    "ms_struct": S.MarketStructure.description, "rel_value": S.RelativeValue.description,
    "news_regime": S.NewsRegime.description, "ta_trend": S.TATrend.description,
    "coin_flip": signals.DESCRIPTIONS["coin_flip"], "buy_hold": signals.DESCRIPTIONS["buy_hold"],
}


def fresh_state():
    return {"created": int(time.time() * 1000), "last_tick": None, "last_decision": None,
            "accounts": {}, "trades": [], "curve": [], "events": [], "matrix": {}, "seen_news": [],
            "last_signals": {}, "news_feed": [], "errors": {}}


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
    return fresh_state()


def migrate(state, marks_legacy, now):
    """v1 -> v2: retire the naive v1 bots (close their positions, keep their history), add the
    four research bots with fresh $500 accounts, keep the two controls running."""
    if state.get("version") == 2:
        if "turbo" not in state["accounts"]:              # + turbo
            state["accounts"]["turbo"] = engine.new_account(C.START_EQUITY)
            state["turbo_started"] = now
        if "carry" not in state["accounts"]:              # + funding carry (Oct 24)
            state["accounts"]["carry"] = engine.new_account(C.START_EQUITY)
            state["carry_started"] = now
        for s in ("trend_2x", "rotation", "confluence", "trend_pro"):   # Oct 30, Nov 14 (chart reader), Nov 15
            if s not in state["accounts"]:
                state["accounts"][s] = engine.new_account(C.START_EQUITY)
                state[f"{s}_started"] = now
        return
    retired = {}
    for s in RETIRED + CONTROLS:              # controls restart too, so every bot starts level
        acct = state["accounts"].pop(s, None)
        if not acct:
            continue
        for a in list(acct["positions"]):
            if a in marks_legacy:
                paper.close(acct, a, marks_legacy[a], now, "bot retired", state["trades"], s)
        name = s + "_v1" if s in CONTROLS else s
        retired[name] = {"equity": round(paper.equity(acct, marks_legacy), 2), "retired_at": now,
                         "trades": sum(1 for t in state["trades"] if t["strategy"] == s),
                         "description": signals.DESCRIPTIONS[s]}
    for t in state["trades"]:
        if t["strategy"] in CONTROLS:
            t["strategy"] += "_v1"
    for s in CONTROLS:
        state["accounts"][s] = paper.new_account()
    for s in ACTIVE:
        state["accounts"][s] = engine.new_account(C.START_EQUITY)
    state["watcher_started"] = state["turbo_started"] = now
    state["last_decision"] = None                  # controls re-enter right away
    state.update({"version": 2, "retired": retired, "v2_started": now, "engine_T": now // HOUR * HOUR,
                  "news_items": [], "top30": None, "top30_src": None, "top30_day": None})


def stats(state):
    out = {}
    for s in ACTIVE + CONTROLS:
        tr = [t for t in state["trades"] if t["strategy"] == s]
        curve = [p["eq"][s] for p in state["curve"] if s in p["eq"]]
        peak, dd = C.START_EQUITY, 0.0
        for e in curve:
            peak = max(peak, e)
            dd = min(dd, e / peak - 1)
        eq = curve[-1] if curve else C.START_EQUITY
        acct = state["accounts"][s]
        out[s] = {"equity": round(eq, 2), "return_pct": round((eq / C.START_EQUITY - 1) * 100, 2),
                  "trades": len(tr), "win_pct": round(sum(t["pnl"] > 0 for t in tr) / len(tr) * 100, 1) if tr else None,
                  "avg_pnl": round(sum(t["pnl"] for t in tr) / len(tr), 3) if tr else None,
                  "max_dd_pct": round(dd * 100, 2), "open": len(acct["positions"]),
                  "closed_pnl": round(sum(t["pnl"] for t in tr), 2),
                  "open_pnl": round(eq - C.START_EQUITY - sum(t["pnl"] for t in tr), 2),   # equity = start + closed + open
                  "control": s in CONTROLS, "description": DESCRIPTIONS[s]}
    return out


def fetch(coins_1h, coins_4h, errors):
    data = {}
    for c in coins_1h:
        try:
            data[c] = market.candles(c, 24 * 200, "1h")
        except Exception as e:
            errors[f"candles:{c}"] = str(e)[:120]
    for c in coins_4h:                               # relative-value coins: 20 days of hourly candles,
        if c in data:                                # enough for its 7-day signal and hourly breaker
            continue
        try:
            data[c] = market.candles(c, 24 * 20, "1h", pause=0.3)
        except Exception as e:
            errors[f"candles:{c}"] = str(e)[:120]
    return {c: v for c, v in data.items() if len(v) > 30}


def tick():
    now = int(time.time() * 1000)
    state = load_state()
    errors = {}
    ctx = market.contexts()
    marks = {c: v["mark"] for c, v in ctx.items()}
    funding = {c: v["funding"] for c, v in ctx.items()}
    marks_legacy = {a: marks[c] for a, c in LEGACY.items() if c in marks}
    migrate(state, marks_legacy, now)

    # ---- universe for relative value: top 30 by market cap that trade on Hyperliquid (daily)
    day = datetime.fromtimestamp(now / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    if state.get("top30_day") != day:
        lst, src = universe.top30(set(ctx))
        if lst:
            state["top30"], state["top30_src"], state["top30_day"] = lst, src, day
        else:
            errors["universe"] = "market-cap sources unavailable, kept previous list"
    top30 = state.get("top30") or universe.FALLBACK_TOP30
    held_rv = list(state["accounts"]["rel_value"]["positions"])

    data = fetch(universe.DIRECTIONAL, top30 + held_rv, errors)

    # ---- Trend Pro's rotating slots: the large caps with the strongest 90-day rise (daily)
    tp = state.setdefault("trend_pro_view", {"picks": [], "table": [], "day": None, "peak": C.START_EQUITY, "killed": False})
    if tp.get("day") != day:
        daily = {}
        for c in [x for x in top30 if x not in TREND_PRO["core"]][:TREND_PRO["top"]]:
            try:
                daily[c] = market.candles(c, TREND_PRO["look_d"] + 10, "1d", pause=0.3)
            except Exception as e:
                errors[f"trend_pro:{c}"] = str(e)[:100]
        if len(daily) >= 5:
            tp["picks"], tp["table"] = S.trending_large_caps(daily, TREND_PRO["core"], TREND_PRO["slots"], TREND_PRO["look_d"])
            tp["day"] = day
    held_tp = list(state["accounts"].get("trend_pro", {}).get("positions", {}))
    for c in tp["picks"] + held_tp:                     # the trend rule needs the long hourly history
        if c not in data or len(data[c]) < 24 * 60:
            try:
                data[c] = market.candles(c, 24 * 200, "1h")
            except Exception as e:
                errors[f"candles:{c}"] = str(e)[:120]

    # ---- news: fetch -> AI analyst (or keyword fallback) -> items; keep learning the keyword matrix
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
    bars_legacy = {a: data[c][-24 * 9:] for a, c in LEGACY.items() if c in data}
    learn.log_events(state, themed, marks_legacy, now)
    learn.fill_returns(state, bars_legacy, now)
    state["seen_news"] = (state["seen_news"] + [it["id"] for it in fresh])[-6000:]
    state["news_feed"] = (sorted(themed, key=lambda x: -x["ts"]) + state["news_feed"])[:150]
    analysed, status = analyst.analyse(fresh, state["matrix"])
    state["news_items"] = sorted(state.get("news_items", []) + analysed, key=lambda x: -x["ts"])
    state["news_items"] = [x for x in state["news_items"] if now / 1000 - x["ts"] < 4 * 86400][:400]
    state["analyst_status"] = status
    nitems = state["news_items"]

    # ---- research bots: replay every closed hour since the last tick
    t_to = now // HOUR * HOUR
    t_from = max(state.get("engine_T") or t_to, t_to - MAX_REPLAY_H * HOUR)
    bots = build(top30, held_rv)
    for b in bots:
        b.prepare(data)
    for s, why in RETIRED_V2.items():                     # Nov 3: close and archive the retired bots
        acct = state["accounts"].pop(s, None)
        if acct is None:
            continue
        px = {c: data[c][-1]["c"] for c in acct["positions"] if c in data}
        br = engine.Broker(s, acct, px, state["trades"], booked=now)
        for c in list(acct["positions"]):
            if c in px:
                br.close(c, now, "bot retired")
        state.setdefault("retired", {})[s] = {
            "equity": round(engine.equity(acct, px), 2), "retired_at": now,
            "trades": sum(1 for t in state["trades"] if t["strategy"] == s), "description": f"Retired Nov 3: {why}."}
    # funding carry: pairs listed on Hyperliquid and Aster (prices, funding history, current rates)
    held_pairs = list(state["accounts"]["carry"]["mem"].get("pairs", {}))
    pairs, book, cdata, cfund, cmarks = carry.build_live(ctx, data, held_pairs, errors)
    data.update(cdata)
    marks.update(cmarks)
    cb = carry.FundingCarry(pairs, book)
    ctx = {"news": lambda coin, T: analyst.view(nitems, coin, T)}
    accts = {s: state["accounts"][s] for s in ACTIVE}
    def fund_fn(c, t):                                   # carry legs: their own funding history
        return cfund(c, t) if c.startswith(("hlc:", "ast:")) else funding.get(c, 0.0)
    srcs = TURBO_SOURCES + ["carry"]
    tb = W.Turbo(srcs, {s: state["accounts"][s] for s in srcs}, state["trades"], list(data),
                 funding=lambda c, t: funding.get(c), lev=TURBO_LEV, **TURBO_RISK)
    tb.share = 1 / 3                                     # as backtested: each bot at a third, times the leverage
    tb.fixed_share = {"carry": TURBO_CARRY_SHARE}        # carry on half the money, no leverage, never trimmed
    tb.prepare(data)
    tb.ctx = ctx
    cf = S.Confluence(universe.DIRECTIONAL, **CONFLUENCE)
    tpa = state["accounts"]["trend_pro"]
    tp_eq = engine.equity(tpa, {c: data[c][-1]["c"] for c in tpa["positions"] if c in data})
    tp["peak"] = max(tp.get("peak", C.START_EQUITY), tp_eq)
    tp["killed"] = tp.get("killed") or tp_eq < tp["peak"] * (1 - TREND_PRO["kill_dd"])      # stays off once tripped
    tpb = S.TrendPro(list(dict.fromkeys(TREND_PRO["core"] + tp["picks"] + held_tp)),
                     **dict(PARAMS["ta_trend"], risk_pct=TREND_PRO["risk_pct"]))
    tpb.allowed = set() if tp["killed"] else set(TREND_PRO["core"] + tp["picks"])
    extra = [S.TrendBig(universe.DIRECTIONAL, **dict(PARAMS["ta_trend"], risk_pct=2.0)), S.Rotation(), cf, tpb]
    for b in extra:
        b.prepare(data)
    engine.run(bots + extra + [cb, tb], accts, data, fund_fn, t_from, t_to, state["trades"], ctx=ctx, booked=now,
               context_fn=tb.context)
    state["carry_view"] = {"pairs": len(pairs), "held": state["accounts"]["carry"]["mem"].get("pairs", {}),
                           "next": (t_to // HOUR // cb.p["rebalance_h"] + 1) * cb.p["rebalance_h"] * HOUR,
                           "top_gaps": sorted(([p["sym"], round(g, 1)] for p in pairs
                                               if (g := book.mean_gap(p["sym"], t_to, 168)) is not None),
                                              key=lambda x: -abs(x[1]))[:10]}
    state["engine_T"] = t_to

    # ---- controls (unchanged from v1): daily coin flip / always long on the six v1 markets
    for s in CONTROLS:
        acct = state["accounts"][s]
        for a in list(acct["positions"]):
            since = [b for b in bars_legacy.get(a, []) if b["t"] + HOUR > (state["last_tick"] or now)]
            paper.check_stops(acct, a, since, state["trades"], s)
            if a in acct["positions"] and a in marks_legacy:
                hours = max(0.0, (now - (state["last_tick"] or now)) / HOUR)
                paper.accrue_funding(acct, a, funding.get(LEGACY[a], 0.0), hours, marks_legacy[a])
    dt = datetime.fromtimestamp(now / 1000, tz=timezone.utc)
    day_key = dt.strftime("%Y-%m-%d") if dt.hour >= C.DECISION_HOUR_UTC else None
    if day_key and day_key != state["last_decision"]:
        rnd = random.Random(day_key)                       # reproducible coin flips per day
        decision = {"coin_flip": {a: rnd.choice([-1, 1]) for a in C.ASSETS}, "buy_hold": {a: 1 for a in C.ASSETS}}
        for s in CONTROLS:
            paper.rebalance(state["accounts"][s], decision[s], marks_legacy, now, state["trades"], s)
        state["last_decision"] = day_key
        state["last_signals"] = decision

    # ---- bookkeeping
    eq = {s: round(engine.equity(state["accounts"][s], marks), 2) for s in ACTIVE}
    eq.update({s: round(paper.equity(state["accounts"][s], marks_legacy), 2) for s in CONTROLS})
    state["curve"].append({"t": now, "eq": eq})
    state["curve"] = state["curve"][-24 * 400:]
    state["trades"] = state["trades"][-8000:]
    state["last_tick"] = now
    state["errors"] = errors
    held_now = {c for s in ACTIVE for c in state["accounts"][s]["positions"]}
    state["marks"] = {c: marks[c] for c in set(universe.DIRECTIONAL) | set(top30) | held_now if c in marks}
    state["marks_legacy"] = marks_legacy
    state["regimes"] = regimes(data, bots, t_to)
    allv = S.Confluence(universe.DIRECTIONAL)                       # all eight readings, for the dashboard
    allv.prepare(data)
    t4 = t_to // (4 * HOUR) * (4 * HOUR)
    for c in allv.d:
        v = allv.votes(c, t4)
        if v is not None:
            state["regimes"].setdefault(c, {})["votes"] = v
    for c, row in state["regimes"].items():
        sc, cf = analyst.view(nitems, c, t_to)
        row["news"] = round(sc * cf, 3)
    write(state)
    return state


def regimes(data, bots, T):
    """What the market looks like right now, per market (for the dashboard). The structure and regime
    readings come from the retired bots' indicators, kept as a view only."""
    out = {}
    ms, nr = S.MarketStructure(universe.DIRECTIONAL, **PARAMS["ms_struct"]), S.NewsRegime(universe.DIRECTIONAL)
    ms.prepare(data)
    nr.prepare(data)
    ta = next(b for b in bots if b.name == "ta_trend")
    for c in universe.DIRECTIONAL:
        row = {}
        d = ms.d.get(c)
        if d:
            i4, idd = d["h4"].at(T), d["d1"].at(T)
            if i4 >= 0:
                s4 = d["s4"][i4]
                row["ms_4h"] = s4["trend"]
                row["ms_protect"] = s4["protect"]
                row["ms_leg"] = s4["leg"]
            if idd >= 0:
                row["ms_1d"] = d["sd"][idd]["trend"]
        d = nr.d.get(c)
        if d:
            i = d["h4"].at(T)
            if i >= 0 and d["adx"][i] is not None:
                reg, tdir = nr.regime(d, i)
                row["regime"] = reg + ("" if not tdir else (" up" if tdir > 0 else " down"))
                row["adx"] = round(d["adx"][i], 1)
        d = ta.d.get(c)
        if d:
            i = d["h4"].at(T)
            if i >= 0:
                row["ema_fast_above"] = d["f"][i] > d["s"][i]
        out[c] = row
    return out


def _p(x):
    return f"{x:.6g}" if isinstance(x, (int, float)) else ("" if x is None else x)


def _utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def _market(t):
    legacy = t["strategy"].removesuffix("_v1") in CONTROLS + RETIRED
    return LEGACY.get(t["asset"], t["asset"]) if legacy else t["asset"]


def write_csv(state):
    """Every trade with UTC timestamps and prices, for cross-checking on the exchange."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["strategy", "asset", "hyperliquid_market", "side", "setup", "signal_utc", "opened_utc", "entry_price",
                "initial_stop", "closed_utc", "exit_price", "move_pct", "r_multiple", "pnl_usd", "funding_usd",
                "reason", "recorded_utc", "note"])
    for t in state["trades"]:
        w.writerow([t["strategy"], t["asset"], _market(t), t["side"], t.get("setup", ""), _utc(t.get("signal_t")),
                    _utc(t["opened"]), _p(t["entry"]), _p(t.get("stop0")), _utc(t["closed"]), _p(t["exit"]),
                    t["move_pct"], _p(t.get("r")), t["pnl"], t["funding"], t["reason"], _utc(t.get("booked")),
                    t.get("note", "")])
    (OUT / "trades.csv").write_text(buf.getvalue())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["strategy", "asset", "hyperliquid_market", "side", "setup", "opened_utc", "entry_price", "stop_price",
                "partial_target", "target_price", "mark_now", "funding_usd_so_far", "note"])
    for s, acct in state["accounts"].items():
        for a, p in acct["positions"].items():
            mk = state.get("marks_legacy", {}).get(a) if s in CONTROLS else state.get("marks", {}).get(a)
            w.writerow([s, a, LEGACY.get(a, a) if s in CONTROLS else a, "long" if p["side"] > 0 else "short",
                        p.get("setup", ""), _utc(p["opened"]), _p(p["entry"]), _p(p["stop"]), _p(p.get("tp1")),
                        _p(p.get("target")), _p(mk), round(p["funding"], 4), p.get("note", "")])
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
    v2 = state.get("v2_started", 0)
    summary = {
        "version": 2, "updated": state["last_tick"], "v2_started": v2, "engine_T": state.get("engine_T"),
        "config": {"start_equity": C.START_EQUITY, "directional": universe.DIRECTIONAL,
                   "labels": {c: universe.label(c) for c in set(universe.DIRECTIONAL) | set(state.get("top30") or [])
                              | set(state.get("marks", {}))},
                   "top30": state.get("top30") or universe.FALLBACK_TOP30, "top30_src": state.get("top30_src"),
                   "params": {k: v for k, v in PARAMS.items()},
                   "legacy_markets": LEGACY},
        "bots": ACTIVE, "controls": CONTROLS, "stats": stats(state), "retired": state.get("retired", {}),
        "curve": [p for p in state["curve"] if p["t"] >= v2][::max(1, len(state["curve"]) // 1500)],
        "positions": {s: state["accounts"][s]["positions"] for s in ACTIVE + CONTROLS},
        "orders": {s: state["accounts"][s].get("orders", {}) for s in ACTIVE},
        "carry": state.get("carry_view"), "carry_started": state.get("carry_started"),
        "started": {s: state.get(f"{s}_started") for s in ("turbo", "carry", "trend_2x", "rotation", "confluence", "trend_pro")},
        "trend_pro": state.get("trend_pro_view"),
        "turbo": {"started": state.get("turbo_started"), "lev": TURBO_LEV,
                  "peak": state["accounts"]["turbo"]["mem"].get("peak"),
                  "liquidations": state["accounts"]["turbo"]["mem"].get("liquidations", 0)},
        "marks": state.get("marks", {}), "marks_legacy": state.get("marks_legacy", {}),
        "trades": state["trades"][-500:][::-1], "regimes": state.get("regimes", {}),
        "news_items": state.get("news_items", [])[:100], "analyst_status": state.get("analyst_status"),
        "errors": state["errors"],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, separators=(",", ":")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    if a.reset and (OUT / "state.json").exists():
        (OUT / "state.json").unlink()
    s = tick()
    st = stats(s)
    print(f"tick {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC | analyst: {s.get('analyst_status')} | " +
          " | ".join(f"{k} ${v['equity']:.2f} ({v['trades']}t, {v['open']} open)" for k, v in st.items()))
    if s["errors"]:
        print("errors:", s["errors"])


if __name__ == "__main__":
    main()
