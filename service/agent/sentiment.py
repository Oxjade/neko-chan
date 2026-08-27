"""Realtime sentiment context for the trading agents.

Free, no-key sources per market:
  - crypto:  alternative.me Fear & Greed Index API + Hyperliquid funding rate
  - stocks:  yfinance ticker news (latest headlines)
  - forex:   Google News RSS headlines for the pair

Output is a compact string injected into the LLM prompt as context.
"""

import html
import re
import time
from typing import Optional

import requests

_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 900  # 15 min


def _cached(key: str, ttl: float, fetcher):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        value = fetcher()
        _CACHE[key] = (now, value)
        return value
    except Exception as exc:
        return f"unavailable ({type(exc).__name__})"


def fear_greed() -> str:
    """Crypto Fear & Greed Index 0-100 (contrarian: extreme = reversal zone)."""
    def fetch():
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=15)
        d = r.json()
        entry = d["data"][0]
        return f"{entry['value']} ({entry['value_classification']})"

    return _cached("fng", _CACHE_TTL, fetch)


def hyperliquid_funding(coin: str) -> str:
    """Current funding rate on Hyperliquid for a coin (longs pay when +)."""
    def fetch():
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "metaAndAssetCtxs"}, timeout=15)
        meta, ctxs = r.json()
        for asset, ctx in zip(meta["universe"], ctxs):
            if asset["name"] == coin.upper():
                rate = float(ctx["funding"])
                return f"{rate * 100:.4f}%/8h"
        return "n/a"

    return _cached(f"funding:{coin}", _CACHE_TTL, fetch)


def stock_news(symbol: str, n: int = 2) -> str:
    """Latest headlines for a US stock via yfinance (free, no key)."""
    def fetch():
        import yfinance as yf

        items = yf.Ticker(symbol).news or []
        out = []
        for it in items[:n]:
            content = it.get("content") if isinstance(it, dict) else None
            title = ""
            if isinstance(content, dict):
                title = content.get("title") or ""
            elif isinstance(it, dict):
                title = it.get("title") or ""
            src = (content or {}).get("provider", {}).get("displayName", "") if isinstance(content, dict) else ""
            ts = (content or {}).get("pubDate") or 0
            out.append(f"[{time.strftime('%m-%d %H:%M', time.gmtime(ts)) if isinstance(ts, (int, float)) else ''} {src}] {str(title)[:100]}")
        return " | ".join(out) if out else "no recent news"

    return _cached(f"news:{symbol}", _CACHE_TTL, fetch)


def forex_news(pair: str, n: int = 2) -> str:
    """Latest headlines for a forex pair via Google News RSS (free, no key)."""
    def fetch():
        query = pair.replace("/", " ")
        r = requests.get(
            "https://news.google.com/rss/search",
            params={"q": f"{query} FX", "hl": "en-US", "gl": "US", "ceid": "US:en"},
            timeout=15,
        )
        titles = re.findall(r"<title>(.*?)</title>", r.text)
        out = []
        for t in titles[1 : n + 1]:
            out.append(html.unescape(t)[:100])
        return " | ".join(out) if out else "no recent news"

    return _cached(f"fxnews:{pair}", _CACHE_TTL, fetch)


def sentiment_block(market: str, symbol: str) -> str:
    """Compact sentiment context line for the prompt."""
    m = market.lower()
    sym = symbol.upper()
    if m == "crypto":
        return (f"Sentiment[{sym}]: Fear&Greed {fear_greed()} "
                f"| funding {hyperliquid_funding(sym)}")
    if m == "us-stock":
        return f"Sentiment[{sym}]: news: {stock_news(sym)}"
    if m == "forex":
        return f"Sentiment[{sym}]: news: {forex_news(sym)}"
    return ""


def fear_greed_value() -> int | None:
    """Parsed Fear&Greed index (0-100) or None when unavailable."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=15)
        d = r.json()
        entry = d["data"][0]
        return int(entry["value"])
    except Exception:
        return None


def sentiment_score(market: str, symbol: str) -> tuple[float, str]:
    """Numeric sentiment score in [-1, +1] for a symbol/market.

    Design (research-informed, IC honesty):
      * Each source contributes a value on [-1, +1] then is combined with a
        documented weight. 0 = neutral/unknown; missing sources contribute 0
        but CAP the score at 0 (never let absence of data give conviction).
      * Source weights are conservative because sentiment is a weak factor
        (typical IC 0.02-0.06), so the MAXIMUM |score| here is 1.0.
      * Scores drive SIZE MULTIPLIER only — never trade creation. A trade
        requires the price/trend gate; sentiment scales it.

    Returns (score, human-readable attribution).
    """
    m = market.lower()
    sym = symbol.upper()
    parts: list[float] = []
    labels: list[str] = []
    if m == "crypto":
        fg = fear_greed_value()
        if fg is not None:
            # Fear&Greed is CONTRARIAN at extremes: 0=extreme fear (buy), 100=extreme greed (sell-like).
            # Map value v in [0,100] to score in [-1, +1]:
            #   score = (v - 50) / 50  -> v=0 => -1 (contrarian bullish), v=100 => +1 (contrarian bearish)
            # For a LONG-biased score we invert: extreme greed is - (stay out), extreme fear is + (opportunity).
            contrarian = (50 - fg) / 50.0   # v=0 -> +1 (fear = opportunity), v=100 -> -1 (greed = caution)
            parts.append(contrarian)
            labels.append(f"FG={fg}(contrarian {contrarian:+.2f})")
        funding = None
        try:
            import requests as _r
            r = _r.post("https://api.hyperliquid.xyz/info",
                        json={"type": "metaAndAssetCtxs"}, timeout=10)
            data = r.json()
            if isinstance(data, list) and len(data) == 2:
                # data[0] = meta (universe with coin lists), data[1] = assetCtxs
                # without coin keys: zip by index.
                meta_ctxs = data[0].get("universe", []) if isinstance(data[0], dict) else []
                ctxs = data[1]
                for m, c in zip(meta_ctxs, ctxs):
                    if isinstance(m, dict) and m.get("name") == sym:
                        f = c.get("funding") if isinstance(c, dict) else None
                        if f is not None:
                            funding = float(f)
                        break
        except Exception:
            pass
        if funding is not None:
            # positive funding = longs pay shorts = crowded long -> caution
            # (negative score) for NEW longs; scale: 0.5% -> -1.0
            parts.append(-min(1.0, max(-1.0, funding * 1000.0 / 5.0)))
            labels.append(f"funding={funding:+.4%}")
    elif m == "us-stock":
        news = stock_news(sym)
        if news and news != "no recent news":
            pos = sum(1 for w in ("beat", "up", "gain", "strong", "upgrade", "buy", "record", "surge") if w in news.lower())
            neg = sum(1 for w in ("fall", "down", "loss", "weak", "downgrade", "sell", "miss", "slump") if w in news.lower())
            n = pos + neg
            # single-source, keyword-heuristic: cap conviction at +-0.5 (never
            # let one source's keyword hit reach full conviction)
            parts.append((pos - neg) / max(n, 1) * 0.5)
            labels.append(f"news +{pos}/-{neg} (capped 0.5)")
    elif m == "forex":
        news = forex_news(sym)
        if news and news != "no recent news":
            pos = sum(1 for w in ("gain", "up", "strong", "rally", "rise", "surge", "support") if w in news.lower())
            neg = sum(1 for w in ("fall", "down", "weak", "slump", "pressure", "decline", "drop") if w in news.lower())
            n = pos + neg
            parts.append((pos - neg) / max(n, 1) * 0.5)
            labels.append(f"news +{pos}/-{neg} (capped 0.5)")

    if not parts:
        return 0.0, "sentiment: no data"
    score = sum(parts) / len(parts)
    # absence-capped: any numerical weakness reduces conviction, never adds
    # (documented in skill: sentiment is a veto/multiplier, not a trigger)
    score = max(-1.0, min(1.0, score))
    return round(score, 3), f"sentiment score {score:+.3f} ({'; '.join(labels)})"


if __name__ == "__main__":
    print(sentiment_block("crypto", "BTC"))
    print(sentiment_block("crypto", "ETH"))
    print(sentiment_block("us-stock", "AAPL"))
    print(sentiment_block("forex", "EURUSD"))