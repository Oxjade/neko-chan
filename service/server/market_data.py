"""Market data for the desk: prices and 4-hour bars from Hyperliquid's public API.

Covers crypto perpetuals on the main exchange and the stock, index, forex and
commodity perpetuals on its "xyz" venue. Everything is cached in memory.
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np
import requests

HL_URL = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz/info")
CRYPTO = ("BTC", "ETH", "SOL", "SUI", "HYPE", "XRP", "DOGE", "BNB", "AVAX", "LINK", "ADA", "LTC")
XYZ = "xyz"
FOREX = {"EUR", "GBP", "JPY"}
INDICES = {"SP500", "XYZ100", "JP225", "KR200", "NIFTY", "MAGS", "SMH", "XBI", "XLE", "TLT",
           "EWJ", "EWT", "EWY", "EWZ", "SOXL", "KORU", "URNM"}
COMMODITIES = {"GOLD", "SILVER", "COPPER", "PLATINUM", "PALLADIUM", "CL", "BRENTOIL", "NATGAS",
               "HO", "ALUMINIUM"}
CLASSES = ("crypto", "stocks", "indices", "forex", "commodities")

H4_MS = 4 * 3600 * 1000
HISTORY_BARS = 5000
MIN_BARS = 242
CANDLE_TTL, MARKET_TTL = 600, 30

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


class DataError(Exception):
    """Market data could not be loaded."""


def cached(key: str, ttl: float, load):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = load()
    with _lock:
        _cache[key] = (now, value)
    return value


def _post(payload: dict):
    try:
        resp = requests.post(HL_URL, json=payload, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise DataError(f"Market data is unavailable right now ({type(exc).__name__}).") from exc


def asset_class(symbol: str) -> str:
    if ":" not in symbol:
        return "crypto"
    name = symbol.split(":", 1)[1]
    if name in FOREX:
        return "forex"
    if name in COMMODITIES:
        return "commodities"
    if name in INDICES:
        return "indices"
    return "stocks"


def label(symbol: str) -> str:
    return symbol.split(":", 1)[-1]


def catalogue() -> dict[str, dict]:
    """Every market the desk can watch, with its live price."""
    def load():
        out = {}
        for dex in ("", XYZ):
            payload = {"type": "metaAndAssetCtxs"}
            if dex:
                payload["dex"] = dex
            meta, ctxs = _post(payload)
            for asset, ctx in zip(meta["universe"], ctxs):
                name = asset["name"]
                if asset.get("isDelisted") or not ctx.get("markPx"):
                    continue
                if not dex and name not in CRYPTO:
                    continue
                price = float(ctx["markPx"])
                prev = float(ctx["prevDayPx"]) if ctx.get("prevDayPx") else None
                out[name] = {
                    "symbol": name, "label": label(name), "class": asset_class(name),
                    "price": price,
                    "day_change_pct": (price / prev - 1) * 100 if prev else None,
                    "funding_hourly": float(ctx.get("funding") or 0.0),
                    "max_leverage": int(asset.get("maxLeverage") or 1),
                    "day_volume_usd": float(ctx.get("dayNtlVlm") or 0.0),
                }
        return out
    return cached("catalogue", MARKET_TTL, load)


def candles(symbol: str) -> dict[str, np.ndarray]:
    """Closed 4-hour bars, oldest first."""
    def load():
        now = int(time.time() * 1000)
        rows = _post({"type": "candleSnapshot", "req": {
            "coin": symbol, "interval": "4h", "startTime": now - HISTORY_BARS * H4_MS,
            "endTime": now}})
        rows = [r for r in rows if int(r["t"]) + H4_MS <= now]
        if len(rows) < MIN_BARS:
            raise DataError(f"{label(symbol)} has too little price history for a signal yet.")
        return {
            "t": np.array([int(r["t"]) for r in rows], dtype=np.int64),
            "open": np.array([float(r["o"]) for r in rows]),
            "high": np.array([float(r["h"]) for r in rows]),
            "low": np.array([float(r["l"]) for r in rows]),
            "close": np.array([float(r["c"]) for r in rows]),
        }
    return cached(f"candles:{symbol}", CANDLE_TTL, load)
