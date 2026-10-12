"""Event engine shared by the backtest and the live paper bot.

Time moves one hourly candle at a time. For every candle the broker first manages what is
already open (limit-order fills, stop, partial take-profit, target, time stop, funding), then,
at the close of the candle, each strategy whose timeframe just closed gets to act using only
data up to that close. Market orders fill at that close (plus slippage).

Because the live bot replays every hour it missed (GitHub's scheduler is often hours late),
a trade's signal time and fill price are the real candle close it was decided on, never the
time the runner happened to wake up; `booked` records when the bot actually wrote it down.

Conservative fills: if one candle touches both the stop and a target, the stop wins; a candle
that opens through the stop fills at the open (gap); targets fill as resting limit orders."""
HOUR = 3_600_000
SLIPPAGE_BPS = 2.0


def fee_bps(coin, maker=False):
    xyz = coin.startswith("xyz:")                       # trade.xyz HIP-3 markets charge 2x
    return (3.0 if xyz else 1.5) if maker else (9.0 if xyz else 4.5)


def new_account(cash):
    return {"cash": cash, "positions": {}, "orders": {}, "mem": {}}


def equity(acct, px):
    eq = acct["cash"]
    for c, p in acct["positions"].items():
        eq += p["side"] * p["qty"] * (px.get(c, p["entry"]) - p["entry"])
    return eq


def gross(acct, px):
    return sum(p["qty"] * px.get(c, p["entry"]) for c, p in acct["positions"].items())


class Broker:
    """Paper broker for one strategy account. `log` receives closed-trade records."""

    def __init__(self, name, acct, px, log, booked=None):
        self.name, self.acct, self.px, self.log, self.booked = name, acct, px, log, booked

    # ---------------------------------------------------------------- bookkeeping
    def _fee(self, coin, notional, maker=False):
        return notional * fee_bps(coin, maker) / 1e4

    def _record(self, coin, p, qty, px, ts, reason, fee):
        gross_pnl = p["side"] * qty * (px - p["entry"])
        share = qty / p["qty0"]
        pnl = gross_pnl - fee - p["fee_in"] * share + p["funding"] * share
        self.log.append({
            "strategy": self.name, "asset": coin, "side": "long" if p["side"] > 0 else "short",
            "setup": p.get("setup", ""), "signal_t": p.get("signal_t"), "opened": p["opened"],
            "closed": ts, "entry": p["entry"], "exit": px, "qty": qty,
            "stop0": p["stop0"], "target0": p.get("target0"),
            "move_pct": round(p["side"] * (px / p["entry"] - 1) * 100, 3),
            "r": round(p["side"] * (px - p["entry"]) / p["risk_px"], 2) if p.get("risk_px") else None,
            "pnl": round(pnl, 4), "funding": round(p["funding"] * share, 4), "reason": reason,
            "booked": self.booked or ts, "note": p.get("note", "")})

    def _exit(self, coin, qty, px, ts, reason, maker=False):
        p = self.acct["positions"][coin]
        qty = min(qty, p["qty"])
        fee = self._fee(coin, qty * px, maker)
        self.acct["cash"] += p["side"] * qty * (px - p["entry"]) - fee
        self._record(coin, p, qty, px, ts, reason, fee)
        p["qty"] -= qty
        if p["qty"] <= p["qty0"] * 1e-9:
            del self.acct["positions"][coin]

    # ---------------------------------------------------------------- strategy API
    def equity(self):
        return equity(self.acct, self.px)

    def position(self, coin):
        return self.acct["positions"].get(coin)

    def size_for_risk(self, coin, entry, stop, risk_pct, max_lev=5.0, max_gross=5.0):
        """Qty so that hitting the stop loses risk_pct of equity (EmperorBTC ch.1 sizing),
        capped by per-position and total leverage (the notes: never above 5x)."""
        eq = self.equity()
        dist = abs(entry - stop)
        if eq <= 0 or dist <= 0:
            return 0.0
        qty = eq * risk_pct / 100 / dist
        room = max(0.0, eq * max_gross - gross(self.acct, self.px))
        return max(0.0, min(qty, eq * max_lev / entry, room / entry))

    def market(self, coin, side, qty, ts, stop, target=None, tp1=None, tp1_frac=0.5, be_after_tp1=True,
               time_stop=None, setup="", note="", signal_t=None, track_r=True):
        px0 = self.px[coin]
        if qty * px0 < 10 or coin in self.acct["positions"]:          # $10 exchange minimum
            return False
        px = px0 * (1 + side * SLIPPAGE_BPS / 1e4)
        fee = self._fee(coin, qty * px)
        self.acct["cash"] -= fee
        self.acct["positions"][coin] = {
            "side": side, "qty": qty, "qty0": qty, "entry": px, "stop": stop, "stop0": stop,
            "target": target, "target0": target, "tp1": tp1, "tp1_frac": tp1_frac, "be": be_after_tp1,
            "time_stop": time_stop, "opened": ts, "signal_t": signal_t or ts, "fee_in": fee,
            "funding": 0.0, "risk_px": abs(px - stop) if stop and track_r else None, "setup": setup, "note": note}
        return True

    def limit(self, coin, side, price, qty, ts, expires, **kw):
        if qty * price < 10 or coin in self.acct["positions"]:
            return False
        self.acct["orders"][coin] = {"side": side, "price": price, "qty": qty, "placed": ts,
                                     "expires": expires, "kw": kw}
        return True

    def cancel(self, coin):
        self.acct["orders"].pop(coin, None)

    def close(self, coin, ts, reason):
        p = self.acct["positions"].get(coin)
        if p:
            self._exit(coin, p["qty"], self.px[coin] * (1 - p["side"] * SLIPPAGE_BPS / 1e4), ts, reason)

    def tighten(self, coin, new_stop):
        """Move a stop only in the trade's favour."""
        p = self.acct["positions"].get(coin)
        if p and new_stop is not None:
            p["stop"] = max(p["stop"], new_stop) if p["side"] > 0 else min(p["stop"], new_stop)

    # ---------------------------------------------------------------- per-candle management
    def on_bar(self, coin, b, rate, dur=HOUR):
        """Manage one closed candle of length dur (ms). rate = hourly funding rate during it."""
        end = b["t"] + dur
        o = self.acct["orders"].get(coin)
        if o and b["t"] >= o["placed"]:
            if end > o["expires"]:
                del self.acct["orders"][coin]
            elif (o["side"] > 0 and b["l"] <= o["price"]) or (o["side"] < 0 and b["h"] >= o["price"]):
                del self.acct["orders"][coin]
                fill = min(b["o"], o["price"]) if o["side"] > 0 else max(b["o"], o["price"])
                saved = self.px[coin]
                self.px[coin] = fill / (1 + o["side"] * SLIPPAGE_BPS / 1e4)   # limit: no slippage
                self.market(coin, o["side"], o["qty"], b["t"], **o["kw"])
                self.px[coin] = saved
                p = self.acct["positions"].get(coin)
                if p:                                                       # maker fee, not taker
                    refund = self._fee(coin, p["qty"] * fill) - self._fee(coin, p["qty"] * fill, True)
                    self.acct["cash"] += refund
                    p["fee_in"] -= refund
                    if (p["side"] > 0 and b["l"] <= p["stop"]) or (p["side"] < 0 and b["h"] >= p["stop"]):
                        self._exit(coin, p["qty"], p["stop"] * (1 - p["side"] * SLIPPAGE_BPS / 1e4), end, "stop-loss")
                return
        p = self.acct["positions"].get(coin)
        if not p or b["t"] < p["opened"]:
            return
        s = p["side"]
        pay = -s * p["qty"] * b["c"] * rate * dur / HOUR       # longs pay positive funding
        p["funding"] += pay
        self.acct["cash"] += pay
        if (s > 0 and b["l"] <= p["stop"]) or (s < 0 and b["h"] >= p["stop"]):
            px = min(b["o"], p["stop"]) if s > 0 else max(b["o"], p["stop"])
            why = "stop-loss" if s * (p["stop"] - p["entry"]) < 0 else "trailing/breakeven stop"
            self._exit(coin, p["qty"], px * (1 - s * SLIPPAGE_BPS / 1e4), end, why)
            return
        if p["tp1"] and (s * (b["h" if s > 0 else "l"] - p["tp1"]) >= 0):
            self._exit(coin, p["qty0"] * p["tp1_frac"], p["tp1"], end, "partial take-profit", maker=True)
            if coin not in self.acct["positions"]:
                return
            p["tp1"] = None
            if p["be"]:
                self.tighten(coin, p["entry"])
        if p["target"] and (s * (b["h" if s > 0 else "l"] - p["target"]) >= 0):
            self._exit(coin, p["qty"], p["target"], end, "take-profit", maker=True)
            return
        if p["time_stop"] and end >= p["time_stop"]:
            self._exit(coin, p["qty"], b["c"] * (1 - s * SLIPPAGE_BPS / 1e4), end, "time stop")


class Strategy:
    """Subclasses set name, every_h, coins and implement prepare() and step()."""
    name, every_h, coins, start_cash = "base", 4, [], 500.0

    def prepare(self, data):
        """data: {coin: [1h candles]}. Precompute causal indicators (lab.ind)."""

    def step(self, T, br, ctx):
        """Called at candle close time T (ms). br = Broker; ctx = shared context (news etc)."""


def bar_ms(bars):
    return bars[1]["t"] - bars[0]["t"] if len(bars) > 1 else HOUR


def run(strategies, accounts, data, funding, t_from, t_to, log, ctx=None, booked=None, curve=None,
        curve_every_h=24):
    """Replay every candle that closes in (t_from, t_to], hour by hour, through every strategy.
    data: {coin: candles} (1h, 4h or 1d; each coin's candles are handled at their close time).
    funding: {coin: hourly rate} or a function (coin, t) -> hourly rate."""
    dur = {c: bar_ms(bars) for c, bars in data.items()}
    by_close = {c: {b["t"] + dur[c]: b for b in bars} for c, bars in data.items()}
    px = {}
    for c, bars in data.items():                               # marks as of t_from
        prev = [b for b in bars if b["t"] + dur[c] <= t_from]
        if prev:
            px[c] = prev[-1]["c"]
    brokers = {s.name: Broker(s.name, accounts[s.name], px, log, booked) for s in strategies}
    T = t_from // HOUR * HOUR + HOUR
    while T <= t_to:
        for c in data:
            b = by_close[c].get(T)
            if not b:
                continue
            px[c] = b["c"]
            rate = funding(c, b["t"]) if callable(funding) else funding.get(c, 0.0)
            for s in strategies:
                if c in s.coins:
                    brokers[s.name].on_bar(c, b, rate, dur[c])
        for s in strategies:
            if (T // HOUR) % s.every_h == 0:
                s.step(T, brokers[s.name], ctx or {})
        if curve is not None and (T // HOUR) % curve_every_h == 0:
            curve.append({"t": T, "eq": {s.name: round(equity(accounts[s.name], px), 2) for s in strategies}})
        T += HOUR
    return px
