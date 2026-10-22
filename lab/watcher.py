"""The watcher: a bot that trades nothing of its own. It watches the other four bots, records
the market conditions around every trade they open, learns which bot does well in which
conditions, and copies their trades into its own $500 account with more or less money
depending on what it has learned. Free: plain statistics, no AI service needed.

Conditions recorded at every entry (all from data available at that moment):
  regime      ADX on 4H candles: trend (>= 25), range (< 20), transition
  daily       is the trade with or against the daily market structure (BOS / protected levels)?
  volatility  4H ATR as % of price vs its last 90 candles: low / normal / high
  news        the news analyst's view on that market: aligned / against / neutral
  funding     long while longs pay a lot, short while shorts pay ... = "crowded side" or not
  session     time of day (UTC): asia / europe / us / late
  btc_trend   Bitcoin's daily structure: the market backdrop
  side        long / short;  market  crypto / tradfi

Learning: every closed trade is scored in 'R-equivalents' (P&L / $5, i.e. 1% of the
starting $500, the normal risk per trade). For each bot and each condition bucket it keeps
the count and a heavily *shrunk* average (sum / (n + 30)), so a bucket with a few lucky
trades barely moves anything. A new trade's expected value = the bot's overall shrunk
average plus the mean extra edge of its conditions (only buckets with 30+ trades count).

Copying: each bot gets a base 25% share of the watcher's money (mirroring the bot's own
exposure). Until a bot has 20 closed trades the watcher copies it at 1x (neutral). After
that the size multiplier is 1 + expected R, kept between 0.5x and 1.5x. Relative value is a
market-neutral book, so it is copied as a whole (bot-level estimate only), never leg by leg.

Why so cautious: on the 12-month backtest, bolder learning (0x-2x, small buckets) did WORSE
than a plain equal split (+32% vs +38%) because the patterns it found were mostly noise. This
setting stays close to the equal split (+35%) and only tilts when evidence piles up.
Exits mirror the source: same stop / target / time stop, and close when the source closes."""
from collections import defaultdict
from datetime import datetime, timezone

from lab import ind
from lab.engine import HOUR, Strategy, equity

DAY = 24 * HOUR
R_UNIT = 5.0                      # $5 = 1% of the $500 start: the normal risk per trade
SHRINK_K = 30
MIN_BUCKET = 30
MULT_LO, MULT_HI = 0.5, 1.5
WARMUP_TRADES = 20
BASE_SHARE = 0.25
FEATURES = ["regime", "daily", "volatility", "news", "funding", "session", "btc_trend", "side", "market"]
BASELINE_FUNDING = 0.0000125      # Hyperliquid's neutral hourly funding


class Watcher(Strategy):
    name, every_h = "watcher", 1
    description = ("Watcher (self-learning): records the conditions around every trade the four bots take "
                   "(trend/range, with/against the daily structure, volatility, news, funding, session), learns "
                   "which bot wins in which conditions, and copies their trades with more money (up to 1.5x) "
                   "where they have an edge and less (down to 0.5x) where they don't. Starts as an equal split")

    def __init__(self, sources, source_accounts, log, coins, funding=None, learning=True):
        self.sources, self.src, self.log, self.coins = sources, source_accounts, log, coins
        self.learning = learning                       # False = plain equal split (the benchmark)
        self.funding = funding or (lambda c, t: 0.0)
        self.d, self.ctx = {}, {}
        self.stats, self.stats_T = {}, None
        self.share, self.max_gross = BASE_SHARE, 5.0

    def leverage(self, br, eq):
        """Extra size multiplier (the watcher itself: none)."""
        return 1.0

    # ------------------------------------------------------------------ context
    def prepare(self, data):
        for c, bars in data.items():
            if len(bars) < 60:
                continue
            h4 = _frame(bars, 4)
            d1 = _frame(bars, 24)
            if len(h4.b) < 40:
                continue
            ad, _, _ = ind.adx(h4.b, 14)
            self.d[c] = {"h4": h4, "d1": d1, "adx": ad, "atr": ind.atr(h4.b, 14),
                         "sd": ind.swing_structure(d1.b, 2) if len(d1.b) > 10 else []}

    def context(self, bot, coin, T, side):
        """Market conditions for a trade opened at T (only data up to T)."""
        out = {"side": "long" if side > 0 else "short", "market": "tradfi" if coin.startswith("xyz:") else "crypto"}
        h = datetime.fromtimestamp(T / 1000, tz=timezone.utc).hour
        out["session"] = "asia" if h < 8 else "europe" if h < 14 else "us" if h < 22 else "late"
        d = self.d.get(coin)
        if d:
            i = d["h4"].at(T)
            if i >= 20 and d["adx"][i] is not None:
                a = d["adx"][i]
                out["regime"] = "trend" if a >= 25 else "range" if a < 20 else "transition"
            if i >= 20 and d["atr"][i]:
                w = [d["atr"][k] / d["h4"].b[k]["c"] for k in range(max(14, i - 90), i + 1) if d["atr"][k]]
                cur = d["atr"][i] / d["h4"].b[i]["c"]
                pct = sum(x <= cur for x in w) / len(w)
                out["volatility"] = "low" if pct < 0.33 else "high" if pct > 0.67 else "normal"
            j = d["d1"].at(T)
            if d["sd"] and j >= 0:
                tr = d["sd"][j]["trend"]
                out["daily"] = "none" if tr == 0 else ("with" if tr == side else "against")
        b = self.d.get("BTC")
        if b and b["sd"]:
            j = b["d1"].at(T)
            if j >= 0:
                out["btc_trend"] = {1: "up", -1: "down"}.get(b["sd"][j]["trend"], "none")
        news = self.ctx.get("news")
        if news:
            sc, cf = news(coin, T)
            v = sc * cf * side
            out["news"] = "aligned" if v >= 0.1 else "against" if v <= -0.1 else "neutral"
        f = self.funding(coin, T)
        if f is not None:
            paying = f * side
            out["funding"] = ("crowded side" if paying > 3 * BASELINE_FUNDING else
                              "paid to hold" if paying < 0 else "normal")
        return out

    # ------------------------------------------------------------------ learning
    def learn(self, T):
        """Rebuild the statistics from every source-bot trade closed before T."""
        acc = defaultdict(lambda: [0, 0.0, 0])          # n, sum R, wins
        for t in self.log:
            if t["strategy"] not in self.sources or t["closed"] > T:
                continue
            r = t["pnl"] / R_UNIT
            keys = [(t["strategy"], "all", "all")]
            keys += [(t["strategy"], f, v) for f, v in (t.get("ctx") or {}).items() if f in FEATURES]
            for k in keys:
                a = acc[k]
                a[0] += 1
                a[1] += r
                a[2] += r > 0
        self.stats = {k: {"n": n, "mean": s / n, "shrunk": s / (n + SHRINK_K), "win": w / n}
                      for k, (n, s, w) in acc.items()}
        self.stats_T = T

    def expect(self, bot, ctx):
        """(expected R, multiplier, reason text) for a new trade by `bot` in conditions ctx."""
        base = self.stats.get((bot, "all", "all"))
        if not self.learning or not base or base["n"] < WARMUP_TRADES:
            n = base["n"] if base else 0
            return 0.0, 1.0, f"learning ({n}/{WARMUP_TRADES} trades): copied at 1x"
        e0, extras, why = base["shrunk"], [], []
        if bot != "rel_value":                        # RV is a neutral book: bot-level only
            for f, v in (ctx or {}).items():
                s = self.stats.get((bot, f, v))
                if s and s["n"] >= MIN_BUCKET:
                    extras.append(s["shrunk"] - e0)
                    if abs(s["shrunk"] - e0) >= 0.05:
                        why.append(f"{f}={v} {s['shrunk'] - e0:+.2f}R (n={s['n']})")
        e = e0 + (sum(extras) / len(extras) if extras else 0.0)
        m = max(MULT_LO, min(MULT_HI, 1 + e))
        return e, m, f"expected {e:+.2f}R (bot {e0:+.2f}R{'; ' + ', '.join(why) if why else ''}): x{m:.2f}"

    # ------------------------------------------------------------------ trading
    def step(self, T, br, ctx):
        self.ctx = ctx or self.ctx
        if self.stats_T is None or T - self.stats_T >= DAY:
            self.learn(T)
        mem = br.acct["mem"].setdefault("src", {})
        eq = br.equity()
        # mirror exits and stop moves of the source positions
        for c, p in list(br.acct["positions"].items()):
            bot = mem.get(c)
            sp = self.src[bot]["positions"].get(c) if bot in self.src else None
            if not sp or sp["side"] != p["side"]:
                br.close(c, T, f"{bot} closed its trade")
                mem.pop(c, None)
            else:
                br.tighten(c, sp["stop"])
                cut = p["qty"] - p["qty0"] * sp["qty"] / sp["qty0"]      # source trimmed (e.g. circuit breaker)
                if cut > p["qty0"] * 1e-6:
                    br._exit(c, cut, br.px[c] * (1 - p["side"] * 2e-4), T, f"{bot} reduced its trade")
        for c in list(mem):
            if c not in br.acct["positions"]:
                mem.pop(c)
        # copy fresh entries
        for bot in self.sources:
            acct = self.src.get(bot)
            if not acct:
                continue
            fresh = [(c, p) for c, p in acct["positions"].items() if p["opened"] == T]
            if not fresh:
                continue
            src_eq = equity(acct, br.px)
            if bot == "rel_value":
                e, m, why = self.expect(bot, {})
            for c, p in fresh:
                if c in br.acct["positions"] or c not in br.px or src_eq <= 0:
                    continue
                if bot != "rel_value":
                    e, m, why = self.expect(bot, p.get("ctx"))
                lv = self.leverage(br, eq)
                qty = p["qty"] * (eq / src_eq) * self.share * m * lv
                room = max(0.0, eq * self.max_gross - sum(q["qty"] * br.px.get(k, q["entry"])
                                                          for k, q in br.acct["positions"].items()))
                qty = min(qty, room / br.px[c])
                if br.market(c, p["side"], qty, T, p["stop"], target=p.get("target"), tp1=p.get("tp1"),
                             tp1_frac=p.get("tp1_frac", 0.5), be_after_tp1=p.get("be", True),
                             time_stop=p.get("time_stop"), setup=f"copy of {bot}: {p.get('setup', '')}",
                             note=why, signal_t=T, track_r=p.get("risk_px") is not None):
                    mem[c] = bot
                    br.acct["positions"][c]["ctx"] = p.get("ctx") or {}
                    full = getattr(self, "lev", 1.0)                 # size it would have at full leverage
                    br.acct["positions"][c]["full_qty"] = qty * full / lv if lv else qty

    # ------------------------------------------------------------------ reporting
    def report(self, T):
        """What the watcher has learned, for the dashboard, plus a plain-English journal entry."""
        if self.stats_T != T:
            self.learn(T)
        bots, notes = {}, []
        for bot in self.sources:
            base = self.stats.get((bot, "all", "all"))
            rows = []
            for (b, f, v), s in self.stats.items():
                if b == bot and f != "all" and s["n"] >= MIN_BUCKET:
                    rows.append({"feature": f, "value": v, "n": s["n"], "win": round(s["win"] * 100, 1),
                                 "avg_r": round(s["mean"], 2), "edge": round(s["shrunk"] - (base["shrunk"] if base else 0), 3)})
            rows.sort(key=lambda r: -r["edge"])
            e, m, why = self.expect(bot, {})
            bots[bot] = {"n": base["n"] if base else 0, "win": round(base["win"] * 100, 1) if base else None,
                         "avg_r": round(base["mean"], 2) if base else None,
                         "shrunk": round(base["shrunk"], 3) if base else None,
                         "warm": bool(base and base["n"] >= WARMUP_TRADES), "multiplier": round(m, 2),
                         "best": rows[:3], "worst": [r for r in rows[::-1][:3] if r["edge"] < 0]}
            name = BOT_NAMES.get(bot, bot)
            if not base:
                notes.append(f"{name}: no closed trades yet.")
                continue
            line = (f"{name}: {base['n']} trades, {base['win'] * 100:.0f}% winners, average {base['mean']:+.2f}R. ")
            if base["n"] < WARMUP_TRADES:
                line += f"Still learning ({base['n']}/{WARMUP_TRADES}); copying at normal size."
            else:
                line += f"Copying at x{m:.2f} by default."
            good = [r for r in rows if r["edge"] >= 0.05][:2]
            bad = [r for r in rows[::-1] if r["edge"] <= -0.05][:2]
            if good:
                line += " Works best when " + "; ".join(f"{r['feature']} = {r['value']} ({r['avg_r']:+.2f}R over {r['n']})" for r in good) + "."
            if bad:
                line += " Weaker when " + "; ".join(f"{r['feature']} = {r['value']} ({r['avg_r']:+.2f}R over {r['n']})" for r in bad) + "."
            notes.append(line)
        total = sum(v["n"] for v in bots.values())
        if total < 40:
            notes.append(f"Only {total} closed trades so far: everything above is still mostly luck. "
                         f"Patterns start to mean something after a few hundred.")
        return {"bots": bots, "journal": notes, "features": FEATURES}


BOT_NAMES = {"ms_struct": "Market structure", "rel_value": "Relative value", "news_regime": "News + regime",
             "ta_trend": "TA trend"}


def _frame(bars, hours):
    from lab.strategies import frame
    return frame(bars, hours)


class Turbo(Watcher):
    """Turbo: the "grow a small account fast" experiment.

    Same copies as the watcher (equal split, no learning: learning did not help in the
    backtest), but levered. From first principles, the fastest long-run growth for an edge
    with mean m and variance v per day comes at leverage m / v (the Kelly fraction); our
    three-bot portfolio's backtest put that near 8x, which would have made ~1% a day with a
    -76% drawdown. Real edges are usually smaller than backtests show, and betting above
    the true Kelly leverage shrinks growth and invites ruin, so this runs at about half of
    the backtest Kelly, plus a brake: while the account is more than 30% below its peak,
    new copies are made at half size. Total exposure is capped at 15x equity."""
    name = "turbo"
    description = ("Turbo (growth): copies all bots like the watcher but at ~3x their size. Two safety valves: "
                   "when the account's own daily swings exceed 1.5% it shrinks every position (open ones too) in "
                   "proportion, and if it ever falls 12%+ below its peak it keeps shrinking toward 20% size by a 25% "
                   "drawdown. Backtest: +0.40%/day, worst drawdown -13.9%")

    def __init__(self, *a, lev=4.0, brake_dd=0.30, dd_budget=None, min_scale=0.0, vol_target=None,
                 dd_start=0.0, **kw):
        kw.setdefault("learning", False)
        super().__init__(*a, **kw)
        self.lev, self.brake_dd, self.max_gross = lev, brake_dd, 15.0
        self.dd_budget, self.min_scale, self.vol_target, self.dd_start = dd_budget, min_scale, vol_target, dd_start

    def scale(self, br, eq):
        """How much of full size to run right now. With a drawdown budget B: full size at a new
        peak, shrinking in a straight line to min_scale as the drawdown approaches B (so losses
        slow down the deeper it goes). Without one: the simple brake (half size beyond brake_dd)."""
        mem = br.acct["mem"]
        mem["peak"] = max(mem.get("peak", eq), eq)
        dd = 1 - eq / mem["peak"]
        sc = 0.5 if dd > self.brake_dd else 1.0
        if self.dd_budget:                                    # backstop: untouched until dd_start
            sc = max(self.min_scale, min(1.0, 1 - max(0.0, dd - self.dd_start) / (self.dd_budget - self.dd_start)))
        if self.vol_target:                                   # daily vol of the account, last 20 days
            h = mem.get("eq_daily", [])
            r = [h[i] / h[i - 1] - 1 for i in range(max(1, len(h) - 20), len(h)) if h[i - 1] > 0]
            if len(r) >= 10:
                m = sum(r) / len(r)
                vol = (sum((x - m) ** 2 for x in r) / len(r)) ** 0.5
                if vol > 0:
                    sc *= min(1.0, self.vol_target / vol)
        return sc

    def leverage(self, br, eq):
        return self.lev * self.scale(br, eq)

    @staticmethod
    def maint_rate(coin):
        """Approximate Hyperliquid maintenance margin (half the initial margin at max leverage)."""
        return {"BTC": 0.0125, "ETH": 0.02, "SOL": 0.025, "XRP": 0.025}.get(coin, 0.05)

    def step(self, T, br, ctx):
        """Liquidation check first: on Hyperliquid (cross margin) the whole account is closed
        when equity falls below the maintenance margin of everything open."""
        eq = br.equity()
        maint = sum(p["qty"] * br.px.get(c, p["entry"]) * self.maint_rate(c) for c, p in br.acct["positions"].items())
        if br.acct["positions"] and eq < maint:
            for c in list(br.acct["positions"]):
                br.close(c, T, "LIQUIDATED (equity below maintenance margin)")
            br.acct["mem"]["liquidations"] = br.acct["mem"].get("liquidations", 0) + 1
            br.acct["mem"]["src"] = {}
        if T % (24 * HOUR) == 0:
            h = br.acct["mem"].setdefault("eq_daily", [])
            h.append(br.equity())
            del h[:-30]
        if (self.dd_budget or self.vol_target) and br.acct["positions"]:   # size control also trims what is open
            sc = self.scale(br, br.equity())
            src_of = br.acct["mem"].get("src", {})
            for c, p in list(br.acct["positions"].items()):
                sp = self.src.get(src_of.get(c), {}).get("positions", {}).get(c)
                ratio = sp["qty"] / sp["qty0"] if sp else 1.0
                target = p.get("full_qty", p["qty"]) * sc * ratio
                if p["qty"] > target * 1.05 and p["qty"] - target > p["qty0"] * 1e-6:
                    br._exit(c, p["qty"] - target, br.px[c] * (1 - p["side"] * 2e-4), T,
                             f"risk control: size cut to {sc:.0%}")
        super().step(T, br, ctx)
