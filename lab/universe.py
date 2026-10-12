"""Which markets each bot trades.

Hyperliquid has no market-cap data, so the relative-value universe = the 30 largest coins by
market cap (CoinPaprika, free, no key; CoinGecko as backup) that have a Hyperliquid perp.
Stablecoins, wrapped/staked copies and gold tokens are skipped. Refreshed once a day; if
both sources fail, the last good list (or the list below, taken 2026-10-12) is used."""
import requests

# directional bots: the biggest liquid crypto perps + gold, oil and the Nasdaq-100 on trade.xyz
DIRECTIONAL = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE", "ZEC", "LINK", "ADA", "AVAX", "SUI",
               "xyz:GOLD", "xyz:CL", "xyz:XYZ100"]
LABELS = {"xyz:GOLD": "GOLD", "xyz:CL": "OIL (WTI)", "xyz:XYZ100": "NASDAQ-100", "kSHIB": "SHIB (x1000)",
          "kPEPE": "PEPE (x1000)", "kBONK": "BONK (x1000)"}

FALLBACK_TOP30 = ["BTC", "ETH", "BNB", "XRP", "SOL", "TRX", "ZEC", "HYPE", "DOGE", "LINK", "XMR", "ADA", "NEAR",
                  "XLM", "BCH", "CC", "LTC", "UNI", "AVAX", "SUI", "HBAR", "GRAM", "kSHIB", "ENA", "TAO", "PUMP",
                  "AAVE", "ONDO", "WLD", "ICP"]
SKIP = {"USDT", "USDC", "USDS", "DAI", "USDE", "USD1", "PYUSD", "USDG", "FDUSD", "TUSD", "USDF", "USDD", "BFUSD",
        "GHO", "USD0", "SUSDE", "SUSDS", "PAXG", "XAUT", "STETH", "WSTETH", "WBTC", "CBBTC", "WETH", "WEETH",
        "RETH", "METH", "JITOSOL", "BNSOL", "LBTC", "KHYPE", "WBNB", "RSETH", "JUPSOL", "MSOL", "LSETH"}
UA = {"User-Agent": "Mozilla/5.0 (signal-lab paper bot)"}


def label(coin):
    return LABELS.get(coin, coin)


def _paprika():
    j = requests.get("https://api.coinpaprika.com/v1/tickers?limit=200", headers=UA, timeout=30).json()
    return [(x["symbol"].upper(), x["quotes"]["USD"]["market_cap"]) for x in sorted(j, key=lambda x: x["rank"] or 1e9)]


def _gecko():
    j = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                     params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 200}, headers=UA,
                     timeout=30).json()
    return [(x["symbol"].upper(), x["market_cap"]) for x in j]


def top30(hl_names, n=30):
    """Returns (list, source). hl_names = set of Hyperliquid perp names."""
    for src, fn in (("CoinPaprika", _paprika), ("CoinGecko", _gecko)):
        try:
            out, seen = [], set()
            for sym, _ in fn():
                if sym in SKIP or sym in seen:
                    continue
                seen.add(sym)
                hl = sym if sym in hl_names else ("k" + sym if "k" + sym in hl_names else None)
                if hl == "GRAM" or (sym == "TON" and "GRAM" in hl_names):
                    hl = "GRAM"
                if hl and hl not in out:
                    out.append(hl)
                if len(out) == n:
                    return out, src
        except Exception:                      # a dead source must never stop the bot
            continue
    return None, None
