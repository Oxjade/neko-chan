"""Enhanced sentiment context for US stocks.

Free, no-key sources:
  - yfinance ticker news (headlines)
  - Google News RSS fallback when yfinance returns nothing
  - yfinance index movers for market-wide context (SPX / NASDAQ / DOW)

Output is a compact string injected into the LLM prompt as context.
"""

import html
import re
import time

import requests

from sentiment import stock_news

_CACHE: dict[str, tuple[float, str]] = {}
_NEWS_TTL = 900  # 15 min
_MOVERS_TTL = 600  # 10 min


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


def google_news_fallback(symbol: str, n: int = 3) -> str:
    """Headlines for a US stock via Google News RSS (free, no key)."""
    def fetch():
        r = requests.get(
            "https://news.google.com/rss/search",
            params={"q": f"{symbol.upper()} stock", "hl": "en-US", "gl": "US", "ceid": "US:en"},
            timeout=15,
        )
        titles = re.findall(r"<title>(.*?)</title>", r.text)
        out = []
        for t in titles[1 : n + 1]:
            t = html.unescape(t)
            m = re.match(r"Google News\s*(?:[:|]\s*)?", t)
            if m:
                t = t[m.end():]
            out.append(t[:100])
        return " | ".join(out) if out else "no recent news"

    return _cached(f"gnews:{symbol.upper()}", _NEWS_TTL, fetch)


def stock_movers_context() -> str:
    """S&P 500 / NASDAQ / Dow prices with 1d change for market context."""
    def fetch():
        import yfinance as yf

        parts = []
        for name, ticker in (("SPX", "^GSPC"), ("NASDAQ", "^IXIC"), ("DOW", "^DJI")):
            info = yf.Ticker(ticker).fast_info
            price = float(info.last_price)
            prev = float(info.previous_close)
            pct = (price / prev - 1.0) * 100.0 if prev else 0.0
            parts.append(f"{name} {price:,.1f} {pct:+.1f}%")
        return " | ".join(parts)

    return _cached("movers", _MOVERS_TTL, fetch)


def enhanced_stock_sentiment(symbol: str, n: int = 3) -> str:
    """Combined stock sentiment: headlines + market-wide movers context."""
    sym = symbol.upper()
    news = stock_news(sym, n)
    if news == "no recent news" or news.startswith("unavailable"):
        news = google_news_fallback(sym, n)
    return f"STOCK[{sym}]: news: {news} | market: {stock_movers_context()}"


if __name__ == "__main__":
    print(enhanced_stock_sentiment("AAPL"))