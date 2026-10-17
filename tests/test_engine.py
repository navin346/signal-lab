"""Engine, indicator and strategy-plumbing tests: python tests/test_engine.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lab import analyst, engine, ind  # noqa: E402
from lab.engine import HOUR  # noqa: E402


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


def broker(cash=500.0):
    acct, log, px = engine.new_account(cash), [], {"BTC": 100.0}
    return engine.Broker("t", acct, px, log), acct, log


def test_risk_sizing_and_stop_loss_costs():
    br, acct, log = broker()
    qty = br.size_for_risk("BTC", 100.0, 98.0, 2.0)          # 2% of $500 over a $2 stop
    assert abs(qty - 5.0) < 1e-9
    assert br.market("BTC", 1, qty, 0, stop=98.0, target=110.0)
    br.on_bar("BTC", bar(0, 100, 101, 97, 98), 0.0)
    t = log[-1]
    assert t["reason"] == "stop-loss" and not acct["positions"]
    entry, exit_ = 100 * 1.0002, 98 * 0.9998
    exp = 5 * (exit_ - entry) - 5 * entry * 4.5e-4 - 5 * exit_ * 4.5e-4
    assert abs(t["pnl"] - exp) < 1e-6 and abs(acct["cash"] - 500 - exp) < 1e-6
    assert -1.1 < t["r"] < -0.95


def test_leverage_cap():
    br, _, _ = broker()
    assert abs(br.size_for_risk("BTC", 100.0, 99.99, 2.0) - 25.0) < 1e-9   # 5x of $500 at $100


def test_gap_through_stop_fills_at_open():
    br, _, log = broker()
    br.market("BTC", 1, 1.0, 0, stop=98.0)
    br.on_bar("BTC", bar(0, 95, 96, 94, 95), 0.0)
    assert abs(log[-1]["exit"] - 95 * 0.9998) < 1e-9


def test_partial_then_breakeven_and_both_touched_is_stop():
    br, acct, log = broker()
    br.market("BTC", 1, 2.0, 0, stop=98.0, tp1=104.0, tp1_frac=0.5)
    br.on_bar("BTC", bar(0, 100, 104.5, 99, 104), 0.0)
    assert log[-1]["reason"] == "partial take-profit" and abs(acct["positions"]["BTC"]["qty"] - 1.0) < 1e-9
    assert acct["positions"]["BTC"]["stop"] >= acct["positions"]["BTC"]["entry"]       # moved to breakeven
    br.on_bar("BTC", bar(HOUR, 104, 104, 99, 100), 0.0)
    assert log[-1]["reason"] == "trailing/breakeven stop" and not acct["positions"]
    br.market("BTC", -1, 1.0, 2 * HOUR, stop=102.0, target=97.0)
    br.on_bar("BTC", bar(2 * HOUR, 100, 103, 96, 100), 0.0)                             # touches both
    assert log[-1]["reason"] == "stop-loss"


def test_funding_sign_and_time_stop():
    br, acct, log = broker()
    br.market("BTC", 1, 1.0, 0, stop=50.0, time_stop=2 * HOUR)
    c0 = acct["cash"]
    br.on_bar("BTC", bar(0, 100, 100, 100, 100), 0.001)       # positive funding: long pays
    assert acct["cash"] < c0
    br.on_bar("BTC", bar(HOUR, 100, 100, 100, 100), 0.0)
    assert log[-1]["reason"] == "time stop"


def test_limit_order_fill_and_expiry():
    br, acct, _ = broker()
    br.limit("BTC", 1, 99.0, 1.0, 0, expires=3 * HOUR, stop=97.0)
    br.on_bar("BTC", bar(0, 100, 101, 99.5, 100), 0.0)
    assert "BTC" in acct["orders"]
    br.on_bar("BTC", bar(HOUR, 100, 100, 98.5, 99), 0.0)
    p = acct["positions"]["BTC"]
    assert abs(p["entry"] - 99.0) < 1e-9 and "BTC" not in acct["orders"]
    br2, acct2, _ = broker()
    br2.limit("BTC", 1, 90.0, 1.0, 0, expires=HOUR, stop=88.0)
    br2.on_bar("BTC", bar(HOUR, 100, 101, 99, 100), 0.0)
    assert not acct2["orders"] and not acct2["positions"]


def test_structure_bos_and_flip():
    # up-leg, pullback, break above the high (BOS up), then a close below the protected low (flip)
    closes = [10, 11, 12, 13, 14, 13, 12, 11.5, 12, 13, 14, 15, 16, 15, 14, 13, 12, 11, 10.5, 10]
    bars = [bar(i * HOUR, c, c + 0.2, c - 0.2, c) for i, c in enumerate(closes)]
    s = ind.swing_structure(bars, 2)
    ev = [(i, x["event"], x["trend"]) for i, x in enumerate(s) if x["event"]]
    assert any(e == "flip" and tr == 1 for _, e, tr in ev) or any(tr == 1 for _, _, tr in ev)
    up = next(i for i, e, tr in ev if tr == 1)
    assert abs(s[up]["protect"] - 11.3) < 1e-9                 # the pullback low that caused the break
    assert s[-1]["trend"] == -1                                # closed below the protected low


def test_no_lookahead_in_indicators():
    import random
    rnd = random.Random(1)
    px, bars = 100.0, []
    for i in range(400):
        px *= 1 + rnd.uniform(-0.02, 0.02)
        bars.append(bar(i * HOUR, px, px * 1.01, px * 0.99, px))
    full = ind.swing_structure(bars, 3)
    part = ind.swing_structure(bars[:250], 3)
    assert full[:250] == part
    assert ind.adx(bars, 14)[0][:250] == ind.adx(bars[:250], 14)[0]
    assert ind.internal_structure(bars, 2)[:250] == ind.internal_structure(bars[:250], 2)


def test_run_handles_mixed_candle_sizes_at_close():
    data = {"A": [bar(i * HOUR, 1, 1, 1, 1 + i) for i in range(10)],
            "B": [bar(i * 4 * HOUR, 1, 1, 1, 100 + i) for i in range(3)]}
    seen = []

    class Spy(engine.Strategy):
        name, every_h, coins = "spy", 1, ["A", "B"]

        def step(self, T, br, ctx):
            seen.append((T, dict(br.px)))
    acct = {"spy": engine.new_account(500)}
    engine.run([Spy()], acct, data, {}, 0, 8 * HOUR, [])
    T4 = dict(seen)[4 * HOUR]
    assert T4["A"] == 4 and T4["B"] == 100                     # B's first 4h candle closes at 4h
    assert dict(seen)[3 * HOUR].get("B") is None               # not before it has closed


def test_analyst_view_decays_and_fallback():
    items = [{"ts": 1000.0, "impact": "high", "effects": [{"asset": "BTC", "direction": 1.0, "confidence": 0.8}]}]
    s0, c0 = analyst.view(items, "BTC", 1000 * 1000)
    s8, _ = analyst.view(items, "BTC", (1000 + 8 * 3600) * 1000)
    assert s0 > 0 and 0 < s8 < s0 and analyst.view(items, "ETH", 1000 * 1000) == (0.0, 0.0)
    assert analyst.view(items, "BTC", 900 * 1000) == (0.0, 0.0)  # not published yet
    out, status = analyst.analyse([{"id": "x", "ts": 1.0, "title": "Fed signals rate cut as inflation cools",
                                    "source": "t"}], {}, client=None)
    assert status.startswith("keywords") and out and out[0]["by"] == "keywords"


class _FakeClient:
    """Stands in for anthropic.Anthropic() to test parsing without a network call."""

    class beta:
        class messages:
            @staticmethod
            def create(**kw):
                assert kw["output_config"]["format"]["type"] == "json_schema"
                txt = ('{"items":[{"i":0,"event":"rate cut","impact":"high","why":"easier policy",'
                       '"effects":[{"asset":"ALL_CRYPTO","direction":0.7,"confidence":0.6},'
                       '{"asset":"GOLD","direction":3,"confidence":0.5}]}]}')
                block = type("B", (), {"type": "text", "text": txt})
                return type("R", (), {"stop_reason": "end_turn", "content": [block]})


def test_analyst_parses_claude_output():
    out, status = analyst.analyse([{"id": "x", "ts": 1.0, "title": "Fed cuts rates", "source": "t"}], {},
                                  client=_FakeClient())
    assets = {e["asset"]: e for e in out[0]["effects"]}
    assert "BTC" in assets and "SUI" in assets and assets["xyz:GOLD"]["direction"] == 1.0   # clamped
    assert out[0]["by"] == "claude" and not status.startswith("keywords")


def test_watcher_copies_mirrors_exits_and_learns():
    from lab import watcher as W
    data = {"BTC": [bar(i * HOUR, 100, 101, 99, 100) for i in range(300)]}

    class Opener(engine.Strategy):
        name, every_h, coins = "src", 1, ["BTC"]

        def step(self, T, br, ctx):
            if T == 250 * HOUR:
                br.market("BTC", 1, 2.0, T, stop=90.0, setup="test")
            if T == 260 * HOUR:
                br.close("BTC", T, "source exit")
    log = []
    accts = {"src": engine.new_account(500.0), "watcher": engine.new_account(500.0)}
    wt = W.Watcher(["src"], {"src": accts["src"]}, log, ["BTC"])
    wt.prepare(data)
    engine.run([Opener(), wt], accts, data, {}, 240 * HOUR, 270 * HOUR, log, context_fn=wt.context)
    copies = [t for t in log if t["strategy"] == "watcher"]
    assert len(copies) == 1 and copies[0]["reason"] == "src closed its trade"
    assert abs(copies[0]["qty"] / (2.0 * W.BASE_SHARE) - 1) < 0.01    # 25% share, neutral while learning
    src = [t for t in log if t["strategy"] == "src"][0]
    assert src["ctx"]["side"] == "long" and "session" in src["ctx"]
    # learning: a bot with many losing trades in one condition gets a smaller multiplier there
    wt.log = [{"strategy": "src", "closed": 0, "pnl": 5.0, "ctx": {"regime": "trend"}}] * 60 + \
             [{"strategy": "src", "closed": 0, "pnl": -5.0, "ctx": {"regime": "range"}}] * 60
    wt.learn(1)
    _, m_trend, _ = wt.expect("src", {"regime": "trend"})
    _, m_range, _ = wt.expect("src", {"regime": "range"})
    assert W.MULT_LO <= m_range < 1.0 < m_trend <= W.MULT_HI


def test_rel_value_skips_extreme_movers():
    from lab import strategies as S
    D = 24 * HOUR
    moves = {f"C{i}": 1 + (i - 6) * 0.01 for i in range(12)}        # gentle spread of 7-day moves
    moves["CRASH"] = 0.6                                             # one coin fell 40%: extreme
    data = {}
    for c, m in moves.items():
        data[c] = [bar(k * D, 1, 1, 1, 1.0 if k < 10 else m) for k in range(14)]
    data["BTC"] = [bar(k * D, 1, 1, 1, 1.0) for k in range(14)]
    T = 14 * D
    for max_z, expect_short in ((None, True), (2.0, False)):
        rv = S.RelativeValue(list(data), mode="momentum", lookback_d=7, n_side=3, hold_band=1, max_z=max_z)
        rv.prepare(data)
        br, acct, _ = broker()
        br.px.update({c: v[-1]["c"] for c, v in data.items()})
        rv.step(T, br, {})
        assert ("CRASH" in acct["positions"]) == expect_short
        assert sum(p["side"] < 0 for p in acct["positions"].values()) == 3   # still a full short book


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
