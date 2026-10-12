"""Causal indicators on candle lists. Every output value at index i uses only bars 0..i,
so the same arrays serve the backtest and the live bot without look-ahead.

Candles are dicts {t, o, h, l, c} with t = open time (ms)."""
import math

HOUR = 3_600_000


def resample(bars, hours):
    """Aggregate 1h candles into complete `hours`-long candles aligned to UTC (4h, 24h ...)."""
    span = hours * HOUR
    out, cur = [], None
    for b in bars:
        k = b["t"] // span * span
        if cur is None or cur["t"] != k:
            if cur is not None and cur["n"] == hours:
                out.append(cur)
            cur = {"t": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "n": 0}
        cur["h"] = max(cur["h"], b["h"])
        cur["l"] = min(cur["l"], b["l"])
        cur["c"] = b["c"]
        cur["n"] += 1
    if cur is not None and cur["n"] == hours:
        out.append(cur)
    return [{k: v for k, v in x.items() if k != "n"} for x in out]


def ema(xs, n):
    a, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else e + a * (x - e)
        out.append(e)
    return out


def sma(xs, n):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def stdev(xs, n):
    out = []
    for i in range(len(xs)):
        if i < n - 1:
            out.append(None)
            continue
        w = xs[i - n + 1:i + 1]
        m = sum(w) / n
        out.append(math.sqrt(sum((x - m) ** 2 for x in w) / n))
    return out


def _rma(xs, n):
    out, v = [], None
    for i, x in enumerate(xs):
        if i < n - 1:
            out.append(None)
            continue
        v = sum(xs[:n]) / n if v is None else (v * (n - 1) + x) / n
        out.append(v)
    return out


def true_range(bars):
    tr = []
    for i, b in enumerate(bars):
        pc = bars[i - 1]["c"] if i else b["c"]
        tr.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return tr


def atr(bars, n=14):
    return _rma(true_range(bars), n)


def adx(bars, n=14):
    """Wilder's ADX with +DI / -DI. Returns three lists."""
    pdm, mdm = [0.0], [0.0]
    for i in range(1, len(bars)):
        up = bars[i]["h"] - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - bars[i]["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    tr = _rma(true_range(bars), n)
    p, m = _rma(pdm, n), _rma(mdm, n)
    pdi, mdi, dx = [], [], []
    for t, a, b in zip(tr, p, m):
        if t is None or not t:
            pdi.append(None), mdi.append(None), dx.append(None)
            continue
        pi, mi = 100 * a / t, 100 * b / t
        pdi.append(pi), mdi.append(mi)
        dx.append(100 * abs(pi - mi) / (pi + mi) if pi + mi else 0.0)
    first = next((i for i, v in enumerate(dx) if v is not None), len(dx))
    tail = _rma(dx[first:], n)
    return [None] * first + tail, pdi, mdi


def pivots(bars, n):
    """Swing points confirmed n bars later. Returns per-bar lists of (pivot_index, price) that
    become *known* at that bar: ph[i] / pl[i] is None or the pivot confirmed at bar i."""
    ph, pl = [None] * len(bars), [None] * len(bars)
    for i in range(2 * n, len(bars)):
        j = i - n
        hs = [bars[k]["h"] for k in range(j - n, i + 1)]
        ls = [bars[k]["l"] for k in range(j - n, i + 1)]
        if bars[j]["h"] == max(hs) and all(bars[k]["h"] < bars[j]["h"] for k in range(j - n, j)):
            ph[i] = (j, bars[j]["h"])
        if bars[j]["l"] == min(ls) and all(bars[k]["l"] > bars[j]["l"] for k in range(j - n, j)):
            pl[i] = (j, bars[j]["l"])
    return ph, pl


def swing_structure(bars, n=3):
    """Market structure the way the EmperorBTC notes define it (closes only, deep swings):

    * Bullish: a close above the highest swing high of the current leg is a BOS; the lowest low
      between that swing high and the break becomes the protected higher low (HL).
    * The trend only turns bearish when a candle CLOSES below the protected HL (the deep swing
      low), not on every minor dip. Mirror image for bearish structure (protected LH).

    Returns per-bar dicts: trend (+1/-1/0), protect (HL or LH), leg (HH of an up-leg or LL of a
    down-leg so far: the 'previous high' a pullback long targets), bos_t (time of last BOS or
    trend change), event ('bos', 'flip' or None)."""
    ph, pl = pivots(bars, n)
    out = []
    trend, protect, prot_i, leg = 0, None, None, None
    hi_lvl = lo_lvl = None          # (index, price): swing to break for the next BOS
    bos_t = None
    for i, b in enumerate(bars):
        ev = None
        if ph[i] and (hi_lvl is None or trend != 1 or ph[i][1] > hi_lvl[1]):
            hi_lvl = ph[i]
        if pl[i] and (lo_lvl is None or trend != -1 or pl[i][1] < lo_lvl[1]):
            lo_lvl = pl[i]
        c = b["c"]
        if trend >= 0 and hi_lvl and c > hi_lvl[1]:
            j = min(range(hi_lvl[0], i + 1), key=lambda k: bars[k]["l"])
            ev = "bos" if trend == 1 else "flip"
            trend, protect, prot_i, hi_lvl, bos_t = 1, bars[j]["l"], j, None, b["t"]
            leg = b["h"]
        elif trend <= 0 and lo_lvl and c < lo_lvl[1]:
            j = max(range(lo_lvl[0], i + 1), key=lambda k: bars[k]["h"])
            ev = "bos" if trend == -1 else "flip"
            trend, protect, prot_i, lo_lvl, bos_t = -1, bars[j]["h"], j, None, b["t"]
            leg = b["l"]
        elif trend == 1 and c < protect:                    # deep swing low lost: bearish
            j = max(range(prot_i, i + 1), key=lambda k: bars[k]["h"])
            ev, trend, protect, prot_i, lo_lvl, bos_t = "flip", -1, bars[j]["h"], j, None, b["t"]
            leg = b["l"]
        elif trend == -1 and c > protect:                   # deep swing high reclaimed: bullish
            j = min(range(prot_i, i + 1), key=lambda k: bars[k]["l"])
            ev, trend, protect, prot_i, hi_lvl, bos_t = "flip", 1, bars[j]["l"], j, None, b["t"]
            leg = b["h"]
        elif trend == 1:
            leg = max(leg, b["h"])
        elif trend == -1:
            leg = min(leg, b["l"])
        out.append({"trend": trend, "protect": protect, "leg": leg, "bos_t": bos_t, "event": ev})
    return out


def internal_structure(bars, n=2):
    """Substructure (minor swings) for CHoCH: the internal trend flips when a candle closes
    beyond the most recent minor swing high / low. Returns per-bar dicts with itrend, flip
    (+1 bullish CHoCH, -1 bearish CHoCH, 0), lvl (the swing level that was broken)."""
    ph, pl = pivots(bars, n)
    out, it, last_h, last_l = [], 0, None, None
    for i, b in enumerate(bars):
        if ph[i]:
            last_h = ph[i][1]
        if pl[i]:
            last_l = pl[i][1]
        flip, lvl = 0, None
        if it <= 0 and last_h is not None and b["c"] > last_h:
            it, flip, lvl, last_h = 1, 1, last_h, None
        elif it >= 0 and last_l is not None and b["c"] < last_l:
            it, flip, lvl, last_l = -1, -1, last_l, None
        out.append({"itrend": it, "flip": flip, "lvl": lvl})
    return out
