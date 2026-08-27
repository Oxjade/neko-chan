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


def stock_news(symbol: str, n: int = 3) -> str:
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


def forex_news(pair: str, n: int = 3) -> str:
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


if __name__ == "__main__":
    print(sentiment_block("crypto", "BTC"))
    print(sentiment_block("crypto", "ETH"))
    print(sentiment_block("us-stock", "AAPL"))
    print(sentiment_block("forex", "EURUSD"))