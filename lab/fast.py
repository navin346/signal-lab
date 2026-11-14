"""Fast lane (paper): the squeeze fade, traded within seconds of each hour on Hyperliquid.

Backtest (BACKTEST.md, Nov 14): after an hour in which a coin rose sharply (top 20% of hours, in units
of its own volatility) while open interest fell (bottom 20%), the price gave back about 35 bps over the
next five minutes. Selling one minute after the hour made +16 / +18 bps a trade in two separate years
(20 bps assumed costs); four minutes after, nothing. An hourly job cannot test that, so this process
stays on, polls Hyperliquid every few seconds and records what a real order would have got: it sells at
the impact bid, buys back at the impact ask an hour later, and pays taker fees and funding. Every
signal also logs the bid 1, 2 and 5 minutes later and the ask after 5, 15 and 30 minutes, to show how
fast the entry must be and how long to hold. Paper only: no keys, no orders.

  python -m lab.fast --minutes 290 --state fast.json"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lab import market, scout  # noqa: E402

HOUR = 3_600_000
Z_MIN, DOI_MAX = 0.70, -0.905          # the 80th / 20th percentiles of Nov 2024 - Nov 2025, all coins pooled
MIN_VOL = 1e6                          # 24h dollar volume on Hyperliquid
HOLD_MIN, STOP = 60, 0.08              # buy back at the next hour; disaster stop 8% above the entry
FEE = 0.00045                          # taker, per side
SIZE, MAX_OPEN = 1.0, 3                # each trade = 1x equity, at most three at once
SHADOW_S, PATH_MIN = (60, 120, 300), (5, 15, 30)
MARK_GRACE_S = 90                      # the hour's snapshot must arrive within this, else the hour is skipped
START = 500.0


class Fast:
    def __init__(self, state=None):
        self.s = state or {"equity": START, "started": None, "open": [], "done": [], "snap": {}, "curve": [],
                           "hours": 0, "skipped": 0}
        self.sd, self.last_close, self.hist_T = {}, {}, {}

    def set_history(self, coin, closes, close_t):
        """closes: closed hourly closes, oldest first; close_t: when the last one closed (an hour mark)."""
        r = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0][-72:]
        if len(r) >= 48:
            self.sd[coin] = math.sqrt(sum(x * x for x in r) / len(r))
            self.last_close[coin], self.hist_T[coin] = closes[-1], close_t

    def on_poll(self, now, ctx):
        """ctx: {coin: {mid, bid, ask, oi (dollars), vol, funding}}. Returns the signals closed on this poll."""
        s, closed = self.s, []
        s["started"] = s["started"] or now
        s["heartbeat"] = now
        for g in list(s["open"]):                                  # exits first, so a coin can re-enter the next hour
            x = ctx.get(g["coin"])
            if not x:
                continue
            el = now - g["t0"]
            for d in SHADOW_S:
                if str(d) not in g["shadow"] and el >= d * 1000:
                    g["shadow"][str(d)] = x["bid"]
            for m in PATH_MIN:
                if str(m) not in g["path"] and el >= m * 60_000:
                    g["path"][str(m)] = x["ask"]
            stop = x["ask"] >= g["e"] * (1 + STOP)
            if stop or now >= g["T"] + HOLD_MIN * 60_000:
                g["fund"] = (now // HOUR - g["t0"] // HOUR) * x["funding"]      # a short receives positive funding
                g.update(x=x["ask"], x_t=now, stop=stop)
                g["pnl"] = (g["e"] - g["x"]) / g["e"] - 2 * FEE + g["fund"]
                if g["taken"]:
                    s["equity"] += g["size"] * g["pnl"]
                    s["curve"] = (s["curve"] + [[now, round(s["equity"], 2)]])[-3000:]
                s["open"].remove(g)
                s["done"] = (s["done"] + [g])[-5000:]
                closed.append(g)
        T = now // HOUR * HOUR
        if s["snap"].get("T") == T:
            return closed
        if now - T > MARK_GRACE_S * 1000:                          # too late for this hour: no snapshot, no signals
            s["snap"] = {"T": T, "px": {}}
            s["skipped"] += 1
            return closed
        prev = s["snap"]
        if prev.get("T") == T - HOUR and prev.get("px"):
            cands = []
            for c, x in ctx.items():
                p = prev["px"].get(c)
                if (not p or p[1] <= 0 or x["vol"] < MIN_VOL or self.hist_T.get(c) != T - HOUR
                        or any(g["coin"] == c for g in s["open"])):
                    continue
                z = math.log(x["mid"] / self.last_close[c]) / self.sd[c]
                doi = (x["oi"] / p[1] - 1) * 100
                if z > Z_MIN and doi < DOI_MAX:
                    cands.append((doi, c, z))
            for doi, c, z in sorted(cands):                        # biggest open-interest drop first
                x = ctx[c]
                taken = sum(g["taken"] for g in s["open"]) < MAX_OPEN
                s["open"].append({"coin": c, "T": T, "t0": now, "delay_s": round((now - T) / 1000, 1), "z": round(z, 2),
                                  "doi": round(doi, 2), "spread_bps": round((x["ask"] / x["bid"] - 1) * 1e4, 1),
                                  "vol": round(x["vol"]), "e": x["bid"], "taken": taken,
                                  "size": round(SIZE * s["equity"], 2) if taken else 0.0, "shadow": {}, "path": {},
                                  "fund": 0.0})
            s["hours"] += 1
        else:
            s["skipped"] += 1
        s["snap"] = {"T": T, "t": now, "px": {c: [x["mid"], x["oi"]] for c, x in ctx.items()}}
        return closed

    def stats(self):
        """Per-signal results in bps, after fees and funding, for the dashboard."""
        done = self.s["done"]

        def agg(vals):
            vals = [v for v in vals if v is not None]
            if not vals:
                return None
            m = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) if len(vals) > 1 else 0.0
            return {"n": len(vals), "avg_bps": round(m * 1e4, 1), "win_pct": round(sum(v > 0 for v in vals) / len(vals) * 100, 1),
                    "t": round(m / sd * math.sqrt(len(vals)), 1) if sd else None}
        out = {"signals": len(done), "taken": sum(g["taken"] for g in done), "open": len(self.s["open"]),
               "equity": round(self.s["equity"], 2), "hours": self.s["hours"], "skipped": self.s["skipped"],
               "entry_now": agg([g["pnl"] for g in done]),
               "spread_bps": round(sum(g["spread_bps"] for g in done) / len(done), 1) if done else None,
               "delay_s": round(sum(g["delay_s"] for g in done) / len(done), 1) if done else None,
               "by_delay": {str(d): agg([(g["shadow"][str(d)] - g["x"]) / g["shadow"][str(d)] - 2 * FEE + g["fund"]
                                         if str(d) in g["shadow"] and not g["stop"] else None for g in done])
                            for d in SHADOW_S},
               "by_exit": {str(m): agg([(g["e"] - g["path"][str(m)]) / g["e"] - 2 * FEE if str(m) in g["path"] else None
                                        for g in done]) for m in PATH_MIN}}
        return out


def feed():
    meta, ctxs = market._post({"type": "metaAndAssetCtxs"})
    out = {}
    for u, c in zip(meta["universe"], ctxs):
        ip = c.get("impactPxs")
        if u.get("isDelisted") or not ip or not c.get("midPx"):
            continue
        mid = float(c["midPx"])
        out[u["name"]] = {"mid": mid, "bid": float(ip[0]), "ask": float(ip[1]), "oi": float(c["openInterest"]) * mid,
                          "vol": float(c.get("dayNtlVlm") or 0), "funding": float(c["funding"])}
    return out


def publish(*paths):
    """Force-push the state files as the only commit on the fast-data branch (read by the dashboard)."""
    def git(*a, **kw):
        return subprocess.run(["git", *a], capture_output=True, text=True, check=True, timeout=120, **kw).stdout.strip()
    try:
        tree = git("mktree", input="".join(f"100644 blob {git('hash-object', '-w', str(p))}\t{Path(p).name}\n"
                                           for p in paths if Path(p).exists()))
        commit = git("-c", "user.name=signal-lab-bot", "-c", "user.email=signal-lab-bot@users.noreply.github.com",
                     "commit-tree", tree, "-m", "fast")
        git("push", "-q", "--force", "origin", f"{commit}:refs/heads/fast-data")
    except Exception as e:                                        # a failed push must never stop the bot
        print("publish failed:", str(e)[:200], flush=True)


def save(bot, path):
    out = dict(bot.s, stats=bot.stats(), rule={"z_min": Z_MIN, "doi_max": DOI_MAX, "hold_min": HOLD_MIN,
                                                "size": SIZE, "max_open": MAX_OPEN, "min_vol": MIN_VOL})
    Path(path).write_text(json.dumps(out, separators=(",", ":")))


def run(minutes, path, scout_path=None):
    state = None
    if Path(path).exists():
        try:
            state = json.loads(Path(path).read_text())
            state.pop("stats", None)
            state.pop("rule", None)
        except json.JSONDecodeError:
            state = None
    bot = Fast(state)
    t_end, queue, refreshed, last_pub, last_scout = time.time() + minutes * 60, [], None, 0.0, 0.0
    out = [path] + ([scout_path] if scout_path else [])
    while True:
        try:
            ctx = feed()
        except Exception as e:
            print("feed failed:", str(e)[:120], flush=True)
            time.sleep(5)
            continue
        now = int(time.time() * 1000)
        T, into = now // HOUR * HOUR, now % HOUR
        closed = bot.on_poll(now, ctx)
        for g in closed:
            print(f"closed {g['coin']} {g['pnl'] * 1e4:+.0f} bps taken={g['taken']} stop={g['stop']}", flush=True)
        if refreshed != T and (refreshed is None or into >= 7 * 60_000) and not queue:   # volatility history, hourly
            queue, refreshed = [c for c, x in ctx.items() if x["vol"] >= MIN_VOL], T
        for c in queue[:2]:                                        # two coins a loop keeps us under the rate limit
            try:
                bars = market.candles(c, 76, "1h", pause=0.2)
                if bars:
                    bot.set_history(c, [b["c"] for b in bars], bars[-1]["t"] + HOUR)
            except Exception as e:
                print("history failed:", c, str(e)[:80], flush=True)
        queue = queue[2:]
        fresh = any(now - g["t0"] < 6 * 60_000 for g in bot.s["open"])
        if scout_path and time.time() - last_scout > 900 and 3 * 60_000 < into < 55 * 60_000 and not fresh and not queue:
            try:                                                   # the on-chain scout shares this always-on process
                scout.cycle(scout_path)
            except Exception as e:
                print("scout failed:", str(e)[:120], flush=True)
            last_scout, last_pub = time.time(), 0.0
        if closed or time.time() - last_pub > 600:
            save(bot, path)
            if os.environ.get("FAST_PUBLISH"):
                publish(*out)
            last_pub = time.time()
        if time.time() > t_end and 20 * 60_000 <= into < 45 * 60_000:     # hand over away from the hour mark
            save(bot, path)
            if os.environ.get("FAST_PUBLISH"):
                publish(*out)
            return
        near_mark = into > HOUR - 15_000 or into < 20_000
        time.sleep(2 if near_mark else 3 if (fresh or queue) else 10)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=290)
    ap.add_argument("--state", default="fast.json")
    ap.add_argument("--scout", default=None, help="state file of the on-chain scout (lab/scout.py); omit to skip it")
    a = ap.parse_args()
    run(a.minutes, a.state, a.scout)
