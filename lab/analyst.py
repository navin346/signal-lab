"""AI news analyst: reads fresh headlines and says which tradable markets each one should move,
which way, how confidently, and why.

Uses Claude through the official Anthropic SDK when the ANTHROPIC_API_KEY secret is set
(model from NEWS_MODEL, default claude-opus-5-5). Without a key, or if a call fails, it falls
back to the keyword themes in lab/news.py, so the bot never stops because of the analyst.

Each analysed headline becomes an item {ts, title, source, impact, event, why, effects:
[{asset, direction -1..1, confidence 0..1}], by: "claude"|"keywords"}. Strategies read a
time-decayed sum of those items per coin via view(items, coin, T)."""
import json
import math
import os
from datetime import datetime, timezone

from lab import news, universe

MODEL = os.environ.get("NEWS_MODEL") or "claude-opus-5-5"
MAX_PER_CALL = 80
IMPACT_W = {"low": 0.4, "medium": 0.7, "high": 1.0}
HALF_LIFE_H = 8.0          # a headline's weight halves every 8 hours
WINDOW_H = 36
CRYPTO = [c for c in universe.DIRECTIONAL if not c.startswith("xyz:")]

SYSTEM = """You are the news desk of a paper-trading research bot. Each run you get fresh headlines
(with source and publish time) and a list of markets the bot trades. For every headline that could
plausibly move one of those markets over the next ~24 hours, say which markets, which way, and how
confident you are. Skip headlines with no plausible market impact.

Judge it like a professional macro/crypto trader:
- Headlines that only describe a move that already happened ("Bitcoin rises 3%", "gold hits record")
  carry little new information; give them low confidence. Surprises, policy decisions, flows,
  hacks, regulation, supply shocks, and scheduled data that beat or missed expectations matter more.
- direction is from -1 (strongly bearish) to +1 (strongly bullish). confidence is from 0 to 1: how
  sure you are the market moves that way because of this news, not how big the story is.
- impact is how large a move the news could cause if it plays out: low, medium or high.
- Use ALL_CRYPTO for news that moves crypto as a whole; name single coins only when the news is
  specific to them. GOLD, OIL and NASDAQ are trade.xyz perps on gold, WTI crude and the Nasdaq-100.
- Duplicates of the same story: analyse once, refer to the earliest index.
Be calibrated: most headlines deserve confidence below 0.5."""


def _asset_names():
    out = {universe.label(c).split(" ")[0]: c for c in universe.DIRECTIONAL}
    out["NASDAQ"] = "xyz:XYZ100"
    out["OIL"] = "xyz:CL"
    out["GOLD"] = "xyz:GOLD"
    return out


def _schema(names):
    effect = {"type": "object", "additionalProperties": False, "required": ["asset", "direction", "confidence"],
              "properties": {"asset": {"type": "string", "enum": sorted(names) + ["ALL_CRYPTO"]},
                             "direction": {"type": "number"}, "confidence": {"type": "number"}}}
    item = {"type": "object", "additionalProperties": False, "required": ["i", "event", "impact", "effects", "why"],
            "properties": {"i": {"type": "integer"}, "event": {"type": "string"},
                           "impact": {"type": "string", "enum": ["low", "medium", "high"]},
                           "effects": {"type": "array", "items": effect}, "why": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["items"],
            "properties": {"items": {"type": "array", "items": item}}}


def _clamp(x, lo, hi):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _expand(asset, names):
    return CRYPTO if asset == "ALL_CRYPTO" else ([names[asset]] if asset in names else [])


def _claude(batch, names, client):
    lines = [f"{i}. [{h['source']}, {h['ts_iso']}] {h['title']}" for i, h in enumerate(batch)]
    user = ("Markets: " + ", ".join(sorted(names)) + ", ALL_CRYPTO\n\nHeadlines:\n" + "\n".join(lines))
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=16000, system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema(names)}},
        messages=[{"role": "user", "content": user}])
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"analyst stop_reason={resp.stop_reason}")
    text = next(b.text for b in resp.content if b.type == "text")
    out = []
    for it in json.loads(text)["items"]:
        i = it.get("i")
        if not isinstance(i, int) or not 0 <= i < len(batch):
            continue
        effects = []
        for e in it.get("effects", []):
            for coin in _expand(e.get("asset"), names):
                effects.append({"asset": coin, "direction": round(_clamp(e.get("direction"), -1, 1), 2),
                                "confidence": round(_clamp(e.get("confidence"), 0, 1), 2)})
        if effects:
            h = batch[i]
            out.append({"id": h["id"], "ts": h["ts"], "title": h["title"], "source": h["source"],
                        "link": h.get("link", ""), "impact": it.get("impact", "low"), "event": it.get("event", "")[:80],
                        "why": it.get("why", "")[:240], "effects": effects, "by": "claude"})
    return out


def _keywords(batch, matrix):
    """Fallback: keyword themes with their fixed prior reactions (lab/news.py). The learned
    matrix is not used here: a few days of drifting prices make it flip signs on noise."""
    legacy = {"xyz:GOLD": "GOLD", "xyz:CL": "OIL", "xyz:XYZ100": "NASDAQ"}
    out = []
    for h in batch:
        themes = news.tag(h["title"])
        for th in themes:
            prior = news.THEMES[th]["prior"]
            effects = []
            for coin in universe.DIRECTIONAL:
                d = prior.get(legacy.get(coin, coin), prior.get("ETH", 0) if coin in CRYPTO else 0)
                if d:
                    effects.append({"asset": coin, "direction": float(d), "confidence": 0.25})
            if effects:
                out.append({"id": h["id"], "ts": h["ts"], "title": h["title"], "source": h["source"],
                            "link": h.get("link", ""), "impact": "low", "event": th,
                            "why": "keyword theme: " + th.replace("_", " "), "effects": effects, "by": "keywords"})
    return out


def analyse(headlines, matrix, client=None):
    """headlines: fresh, de-duplicated items from news.fetch_headlines(). Returns (items, status)."""
    if not headlines:
        return [], "no new headlines"
    names = _asset_names()
    batch = sorted(headlines, key=lambda h: -h["ts"])[:MAX_PER_CALL * 2]
    for h in batch:
        h["ts_iso"] = datetime.fromtimestamp(h["ts"], timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    if client is None and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic(max_retries=3, timeout=180)
        except Exception as e:                           # SDK missing: keywords instead
            return _keywords(batch, matrix), f"keywords (SDK unavailable: {str(e)[:80]})"
    if client is None:
        return _keywords(batch, matrix), "keywords (no ANTHROPIC_API_KEY secret)"
    out = []
    try:
        for k in range(0, len(batch), MAX_PER_CALL):
            out += _claude(batch[k:k + MAX_PER_CALL], names, client)
        return out, f"{MODEL} ({len(batch)} headlines)"
    except Exception as e:                               # never let the analyst stop the bot
        return _keywords(batch, matrix), f"keywords (analyst error: {type(e).__name__}: {str(e)[:100]})"


def view(items, coin, T_ms):
    """Net news view for coin at time T: (score, confidence), both from time-decayed items
    published in the last WINDOW_H hours before T."""
    T = T_ms / 1000
    raw = mass = 0.0
    seen = set()
    for it in sorted(items, key=lambda x: -x["ts"]):
        age_h = (T - it["ts"]) / 3600
        if age_h < 0 or age_h > WINDOW_H:
            continue
        if it.get("by") == "keywords":            # many headlines, one theme: count it once
            if it["event"] in seen:
                continue
            seen.add(it["event"])
        w = IMPACT_W.get(it["impact"], 0.4) * 0.5 ** (age_h / HALF_LIFE_H)
        for e in it["effects"]:
            if e["asset"] == coin:
                raw += e["direction"] * e["confidence"] * w
                mass += e["confidence"] * w
    return math.tanh(raw), math.tanh(mass)
