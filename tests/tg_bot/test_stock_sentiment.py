import os
import sys
import types
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "agent"))

from stock_sentiment import (
    _CACHE,
    enhanced_stock_sentiment,
    google_news_fallback,
    stock_movers_context,
)

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title>
<item><title>Apple stock rises after earnings beat &amp; record quarter - Reuters</title></item>
<item><title>Google News | Apple unveils new chip - TechCrunch</title></item>
<item><title>Analysts upgrade Apple stock - Bloomberg</title></item>
</channel></rss>"""


def _fake_yf(raise_on_ticker=False):
    def ticker(_t):
        if raise_on_ticker:
            raise RuntimeError("network down")
        return types.SimpleNamespace(
            fast_info=types.SimpleNamespace(last_price=5812.3, previous_close=5789.0)
        )

    return types.SimpleNamespace(Ticker=ticker)


def test_google_news_fallback_parses_rss_titles():
    _CACHE.clear()
    with patch("stock_sentiment.requests.get") as get:
        get.return_value.text = RSS_XML
        out = google_news_fallback("AAPL", n=3)
    assert get.call_args.args[0] == "https://news.google.com/rss/search"
    assert "Apple stock rises after earnings beat & record quarter" in out
    assert "Apple unveils new chip" in out
    assert "Google News" not in out


def test_movers_context_returns_spx_or_unavailable():
    _CACHE.clear()
    with patch.dict(sys.modules, {"yfinance": _fake_yf()}):
        out = stock_movers_context()
    assert "SPX 5,812.3 +0.4%" in out
    assert "NASDAQ" in out and "DOW" in out

    _CACHE.clear()
    with patch.dict(sys.modules, {"yfinance": _fake_yf(raise_on_ticker=True)}):
        out = stock_movers_context()
    assert out.startswith("unavailable")


def test_enhanced_stock_sentiment_combines_news_and_market():
    _CACHE.clear()
    with (
        patch("stock_sentiment.stock_news", return_value="AAPL rallies on AI demand - Reuters"),
        patch("stock_sentiment.google_news_fallback") as fallback,
        patch("stock_sentiment.stock_movers_context", return_value="SPX 5,812.3 +0.4%"),
    ):
        out = enhanced_stock_sentiment("aapl")
    fallback.assert_not_called()
    assert "STOCK[AAPL]:" in out
    assert "market:" in out
    assert "AAPL rallies on AI demand" in out


def test_enhanced_stock_sentiment_uses_google_fallback():
    _CACHE.clear()
    with (
        patch("stock_sentiment.stock_news", return_value="no recent news"),
        patch("stock_sentiment.google_news_fallback",
              return_value="Apple unveils new chip - TechCrunch") as fallback,
        patch("stock_sentiment.stock_movers_context", return_value="unavailable (Timeout)"),
    ):
        out = enhanced_stock_sentiment("AAPL")
    fallback.assert_called_once_with("AAPL", 3)
    assert "STOCK[AAPL]:" in out
    assert "market:" in out
    assert "Apple unveils new chip" in out


def test_caching_prevents_refetch_within_ttl():
    _CACHE.clear()
    with patch("stock_sentiment.requests.get") as get:
        get.return_value.text = RSS_XML
        first = google_news_fallback("AAPL", n=1)
        second = google_news_fallback("AAPL", n=1)
    assert first == second
    assert get.call_count == 1

    calls = []

    def ticker(t):
        calls.append(t)
        return types.SimpleNamespace(
            fast_info=types.SimpleNamespace(last_price=5812.3, previous_close=5789.0)
        )

    _CACHE.clear()
    with patch.dict(sys.modules, {"yfinance": types.SimpleNamespace(Ticker=ticker)}):
        stock_movers_context()
        stock_movers_context()
    assert len(calls) == 3