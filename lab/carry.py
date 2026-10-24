"""Funding carry across two exchanges (Hyperliquid x Aster): a delta-neutral bot.

For each coin listed on both exchanges, funding is paid every hour on Hyperliquid and every
1-8 hours on Aster, at different rates. When one exchange's funding stays well above the
other's, short the coin where funding is high and buy it where funding is low: the price moves
cancel out and the funding difference is collected.

Rules (the earlier 12-month study's defaults, see BACKTEST.md):
  * every 3 days rank coins by the 7-day average funding gap (% a year)
  * hold the 5 biggest gaps, each at least 20% a year; keep a pair while its gap stays above
    10% in the same direction (fewer, cheaper trades)
  * each pair: equal dollars on both exchanges, 3x margin over the whole book
  * paired stop: if either leg has moved 25% against itself (well before a 3x leg would be
    liquidated at ~30%), or the two prices drift more than 15% apart (a token mismatch or a frozen
    price on one exchange), close both legs at once; if one leg is ever closed on its own, the
    other is closed straight away. Backtest: tighter stops (15% / 3%) fired on normal noise and cut
    the return; no drift guard let one broken pair turn the year negative

Instruments in the engine: "hlc:<HL coin>" for the Hyperliquid leg (same prices as the coin,
kept separate so it never collides with the directional bots' positions) and "ast:<Aster
symbol>" for the Aster leg."""
import math
import time

from lab.engine import HOUR, Strategy

HOURS_YEAR = 24 * 365
KDENOM = {"KPEPE": ("PEPE", 1e3), "KBONK": ("BONK", 1e3), "KSHIB": ("SHIB", 1e3), "KFLOKI": ("FLOKI", 1e3),
          "KLUNC": ("LUNC", 1e3), "KNEIRO": ("NEIRO", 1e3), "KDOGS": ("DOGS", 1e3)}
ALIASES = {"RNDR": "RENDER", "MATIC": "POL"}


def norm_sym(s):
    """Exchange symbol -> (canonical coin, price multiplier). kPEPE / 1000PEPEUSDT -> (PEPE, 1000)."""
    s = s.upper()
    for suf in ("USDT", "USDC", "USD"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
    mult = 1.0
    if s in KDENOM:
        s, mult = KDENOM[s]
    else:
        for pre, m in (("1000000", 1e6), ("1M", 1e6), ("1000", 1e3)):
            if s.startswith(pre) and len(s) > len(pre):
                s, mult = s[len(pre):], m
                break
    return ALIASES.get(s, s), mult


class FundingBook:
    """Hourly funding of both legs of every pair, as % a year, for the trailing-average signal.

    hl[sym]  = {hour_ms: rate per hour}     (Hyperliquid pays hourly)
    ast[sym] = sorted [(time_ms, rate per interval, interval_h)]  (Aster's settled payments)"""

    def __init__(self):
        self.hl, self.ast = {}, {}
        self._cache = {}

    def gap_series(self, sym, t_from, t_to):
        """Hourly Aster APR minus Hyperliquid APR over [t_from, t_to). None where unknown."""
        hl, ev = self.hl.get(sym, {}), self.ast.get(sym, [])
        out, j, last = [], 0, None
        t = t_from // HOUR * HOUR
        while t < t_to:
            while j < len(ev) and ev[j][0] <= t:
                last = ev[j][1] * HOURS_YEAR / max(ev[j][2], 1.0) * 100
                j += 1
            h = hl.get(t)
            out.append(None if h is None or last is None else last - h * HOURS_YEAR * 100)
            t += HOUR
        return out

    def mean_gap(self, sym, T, window_h):
        """Average gap over the window ending at T (needs 80% of the hours), else None."""
        key = (sym, T, window_h)
        if key not in self._cache:
            g = [x for x in self.gap_series(sym, T - window_h * HOUR, T) if x is not None]
            self._cache[key] = sum(g) / len(g) if len(g) >= 0.8 * window_h else None
        return self._cache[key]


class FundingCarry(Strategy):
    name, every_h = "carry", 1
    description = ("Funding carry (no price bet): for coins listed on both Hyperliquid and Aster, short where funding "
                   "is high and buy where it is low, equal dollars, so price moves cancel and the funding gap is "
                   "collected. Holds the 5 biggest 7-day gaps (20%+ a year), re-checked every 3 days; both legs are "
                   "closed together if either moves 25% against itself or the two prices drift 15% apart")

    def __init__(self, pairs, book, window_h=168, rebalance_h=72, top_k=5, min_apr=20.0, keep_frac=0.5,
                 lev=3.0, max_px_div=1.0, leg_stop=0.25, drift_stop=0.15):
        """pairs: [{"sym", "hl": "hlc:<coin>", "ast": "ast:<symbol>", "hl_mult", "ast_mult"}]"""
        self.pairs = {p["sym"]: p for p in pairs}
        self.book = book
        self.coins = [p["hl"] for p in pairs] + [p["ast"] for p in pairs]
        self.p = dict(window_h=window_h, rebalance_h=rebalance_h, top_k=top_k, min_apr=min_apr, keep_frac=keep_frac,
                      lev=lev, max_px_div=max_px_div, leg_stop=leg_stop, drift_stop=drift_stop)

    def prepare(self, data):
        pass

    def _close_pair(self, br, sym, T, why):
        pr = self.pairs.get(sym)
        legs = br.acct["mem"].setdefault("pairs", {}).pop(sym, None)
        for k in ((pr["hl"], pr["ast"]) if pr else (legs or {}).get("legs", ())):
            if br.position(k) and k in br.px:
                br.close(k, T, why)

    def step(self, T, br, ctx):
        held = br.acct["mem"].setdefault("pairs", {})
        # ---- every hour: protect what is open
        for sym in list(held):
            pr = self.pairs.get(sym)
            if not pr:
                if not all(k in br.px for k in held[sym]["legs"]):
                    continue                                      # no prices (exchange or feed down): wait
                for k in held[sym]["legs"]:                       # coin left the universe: unwind
                    if br.position(k):
                        br.close(k, T, "carry: pair left the universe")
                held.pop(sym)
                continue
            a, b = br.position(pr["hl"]), br.position(pr["ast"])
            if not a or not b:
                self._close_pair(br, sym, T, "carry: other leg closed, closing this one too")
                continue
            if pr["hl"] not in br.px or pr["ast"] not in br.px:
                continue
            ra = br.px[pr["hl"]] / a["entry"] - 1
            rb = br.px[pr["ast"]] / b["entry"] - 1
            if -a["side"] * ra > self.p["leg_stop"] or -b["side"] * rb > self.p["leg_stop"]:
                self._close_pair(br, sym, T, f"carry: paired stop ({max(-a['side'] * ra, -b['side'] * rb):.0%} against a leg)")
            elif abs(ra - rb) > self.p["drift_stop"]:
                self._close_pair(br, sym, T, f"carry: prices drifted {abs(ra - rb):.1%} apart")
        if (T // HOUR) % self.p["rebalance_h"]:
            return
        # ---- rebalance: keep / drop
        gaps = {s: self.book.mean_gap(s, T, self.p["window_h"]) for s in self.pairs}
        for sym in list(held):
            g = gaps.get(sym)
            if g is None or math.copysign(1, g) != held[sym]["dir"] or abs(g) < self.p["keep_frac"] * self.p["min_apr"]:
                self._close_pair(br, sym, T, f"carry: gap faded ({'n/a' if g is None else f'{g:+.0f}%'} a year)")
        # ---- fill free slots with the biggest gaps
        eq = br.equity()
        N = eq * self.p["lev"] / (2 * self.p["top_k"])           # dollars per leg
        for sym, g in sorted(((s, g) for s, g in gaps.items() if g is not None), key=lambda x: -abs(x[1])):
            if len(held) >= self.p["top_k"] or abs(g) < self.p["min_apr"]:
                break
            if sym in held:
                continue
            pr = self.pairs[sym]
            ph, pa = br.px.get(pr["hl"]), br.px.get(pr["ast"])
            if not ph or not pa or br.position(pr["hl"]) or br.position(pr["ast"]):
                continue
            uh, ua = ph / pr["hl_mult"], pa / pr["ast_mult"]
            if abs(uh - ua) / ((uh + ua) / 2) * 100 > self.p["max_px_div"]:
                continue                                          # not the same token, or a stale price
            d = 1 if g > 0 else -1                                # +1: Aster funding higher -> short Aster, long HL
            note = f"7-day funding gap {g:+.0f}% a year (Aster minus Hyperliquid)"
            ok1 = br.market(pr["hl"], d, N / ph, T, stop=ph * (1 - d * 0.5), setup="carry leg (Hyperliquid)",
                            note=note, signal_t=T, track_r=False)
            ok2 = ok1 and br.market(pr["ast"], -d, N / pa, T, stop=pa * (1 + d * 0.5), setup="carry leg (Aster)",
                                    note=note, signal_t=T, track_r=False)
            if ok1 and not ok2:
                br.close(pr["hl"], T, "carry: could not open the Aster leg")
                continue
            if ok2:
                held[sym] = {"dir": d, "legs": [pr["hl"], pr["ast"]], "gap": round(g, 1), "opened": T}


def build_live(hl_ctx, have, held, errors, top=40, history_h=216):
    """Live inputs for the carry bot: the `top` coins on both exchanges by the smaller of the two
    24h volumes (plus any pair still held), their funding history, prices and current rates.
    Returns pairs, FundingBook, candles {instrument: [...]}, funding(instrument, t) -> rate for that
    hour (Hyperliquid's hourly history, else its current rate; Aster pays only at its settlements),
    marks {instrument: price}."""
    from lab import market
    try:
        ast = market.aster_markets()
    except Exception as e:                                  # Aster down: carry pauses, nothing else does
        errors["carry:aster"] = str(e)[:120]
        return [], FundingBook(), {}, (lambda c, t: 0.0), {}
    hl = {}
    for name, v in hl_ctx.items():
        if ":" in name:                                      # trade.xyz stock/commodity perps: not on Aster
            continue
        sym, mult = norm_sym(name)
        if sym not in hl or v["vol"] > hl[sym][2]["vol"]:
            hl[sym] = (name, mult, v)
    best = {}
    for s, v in ast.items():
        sym, mult = norm_sym(s)
        if sym in hl and (sym not in best or v["vol"] > best[sym][1]["vol"]):
            best[sym] = (s, v, mult)
    ranked = sorted(best, key=lambda k: -min(hl[k][2]["vol"], best[k][1]["vol"]))
    chosen = list(dict.fromkeys(ranked[:top] + [s for s in held if s in best]))
    pairs, book, data, fnow, marks = [], FundingBook(), {}, {}, {}
    for sym in chosen:
        hname, hmult, hv = hl[sym]
        aname, av, amult = best[sym]
        hk, ak = "hlc:" + hname, "ast:" + aname
        try:
            hf = market.funding_history(hname, history_h)
            time.sleep(0.4)
            af = market.aster_funding(aname, history_h)
            data[ak] = market.aster_candles(aname, 240)
            data[hk] = have[hname] if hname in have and len(have[hname]) > 200 else market.candles(hname, 240, "1h", pause=0.4)
        except Exception as e:
            errors[f"carry:{sym}"] = str(e)[:100]
            continue
        book.hl[sym] = {t // HOUR * HOUR: x for t, x in hf}
        ev, prev = [], None
        for t, x in sorted(af):
            ev.append((t, x, max(1.0, (t - prev) / HOUR) if prev else av["interval_h"]))
            prev = t
        book.ast[sym] = ev
        fnow[hk] = (book.hl[sym], hv["funding"])
        fnow[ak] = ({t // HOUR * HOUR: x for t, x, _ in ev}, 0.0)            # paid once, at the settlement hour
        marks[hk], marks[ak] = hv["mark"], av["mark"]
        pairs.append({"sym": sym, "hl": hk, "ast": ak, "hl_mult": hmult, "ast_mult": amult})

    def funding(c, t):
        hist, now = fnow.get(c, ({}, 0.0))
        return hist.get(t // HOUR * HOUR, now)
    return pairs, book, data, funding, marks
