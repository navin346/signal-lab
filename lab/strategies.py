"""The four research bots. Each one is a set of explicit, testable rules; nothing here peeks at
the future (indicator values at a candle use only that candle and earlier ones).

  ms_struct    EmperorBTC market structure: daily + 4H swing structure (BOS / protected HL-LH)
               for bias, 1H CHoCH pullback entries, stops at structure + ATR buffer, R:R filter.
               (Swing-failure entries and a trailed runner are implemented but switched off: they
               lost in the backtest.) News can veto entries.
  rel_value    Market-neutral cross-sectional momentum across the top-30 Hyperliquid coins by mcap
               (residual/reversal, carry modes implemented and tested; they lost).
  news_regime  AI news analyst decides whether/which way; ADX regime decides stop/target/hold.
  ta_trend     Trend following on 4H: Donchian breakout (or EMA crossover), ADX filter, ATR trail.

Live parameters are set in lab/main.py; BACKTEST.md explains how each was chosen."""
import bisect
import math

from lab import ind
from lab.engine import HOUR, Strategy

DAY = 24 * HOUR


class TF:
    """Candles of one timeframe plus a lookup from 'time now' to the last closed candle."""

    def __init__(self, bars, hours):
        self.b, self.ms = bars, hours * HOUR
        self.close_t = [x["t"] + self.ms for x in bars]

    def at(self, T):
        """Index of the last candle closed at or before T (-1 if none)."""
        return bisect.bisect_right(self.close_t, T) - 1

    def closes_at(self, T):
        """Index of the candle that closes exactly at T, else None."""
        i = self.at(T)
        return i if i >= 0 and self.close_t[i] == T else None


def _bars_h(bars):
    if len(bars) < 2:
        return 1
    return max(1, round((bars[1]["t"] - bars[0]["t"]) / HOUR))


def frame(bars, hours):
    """Resample whatever candle size came in to `hours`."""
    h = _bars_h(bars)
    if h == hours:
        return TF(bars, hours)
    if h == 1:
        return TF(ind.resample(bars, hours), hours)
    span, out, cur = hours * HOUR, [], None              # e.g. 4h -> 24h
    need = hours // h
    for b in bars:
        k = b["t"] // span * span
        if cur is None or cur["t"] != k:
            if cur and cur["n"] == need:
                out.append(cur)
            cur = {"t": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "n": 0}
        cur["h"], cur["l"], cur["c"] = max(cur["h"], b["h"]), min(cur["l"], b["l"]), b["c"]
        cur["n"] += 1
    if cur and cur["n"] == need:
        out.append(cur)
    return TF([{k: v for k, v in x.items() if k != "n"} for x in out], hours)


def _news(ctx, coin, T):
    """Net news view for a coin at time T from the analyst: (score -1..1, confidence 0..1)."""
    fn = ctx.get("news")
    return fn(coin, T) if fn else (0.0, 0.0)


# =========================================================================== market structure
class MarketStructure(Strategy):
    name, every_h = "ms_struct", 1
    description = ("Market structure (EmperorBTC method): trade only with the daily and 4H structure; after a 4H "
                   "break of structure wait for the pullback, enter on the 1H change of character back in trend "
                   "direction; stop beyond the pullback extreme + 1 ATR, target the previous swing (min 2R), "
                   "1% risk; strong opposite news vetoes the entry")

    def __init__(self, coins, risk_pct=1.5, min_rr=1.5, atr_buf=0.5, sfp_buf=0.25, use_choch=True,
                 use_sfp=True, sfp_bias="daily", runner=True, max_open=5, invalidate_h=72,
                 news_veto=0.5, choch_bias="none"):
        self.coins = coins
        self.p = dict(risk_pct=risk_pct, min_rr=min_rr, atr_buf=atr_buf, sfp_buf=sfp_buf, use_choch=use_choch,
                      use_sfp=use_sfp, sfp_bias=sfp_bias, runner=runner, max_open=max_open,
                      invalidate_h=invalidate_h, news_veto=news_veto, choch_bias=choch_bias)
        self.d = {}

    def prepare(self, data):
        for c in self.coins:
            if c not in data or len(data[c]) < 300:
                continue
            h1, h4, d1 = frame(data[c], 1), frame(data[c], 4), frame(data[c], 24)
            ph1, pl1 = ind.pivots(h1.b, 2)
            last_ph1, last_pl1, a, z = [], [], None, None
            for x, y in zip(ph1, pl1):
                a = x[1] if x else a
                z = y[1] if y else z
                last_ph1.append(a)
                last_pl1.append(z)
            ins = ind.internal_structure(h1.b, 2)
            last_bear, last_bull, lb, lu = [], [], None, None
            for i, s in enumerate(ins):
                last_bear.append(lb)
                last_bull.append(lu)
                if s["flip"] == -1:
                    lb = i
                if s["flip"] == 1:
                    lu = i
            ph4, pl4 = ind.pivots(h4.b, 3)
            self.d[c] = {"h1": h1, "h4": h4, "d1": d1, "s4": ind.swing_structure(h4.b, 3),
                         "sd": ind.swing_structure(d1.b, 2), "in1": ins, "atr1": ind.atr(h1.b, 24),
                         "atr4": ind.atr(h4.b, 14), "ph4": ph4, "pl4": pl4, "last_ph1": last_ph1,
                         "last_pl1": last_pl1, "last_bear": last_bear, "last_bull": last_bull}

    # ---------------------------------------------------------------- management
    def _manage(self, c, T, br, d, i1):
        p = br.position(c)
        if not p:
            return
        if p["tp1"] is not None and T - p["opened"] >= self.p["invalidate_h"] * HOUR:
            br.close(c, T, "idea invalidated (no follow-through in time)")
            return
        if p["tp1"] is None and self.p["runner"]:            # runner: trail under flipped 1H swings
            lvl = d["last_pl1"][i1] if p["side"] > 0 else d["last_ph1"][i1]
            if lvl is not None and d["atr1"][i1]:
                br.tighten(c, lvl - p["side"] * 0.25 * d["atr1"][i1])

    def _open(self, c, side, entry, stop, target, T, br, setup, note, ctx):
        risk = (entry - stop) * side
        if risk <= 0 or target is None or (target - entry) * side <= 0:
            return
        if (target - entry) * side / risk < self.p["min_rr"]:
            return
        score, conf = _news(ctx, c, T)
        if score * side * conf <= -self.p["news_veto"]:
            return                                              # strong news against the trade
        if sum(1 for _ in br.acct["positions"]) >= self.p["max_open"]:
            return
        qty = br.size_for_risk(c, entry, stop, self.p["risk_pct"])
        kw = dict(tp1=target, tp1_frac=0.5, time_stop=T + 10 * DAY) if self.p["runner"] else dict(target=target)
        br.market(c, side, qty, T, stop, setup=setup, note=note, signal_t=T, **kw)

    def step(self, T, br, ctx):
        for c, d in self.d.items():
            if c not in br.px:
                continue
            i1 = d["h1"].closes_at(T)
            if i1 is None or i1 < 50:
                continue
            self._manage(c, T, br, d, i1)
            if br.position(c) or c in br.acct["orders"]:
                continue
            i4 = d["h4"].at(T)
            if i4 < 30:
                continue
            s4 = d["s4"][i4]
            id_ = d["d1"].at(T)
            sd = d["sd"][id_] if id_ >= 0 else {"trend": 0}
            b1 = d["h1"].b[i1]
            # --- setup A: pullback in a 4H trend, entered on the 1H CHoCH back in trend direction
            daily_ok = self.p["choch_bias"] != "daily" or sd["trend"] == s4["trend"]
            if self.p["use_choch"] and s4["trend"] != 0 and s4["bos_t"] and daily_ok:
                side = s4["trend"]
                flip = d["in1"][i1]["flip"]
                start = d["last_bear"][i1] if side > 0 else d["last_bull"][i1]
                if flip == side and start is not None and d["h1"].b[start]["t"] >= s4["bos_t"]:
                    seg = d["h1"].b[start:i1 + 1]
                    ext = min(x["l"] for x in seg) if side > 0 else max(x["h"] for x in seg)
                    if (ext - s4["protect"]) * side > 0 and d["atr1"][i1]:
                        stop = ext - side * self.p["atr_buf"] * d["atr1"][i1]
                        self._open(c, side, b1["c"], stop, s4["leg"], T, br, "CHoCH pullback",
                                   f"4H {'up' if side > 0 else 'down'}trend since BOS, 1H CHoCH at "
                                   f"{d['in1'][i1]['lvl']:.6g}, pullback extreme {ext:.6g}", ctx)
                        if br.position(c):
                            continue
            # --- setup B: swing failure at a key 4H high/low, only on 4H closes
            if not self.p["use_sfp"] or d["h4"].closes_at(T) is None:
                continue
            k = d["h4"].b[i4]
            rng = k["h"] - k["l"]
            if rng <= 0 or abs(k["c"] - k["o"]) < 0.1 * rng or not d["atr4"][i4]:
                continue                                        # skip dojis (the notes)
            for side in (-1, 1):
                if self.p["sfp_bias"] == "daily" and sd["trend"] == -side:
                    continue                                    # never fade the daily structure
                lvl = self._key_level(d, i4, side)
                if lvl is None:
                    continue
                stop = (k["h"] if side < 0 else k["l"]) - side * self.p["sfp_buf"] * d["atr4"][i4]
                tgt = self._swing_target(d, i4, side, k["c"])
                self._open(c, side, k["c"], stop, tgt, T, br, "SFP",
                           f"4H swept {'high' if side < 0 else 'low'} {lvl:.6g} and closed back inside", ctx)
                if br.position(c):
                    break

    def _key_level(self, d, i4, side, lookback=60):
        """A key swing high (side=-1, short) or low (side=+1, long) that this candle swept but
        closed back inside, and that no candle had closed beyond since it formed."""
        b, k = d["h4"].b, d["h4"].b[i4]
        piv = d["ph4"] if side < 0 else d["pl4"]
        best = None
        for j in range(max(0, i4 - lookback), i4):
            if not piv[j]:
                continue
            pi, lvl = piv[j]
            swept = (k["h"] > lvl and k["c"] < lvl) if side < 0 else (k["l"] < lvl and k["c"] > lvl)
            if not swept:
                continue
            clean = all((b[m]["c"] <= lvl) if side < 0 else (b[m]["c"] >= lvl) for m in range(pi + 1, i4))
            if clean and (best is None or (lvl > best if side < 0 else lvl < best)):
                best = lvl
        return best

    def _swing_target(self, d, i4, side, entry, lookback=60):
        """Last opposite swing (the notes: target the last swing low/high)."""
        piv = d["pl4"] if side < 0 else d["ph4"]
        for j in range(i4 - 1, max(0, i4 - lookback), -1):
            if piv[j] and (piv[j][1] - entry) * side > 0:
                return piv[j][1]
        return None


# =========================================================================== TA trend
class TATrend(Strategy):
    name, every_h = "ta_trend", 4
    description = ("TA trend: 4H Donchian breakout (close beyond the 200-candle / ~33-day high or low) when ADX "
                   "confirms a trend; 2 ATR trailing stop, exit on the opposite breakout, 1% risk. Picked over "
                   "4H EMA crossovers by the backtest")

    def __init__(self, coins, fast=21, slow=55, adx_min=20.0, atr_mult=3.0, risk_pct=1.0, mode="ema",
                 exit_on_cross=True, max_open=8):
        self.coins = coins
        self.p = dict(fast=fast, slow=slow, adx_min=adx_min, atr_mult=atr_mult, risk_pct=risk_pct, mode=mode,
                      exit_on_cross=exit_on_cross, max_open=max_open)
        self.d = {}

    def prepare(self, data):
        for c in self.coins:
            if c not in data or len(data[c]) < 300:
                continue
            h4 = frame(data[c], 4)
            cl = [x["c"] for x in h4.b]
            ad, pdi, mdi = ind.adx(h4.b, 14)
            dc_hi = [max(x["h"] for x in h4.b[max(0, i - self.p["slow"]):i]) if i else None for i in range(len(cl))]
            dc_lo = [min(x["l"] for x in h4.b[max(0, i - self.p["slow"]):i]) if i else None for i in range(len(cl))]
            self.d[c] = {"h4": h4, "f": ind.ema(cl, self.p["fast"]), "s": ind.ema(cl, self.p["slow"]),
                         "atr": ind.atr(h4.b, 14), "adx": ad, "hi": dc_hi, "lo": dc_lo}

    def _signal(self, d, i):
        f, s = d["f"], d["s"]
        if self.p["mode"] == "donchian":
            c = d["h4"].b[i]["c"]
            return 1 if c > d["hi"][i] else (-1 if c < d["lo"][i] else 0)
        if f[i] > s[i] and f[i - 1] <= s[i - 1]:
            return 1
        if f[i] < s[i] and f[i - 1] >= s[i - 1]:
            return -1
        return 0

    def step(self, T, br, ctx):
        for c, d in self.d.items():
            i = d["h4"].closes_at(T)
            if i is None or i < self.p["slow"] + 5 or not d["atr"][i] or c not in br.px:
                continue
            b, a = d["h4"].b[i], d["atr"][i]
            sig = self._signal(d, i)
            p = br.position(c)
            if p:
                br.tighten(c, b["c"] - p["side"] * self.p["atr_mult"] * a)        # chandelier trail
                if self.p["exit_on_cross"] and sig == -p["side"]:
                    br.close(c, T, "opposite crossover")
                    p = None
            if p or sig == 0:
                continue
            if self.p["adx_min"] and (d["adx"][i] or 0) < self.p["adx_min"]:
                continue
            if len(br.acct["positions"]) >= self.p["max_open"]:
                continue
            stop = b["c"] - sig * self.p["atr_mult"] * a
            qty = br.size_for_risk(c, b["c"], stop, self.p["risk_pct"])
            br.market(c, sig, qty, T, stop, setup=f"{self.p['mode']} {self.p['fast']}/{self.p['slow']} cross",
                      note=f"ADX {d['adx'][i]:.0f}", signal_t=T)


# =========================================================================== regime + news
class NewsRegime(Strategy):
    """News decides IF and which way; the market regime decides HOW.

    The 12-month backtest showed the regime rules on their own (buy pullbacks in ADX trends,
    fade Bollinger extremes in ranges) lost money in both halves, so this bot never trades
    on them alone: every entry needs the AI news analyst's time-decayed view to point the
    same way. The regime then sets the trade's shape:
      trend (ADX >= 25), news WITH the trend   -> 2 ATR stop, 3 ATR trailing stop, up to 5 days
      trend, news AGAINST the trend            -> only if the news is very strong; 1.5 ATR stop,
                                                  2 ATR target, 24h time stop
      range (ADX < 20)                         -> 1.5 ATR stop, target the far Bollinger band, 48h
      in between                               -> 2 ATR stop, 3 ATR target, 24h
    Size: 1% of equity at risk (ATR-scaled), 1.5% when the news is strong. One entry per
    market per 24h so a single story cannot be traded over and over."""
    name, every_h = "news_regime", 1
    description = ("News trading with regime switching: the AI news analyst decides whether and which way "
                   "to trade; ADX trend/range regime sets the stop, target and holding time (ATR-scaled); "
                   "never trades on technicals alone")

    def __init__(self, coins, adx_trend=25.0, adx_range=20.0, risk_pct=1.0, strong_risk_pct=1.5, news_min=0.2,
                 news_strong=0.5, counter_min=0.7, cooldown_h=24, max_open=6):
        self.coins = coins
        self.p = dict(adx_trend=adx_trend, adx_range=adx_range, risk_pct=risk_pct, strong_risk_pct=strong_risk_pct,
                      news_min=news_min, news_strong=news_strong, counter_min=counter_min, cooldown_h=cooldown_h,
                      max_open=max_open)
        self.d = {}

    def prepare(self, data):
        for c in self.coins:
            if c not in data or len(data[c]) < 300:
                continue
            h4 = frame(data[c], 4)
            cl = [x["c"] for x in h4.b]
            ad, pdi, mdi = ind.adx(h4.b, 14)
            self.d[c] = {"h4": h4, "mid": ind.sma(cl, 20), "sd": ind.stdev(cl, 20), "atr": ind.atr(h4.b, 14),
                         "adx": ad, "pdi": pdi, "mdi": mdi}

    def regime(self, d, i):
        a = d["adx"][i] or 0
        if a >= self.p["adx_trend"]:
            return "trend", (1 if d["pdi"][i] > d["mdi"][i] else -1)
        if a < self.p["adx_range"]:
            return "range", 0
        return "transition", 0

    def step(self, T, br, ctx):
        last = br.acct["mem"].setdefault("last_entry", {})
        for c, d in self.d.items():
            i = d["h4"].at(T)
            if i < 40 or not d["atr"][i] or d["sd"][i] is None or c not in br.px:
                continue
            p = br.position(c)
            a, px = d["atr"][i], br.px[c]
            if p:
                if p.get("setup") == "news + trend":
                    br.tighten(c, px - p["side"] * 3.0 * a)
                continue
            score, conf = _news(ctx, c, T)
            nv = score * conf
            if abs(nv) < self.p["news_min"] or T - last.get(c, 0) < self.p["cooldown_h"] * HOUR:
                continue
            if len(br.acct["positions"]) >= self.p["max_open"]:
                continue
            side = 1 if nv > 0 else -1
            reg, tdir = self.regime(d, i)
            if reg == "trend" and side == tdir:
                setup, stop, kw = "news + trend", px - side * 2.0 * a, dict(time_stop=T + 5 * DAY)
            elif reg == "trend":
                if abs(nv) < self.p["counter_min"]:
                    continue
                setup, stop = "news vs trend", px - side * 1.5 * a
                kw = dict(target=px + side * 2.0 * a, time_stop=T + DAY)
            elif reg == "range":
                band = d["mid"][i] + side * 2.0 * d["sd"][i]
                if (band - px) * side <= a:                     # already at the far side of the range
                    continue
                setup, stop, kw = "news in range", px - side * 1.5 * a, dict(target=band, time_stop=T + 2 * DAY)
            else:
                setup, stop = "news, no clear regime", px - side * 2.0 * a
                kw = dict(target=px + side * 3.0 * a, time_stop=T + DAY)
            risk = self.p["strong_risk_pct"] if abs(nv) >= self.p["news_strong"] else self.p["risk_pct"]
            qty = br.size_for_risk(c, px, stop, risk)
            if br.market(c, side, qty, T, stop, setup=setup, signal_t=T,
                         note=f"news view {nv:+.2f} (score {score:+.2f}, conf {conf:.2f}); regime {reg}, "
                              f"ADX {d['adx'][i]:.0f}", **kw):
                last[c] = T


# =========================================================================== relative value
class RelativeValue(Strategy):
    name, every_h = "rel_value", 24
    description = ("Relative value, market-neutral: top-30 Hyperliquid coins by market cap; once a day go long "
                   "the 5 strongest and short the 5 weakest over the last 7 days (equal dollars each side, keep a "
                   "coin while it stays in the top/bottom 8). Skips coins whose move is extreme (more than 2 "
                   "standard deviations from the pack): crashed coins tend to bounce violently. Trades smaller (down to "
                   "1/3 size) when its own daily swings are bigger than usual")

    def __init__(self, coins, mode="residual", lookback_d=3, beta_d=30, n_side=5, gross_lev=2.0, hold_band=3,
                 funding=None, sizing="equal", max_z=None, leg_stop_k=None, vol_scale=False):
        self.coins = coins
        self.p = dict(mode=mode, lookback_d=lookback_d, beta_d=beta_d, n_side=n_side, gross_lev=gross_lev,
                      hold_band=hold_band, sizing=sizing, max_z=max_z, leg_stop_k=leg_stop_k, vol_scale=vol_scale)
        self.funding = funding
        self.d, self.btc = {}, None

    def prepare(self, data):
        for c in self.coins:
            if c in data and len(data[c]) > 10:
                self.d[c] = frame(data[c], 24)
        self.btc = self.d.get("BTC")

    def _ret(self, tf, T, days):
        i = tf.at(T)
        if i - days < 0 or tf.close_t[i] != T:
            return None
        return math.log(tf.b[i]["c"] / tf.b[i - days]["c"])

    def _beta(self, tf, T):
        i, j = tf.at(T), self.btc.at(T)
        n = self.p["beta_d"]
        if i < n or j < n:
            return None
        x = [math.log(self.btc.b[j - k]["c"] / self.btc.b[j - k - 1]["c"]) for k in range(n)]
        y = [math.log(tf.b[i - k]["c"] / tf.b[i - k - 1]["c"]) for k in range(n)]
        mx, my = sum(x) / n, sum(y) / n
        vx = sum((v - mx) ** 2 for v in x)
        return sum((a - mx) * (b - my) for a, b in zip(x, y)) / vx if vx else None

    def _vol(self, tf, T, n=30):
        i = tf.at(T)
        if i < n:
            return None
        r = [math.log(tf.b[i - k]["c"] / tf.b[i - k - 1]["c"]) for k in range(n)]
        m = sum(r) / n
        return math.sqrt(sum((x - m) ** 2 for x in r) / n) or None

    def _book_scale(self, br, eq):
        """Volatility targeting (Barroso & Santa-Clara): when the book's own daily swings over the
        last 20 days are bigger than its usual (median of the last 120 days), shrink new legs in
        proportion. Never scales above 1x."""
        hist = br.acct["mem"].setdefault("eq_hist", [])
        hist.append(eq)
        del hist[:-140]
        if not self.p["vol_scale"] or len(hist) < 40:
            return 1.0
        r = [hist[i] / hist[i - 1] - 1 for i in range(1, len(hist)) if hist[i - 1] > 0]
        sd = lambda x: math.sqrt(sum((v - sum(x) / len(x)) ** 2 for v in x) / len(x))
        recent = sd(r[-20:])
        rolling = sorted(sd(r[i - 20:i]) for i in range(20, len(r) + 1))
        usual = rolling[len(rolling) // 2]
        return max(0.33, min(1.0, usual / recent)) if recent else 1.0

    def _size(self, c, T, vols):
        """Leg size multiplier. inv_vol: wild coins get smaller legs; beta: each leg carries
        about the same BTC-beta, so a long book of calm coins vs a short book of wild ones is
        not secretly net short the market (what hurt in the Oct 15 bounce)."""
        if self.p["sizing"] == "inv_vol" and vols.get(c):
            med = sorted(v for v in vols.values() if v)[len([v for v in vols.values() if v]) // 2]
            return max(0.33, min(2.0, med / vols[c]))
        if self.p["sizing"] == "beta":
            b = 1.0 if c == "BTC" else self._beta(self.d[c], T)
            return max(0.33, min(2.0, 1 / b)) if b and b > 0 else 1.0
        return 1.0

    def scores(self, T):
        """Higher score = more 'expensive' (short candidate)."""
        out = {}
        rb = self._ret(self.btc, T, self.p["lookback_d"]) if self.btc else None
        for c, tf in self.d.items():
            if self.p["mode"] == "carry":
                f = self.funding(c, T) if self.funding else None
                if f is not None:
                    out[c] = f
                continue
            r = self._ret(tf, T, self.p["lookback_d"])
            if r is None:
                continue
            if self.p["mode"] == "residual":
                if c == "BTC" or rb is None:
                    continue
                beta = self._beta(tf, T)
                if beta is None:
                    continue
                out[c] = r - beta * rb
            elif self.p["mode"] == "momentum":
                out[c] = -r                                    # winners get low score -> long
            else:
                out[c] = r
        return out

    def step(self, T, br, ctx):
        sc = self.scores(T)
        n = self.p["n_side"]
        if len(sc) < 2 * n + 2:
            return
        ranked = sorted(sc, key=sc.get)
        blocked = set()
        if self.p["max_z"]:                                  # skip the most extreme movers for NEW entries:
            vals = list(sc.values())                          # crashed coins bounce hardest (squeezes)
            mu = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals)) or 1.0
            blocked = {c for c, v in sc.items() if abs(v - mu) / sd > self.p["max_z"]}
        keep_l = set(ranked[:n + self.p["hold_band"]])       # hysteresis: fewer, cheaper trades
        keep_s = set(ranked[-(n + self.p["hold_band"]):])
        for c, p in list(br.acct["positions"].items()):
            if (p["side"] > 0 and c not in keep_l) or (p["side"] < 0 and c not in keep_s) or c not in sc:
                br.close(c, T, "rebalance")
        eq = br.equity()
        leg = eq * self.p["gross_lev"] / (2 * n) * self._book_scale(br, eq)
        vols = {c: self._vol(self.d[c], T) for c in sc} if self.p["sizing"] == "inv_vol" or self.p["leg_stop_k"] else {}
        held_l = sum(1 for p in br.acct["positions"].values() if p["side"] > 0)
        held_s = sum(1 for p in br.acct["positions"].values() if p["side"] < 0)
        k = n + (self.p["hold_band"] if blocked else 0)     # skipped extremes -> take the next in line
        for side, names in ((1, ranked[:k]), (-1, ranked[::-1][:k])):
            for c in names:
                if (held_l if side > 0 else held_s) >= n:
                    break
                if br.position(c) or c not in br.px or c in blocked:
                    continue
                k = self.p["leg_stop_k"]
                stop_dist = min(0.5, k * vols[c]) if k and vols.get(c) else 0.5     # default: disaster stop only
                if br.market(c, side, leg * self._size(c, T, vols) / br.px[c], T, stop=br.px[c] * (1 - side * stop_dist),
                             setup=f"RV {self.p['mode']}",
                             note=f"score {sc[c]:+.4f}", signal_t=T, track_r=False):
                    if side > 0:
                        held_l += 1
                    else:
                        held_s += 1
