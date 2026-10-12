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
                                  "oi": float(c["openInterest"]) * float(c["markPx"])}
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
