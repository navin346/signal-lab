"""On-chain scout (paper): looks for real breakouts among DEX pools on the busiest chains.

On-chain coins cannot be backtested honestly (dead and scam pools vanish from free data), so this bot
collects the evidence forward. Every 15 minutes it reads the trending and highest-volume pools on each
chain from GeckoTerminal (free, no key), drops the fakes, and:
  * logs every pool that passes the filters the first time it sees it, then records its price 1, 6, 24,
    72 and 168 hours later: after a few weeks this shows which features (momentum, age, size, chain)
    actually precede further gains;
  * paper-buys the ones already running (+20% to +300% on the day, still rising this hour) with 5% of
    the account each, a 30% stop, a 35% trailing stop once a trade is up 50%, and a 7-day limit.
Costs: 1% per side (DEX fee, slippage, priority fee). A pool whose liquidity is pulled is closed with a
further 50% haircut. Paper only: it cannot tell whether a token can really be sold."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

GT = "https://api.geckoterminal.com/api/v2/"
NETWORKS = ["solana", "robinhood", "base", "bsc"]
MAJORS = {"SOL", "WSOL", "ETH", "WETH", "BNB", "WBNB", "BTC", "WBTC", "CBBTC", "USDC", "USDT", "USDG", "USD1", "DAI",
          "USDE", "PYUSD", "FDUSD", "USDS", "JITOSOL", "MSOL", "BNSOL", "JUPSOL", "CBETH", "WSTETH", "WEETH", "BTCB"}
HOUR = 3_600_000
MIN_LIQ, MIN_VOL, MAX_TURN, MIN_AGE_H = 150_000, 500_000, 40, 24      # dollars, dollars, volume / liquidity, hours
MIN_BUYERS, MIN_SELLERS = 300, 150                                     # distinct wallets in 24h (sellers: it can be sold)
FDV_MIN, FDV_MAX = 1e6, 1e9
BUY_MIN, BUY_MAX = 20.0, 300.0                                         # 24h change, %
SIZE, MAX_OPEN, COST = 0.05, 10, 0.01
STOP, TRAIL_FROM, TRAIL, MAX_DAYS = 0.30, 0.50, 0.35, 7
MARKS_H = (1, 6, 24, 72, 168)
START = 500.0
_last = [0.0]


def _get(path, params=None):
    for i in range(4):
        wait = 2.2 - (time.time() - _last[0])                          # free tier: 30 calls a minute
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = requests.get(GT + path, params=params, timeout=30, headers={"Accept": "application/json"})
            if r.status_code == 429:
                time.sleep(20 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == 3:
                raise
            time.sleep(3 * (i + 1))
    raise RuntimeError("GeckoTerminal rate limit")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse(p, network):
    a = p["attributes"]
    tx = (a.get("transactions") or {}).get("h24") or {}
    ch, vol = a.get("price_change_percentage") or {}, a.get("volume_usd") or {}
    try:
        created = int(datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp() * 1000)
    except (KeyError, ValueError, AttributeError):
        created = None
    name = a.get("name") or ""
    return {"id": f"{network}_{a['address']}", "net": network, "addr": a["address"], "name": name,
            "sym": name.split(" / ")[0].strip(), "px": _f(a.get("base_token_price_usd")), "fdv": _f(a.get("fdv_usd")),
            "liq": _f(a.get("reserve_in_usd")), "vol24": _f(vol.get("h24")), "vol1": _f(vol.get("h1")),
            "h1": _f(ch.get("h1")), "h6": _f(ch.get("h6")), "h24": _f(ch.get("h24")), "buyers": tx.get("buyers") or 0,
            "sellers": tx.get("sellers") or 0, "buys": tx.get("buys") or 0, "sells": tx.get("sells") or 0,
            "created": created}


def qualifies(p, now):
    """Real, liquid, tradable-looking and not a major: the filters against fake volume and instant rugs."""
    return (p["px"] > 0 and p["sym"].upper() not in MAJORS and p["liq"] >= MIN_LIQ and p["vol24"] >= MIN_VOL
            and p["vol24"] / p["liq"] <= MAX_TURN and p["created"] is not None and now - p["created"] >= MIN_AGE_H * HOUR
            and p["buyers"] >= MIN_BUYERS and p["sellers"] >= MIN_SELLERS and p["sells"] >= 0.25 * p["buys"]
            and FDV_MIN <= p["fdv"] <= FDV_MAX)


def scan(errors):
    out = {}
    for net in NETWORKS:
        for path, params in ((f"networks/{net}/trending_pools", {"page": 1}),
                             (f"networks/{net}/pools", {"page": 1, "sort": "h24_volume_usd_desc"})):
            try:
                for p in _get(path, params)["data"]:
                    q = parse(p, net)
                    out[q["id"]] = q
            except Exception as e:
                errors[f"{net}:{path.rsplit('/', 1)[-1]}"] = str(e)[:80]
    return out


def refresh(ids, errors):
    """Current price and liquidity of specific pools: {id: (price, liquidity)}."""
    out, by = {}, {}
    for i in ids:
        net, addr = i.split("_", 1)
        by.setdefault(net, []).append(addr)
    for net, addrs in by.items():
        for k in range(0, len(addrs), 30):
            try:
                for p in _get(f"networks/{net}/pools/multi/{','.join(addrs[k:k + 30])}")["data"]:
                    a = p["attributes"]
                    out[f"{net}_{a['address']}"] = (_f(a.get("base_token_price_usd")), _f(a.get("reserve_in_usd")))
            except Exception as e:
                errors[f"{net}:refresh"] = str(e)[:80]
    return out


class Scout:
    def __init__(self, state=None):
        self.s = state or {"equity": START, "started": None, "watch": {}, "open": {}, "done": [], "obs": [], "cycles": 0}

    def step(self, now, pools, prices):
        """pools: this cycle's scan {id: pool}; prices: {id: (price, liquidity)} for everything tracked."""
        s = self.s
        s["started"] = s["started"] or now
        s["heartbeat"] = now
        s["cycles"] += 1
        for i, w in list(s["watch"].items()):                         # price marks for everything logged
            px = prices.get(i, (None, None))[0]
            if px:
                w["hi"] = max(w["hi"], px / w["px"] - 1)
                for h in MARKS_H:
                    if str(h) not in w["marks"] and now - w["t"] >= h * HOUR:
                        w["marks"][str(h)] = round(px / w["px"] - 1, 4)
            if now - w["t"] > (MARKS_H[-1] + 6) * HOUR:
                s["obs"] = (s["obs"] + [s["watch"].pop(i)])[-4000:]
        for i, g in list(s["open"].items()):                          # manage the paper trades
            px, liq = prices.get(i, (None, None))
            if not px:
                continue
            g["peak"] = max(g["peak"], px)
            rug = liq is not None and liq < 0.3 * g["liq"]
            why = ("liquidity pulled" if rug else "stop" if px <= g["e"] * (1 - STOP) else
                   "trailing stop" if g["peak"] >= g["e"] * (1 + TRAIL_FROM) and px <= g["peak"] * (1 - TRAIL) else
                   "7 days" if now - g["t"] >= MAX_DAYS * 24 * HOUR else None)
            g["now"] = round(px / g["e"] - 1, 4)
            if why:
                x = px * (0.5 if rug else 1.0)
                g.update(x=x, x_t=now, why=why, pnl=round(x * (1 - COST) / (g["e"] * (1 + COST)) - 1, 4))
                s["equity"] += g["size"] * g["pnl"]
                s["done"] = (s["done"] + [s["open"].pop(i)])[-2000:]
        for i, p in pools.items():                                    # new qualifiers
            if i in s["watch"] or i in s["open"] or not qualifies(p, now):
                continue
            if any(o["id"] == i and now - o["t"] < 14 * 24 * HOUR for o in s["obs"][-600:]):
                continue                                              # seen in the last two weeks
            buy = BUY_MIN <= p["h24"] <= BUY_MAX and p["h1"] >= 0
            s["watch"][i] = {"id": i, "t": now, "net": p["net"], "sym": p["sym"], "px": p["px"], "fdv": round(p["fdv"]),
                             "liq": round(p["liq"]), "vol24": round(p["vol24"]), "h1": p["h1"], "h6": p["h6"],
                             "h24": p["h24"], "buyers": p["buyers"], "age_d": round((now - p["created"]) / HOUR / 24, 1),
                             "buy": buy, "marks": {}, "hi": 0.0}
            if buy and len(s["open"]) < MAX_OPEN:
                s["open"][i] = {"id": i, "t": now, "net": p["net"], "sym": p["sym"], "e": p["px"], "peak": p["px"],
                                "liq": p["liq"], "size": round(SIZE * s["equity"], 2), "h24": p["h24"], "now": 0.0}

    def stats(self):
        s = self.s
        rows = s["obs"] + list(s["watch"].values())

        def after(sel, h):
            v = [w["marks"][str(h)] for w in rows if sel(w) and str(h) in w["marks"]]
            return {"n": len(v), "avg_pct": round(sum(v) / len(v) * 100, 1), "up_pct": round(sum(x > 0 for x in v) / len(v) * 100),
                    "x2_pct": round(sum(x >= 1 for x in v) / len(v) * 100)} if v else None
        done = s["done"]
        return {"logged": len(rows), "open": len(s["open"]), "closed": len(done), "equity": round(s["equity"], 2),
                "open_value": round(sum(g["size"] * g["now"] for g in s["open"].values()), 2),
                "win_pct": round(sum(g["pnl"] > 0 for g in done) / len(done) * 100) if done else None,
                "avg_pnl_pct": round(sum(g["pnl"] for g in done) / len(done) * 100, 1) if done else None,
                "all": {str(h): after(lambda w: True, h) for h in (24, 72, 168)},
                "bought": {str(h): after(lambda w: w["buy"], h) for h in (24, 72, 168)},
                "by_net": {n: after(lambda w, n=n: w["net"] == n, 24) for n in NETWORKS}}


def cycle(path):
    """One scan-and-update pass; state is read from and written to `path`."""
    state = None
    if Path(path).exists():
        try:
            state = json.loads(Path(path).read_text())
            state.pop("stats", None)
            state.pop("errors", None)
        except json.JSONDecodeError:
            state = None
    bot, errors = Scout(state), {}
    now = int(time.time() * 1000)
    pools = scan(errors)
    prices = {i: (p["px"], p["liq"]) for i, p in pools.items()}
    need = [i for i in bot.s["open"] if i not in prices]
    if bot.s["cycles"] % 4 == 0:                                      # hourly: everything still being watched
        need += [i for i in bot.s["watch"] if i not in prices]
    if need:
        prices.update(refresh(need, errors))
    bot.step(now, pools, prices)
    Path(path).write_text(json.dumps(dict(bot.s, stats=bot.stats(), errors=errors), separators=(",", ":")))
    return bot


if __name__ == "__main__":
    import sys
    b = cycle(sys.argv[1] if len(sys.argv) > 1 else "scout.json")
    print(json.dumps(b.stats())[:600])
