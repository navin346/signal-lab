"""Hyperliquid public market data (no keys)."""
import time

import requests

HL = "https://api.hyperliquid.xyz/info"
HOUR = 3_600_000
_MS = {"1h": HOUR, "4h": 4 * HOUR, "1d": 24 * HOUR}


def _post(payload, tries=6):
    for i in range(tries):
        try:
            r = requests.post(HL, json=payload, timeout=30)
            if r.status_code == 429:                       # rate limited: back off and retry
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    raise RuntimeError("Hyperliquid rate limit")


def contexts(coins=None):
    """Mark price, hourly funding and open interest for every perp (core + trade.xyz dex)."""
    out = {}
    for dex in (None, "xyz"):
        payload = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        meta, ctxs = _post(payload)
        for u, c in zip(meta["universe"], ctxs):
            if u.get("isDelisted") or c.get("markPx") is None:
                continue
            if coins is None or u["name"] in coins:
                out[u["name"]] = {"mark": float(c["markPx"]), "funding": float(c["funding"]),
                                  "oi": float(c["openInterest"]) * float(c["markPx"]),
                                  "vol": float(c.get("dayNtlVlm") or 0)}
    return out


def candles(coin, n, interval="1h", pause=0.5):
    """The last n closed candles: list of dicts t,o,h,l,c (t = open time, ms)."""
    ms = _MS[interval]
    end = int(time.time() * 1000) // ms * ms                # drop the still-open candle
    j = _post({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                                 "startTime": end - n * ms, "endTime": end - 1}})
    time.sleep(pause)                                        # stay well under the API weight limit
    return [{"t": x["t"], "o": float(x["o"]), "h": float(x["h"]), "l": float(x["l"]), "c": float(x["c"])}
            for x in j if x["t"] + ms <= end]


def funding_history(coin, hours):
    end = int(time.time() * 1000)
    j = _post({"type": "fundingHistory", "coin": coin, "startTime": end - hours * 3_600_000})
    return [(x["time"], float(x["fundingRate"])) for x in j]


# ---------------------------------------------------------------- Aster (Binance-style public API, no keys)
AST = "https://fapi.asterdex.com/fapi/v1/"


def _aget(path, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(AST + path, params=params, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    raise RuntimeError("Aster rate limit")


def aster_markets():
    """{symbol: {mark, rate (per interval), interval_h, vol}} for every USDT perp on Aster."""
    prem = _aget("premiumIndex")
    vol = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in _aget("ticker/24hr")}
    try:
        iv = {f["symbol"]: float(f.get("fundingIntervalHours") or 8) for f in _aget("fundingInfo")}
    except Exception:
        iv = {}
    out = {}
    for p in prem:
        s = p["symbol"]
        if not s.endswith("USDT"):
            continue
        try:
            out[s] = {"mark": float(p["markPrice"]), "rate": float(p["lastFundingRate"]), "interval_h": iv.get(s, 8.0),
                      "vol": vol.get(s, 0.0)}
        except (TypeError, ValueError):
            continue
    return out


def aster_candles(symbol, n, pause=0.1):
    j = _aget("klines", {"symbol": symbol, "interval": "1h", "limit": min(n + 1, 1500)})
    time.sleep(pause)
    now = int(time.time() * 1000)
    return [{"t": x[0], "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])}
            for x in j if x[0] + HOUR <= now]


def aster_funding(symbol, hours, pause=0.1):
    """Settled funding payments [(time_ms, rate per interval)] over the last `hours`."""
    start = int(time.time() * 1000) - hours * HOUR
    j = _aget("fundingRate", {"symbol": symbol, "startTime": start, "limit": 1000})
    time.sleep(pause)
    return [(int(x["fundingTime"]), float(x["fundingRate"])) for x in j]
