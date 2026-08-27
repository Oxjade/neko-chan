"""Shared helpers for the Neko agent evaluation tools.

Loads the recorded live-agent decisions (live_agent_log.csv + the platform DB),
normalizes actions into LONG/SHORT/FLAT, fetches realized intraday price series
(Hyperliquid 5m for crypto, yfinance for forex/stocks), and computes returns
over the horizons Neko's data resolution supports (5m/15m/30m/1h/4h/24h).

No network access is performed by pure helpers; fetching is cached under a
--cache-dir so gate runs are reproducible offline after the first fetch.
"""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "service" / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

HORIZONS_MIN = [5, 15, 30, 60, 240, 1440]
HORIZON_LABELS = ["5m", "15m", "30m", "1h", "4h", "24h"]

MARKET_GROUPS = {
    "crypto": "CRYPTO",
    "forex": "FOREX",
    "us-stock": "STOCKS",
}

FEES = {"agent": {"optimistic": 0.0, "baseline": 0.001, "adverse": 0.002},
        "slippage": {"optimistic": 0.0000, "baseline": 0.0001, "adverse": 0.0005}}


def utc_parse(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        return pd.Timestamp(value, tz="UTC") if "T" in str(value) else pd.Timestamp(value, tz="UTC")
    except Exception:
        return None


def load_live_log(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def normalize_action(action: str) -> str:
    a = (action or "").strip().lower()
    if a == "buy":
        return "LONG"
    if a == "short":
        return "SHORT"
    if a in ("sell", "cover"):
        return "FLAT"
    return "FLAT"  # hold -> FLAT/HOLD


def db_signals(db_path: str | Path) -> list[dict[str, Any]]:
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = [dict(r) for r in cur.execute(
        "SELECT signal_id, agent_id, market, symbol, side, entry_price, quantity, "
        "executed_at, timestamp FROM signals WHERE message_type='operation' ORDER BY timestamp"
    )]
    con.close()
    return rows


# --------------------------------------------------------------------- price data

def fetch_hyperliquid_5m(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    import requests

    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "5m", "startTime": start_ms, "endTime": end_ms}}
    r = requests.post("https://api.hyperliquid.xyz/info", json=payload, timeout=30)
    data = r.json()
    rows = []
    for c in data:
        rows.append({"t": c["t"], "o": float(c["o"]), "h": float(c["h"]),
                     "l": float(c["l"]), "c": float(c["c"])})
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("t")[["o", "h", "l", "c"]]
    df.columns = ["Open", "High", "Low", "Close"]
    return df


def fetch_yfinance_5m(symbol: str, start: str, end: str) -> pd.DataFrame:
    import yfinance as yf

    df = yf.download(symbol, start=start, end=end, interval="5m", progress=False,
                     auto_adjust=True, threads=False)
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert("UTC")
    else:
        df.index = df.index.tz_localize("UTC")
    return df


def fetch_ohlc(symbol: str, market: str, start_utc: datetime, end_utc: datetime,
               cache_dir: Path | None = None) -> pd.DataFrame:
    """Realized 5m OHLC for a symbol over a window (cached to CSV)."""
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = f"{market}_{symbol}".replace("/", "_").replace("=", "_").upper()
        cache_path = cache_dir / f"{key}_5m.csv"
        if cache_path.exists():
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            if getattr(df.index, "tz", None) is None:
                df.index = df.index.tz_localize("UTC")
            df = df[~df.index.duplicated(keep="last")].sort_index()
            lo, hi = pd.Timestamp(start_utc), pd.Timestamp(end_utc)
            if lo.tzinfo is None:
                lo = lo.tz_localize("UTC")
            if hi.tzinfo is None:
                hi = hi.tz_localize("UTC")
            df = df[(df.index >= lo) & (df.index <= hi)]
            return df

    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    if market == "crypto":
        df = fetch_hyperliquid_5m(symbol, start_ms, end_ms)
    elif market == "forex":
        df = fetch_yfinance_5m(f"{symbol}=X",
                               start_utc.strftime("%Y-%m-%d"),
                               (end_utc + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    elif market == "us-stock":
        df = fetch_yfinance_5m(symbol,
                               start_utc.strftime("%Y-%m-%d"),
                               (end_utc + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    else:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])

    if cache_dir and len(df):
        out = df.copy()
        out.index = out.index.tz_convert("UTC")
        name = f"{market}_{symbol}".replace("/", "_").replace("=", "_").upper() + "_5m.csv"
        out.to_csv(cache_dir / name)
    return df


def price_at(series: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    """Close of the last candle at or before ts (no future data)."""
    past = series[series.index <= ts]
    if past.empty:
        return None
    return float(past["Close"].iloc[-1])


def return_from(series: pd.DataFrame, ts: pd.Timestamp, ref: float, horizon_min: int) -> float | None:
    """Realized return from ref price at ts over horizon minutes (or None if data ends early)."""
    target = ts + pd.Timedelta(minutes=horizon_min)
    future = price_at(series, target)
    if future is None or ref <= 0:
        return None
    return (future - ref) / ref


# ------------------------------------------------------------------- metrics

def binary_metrics(y_true: list[int], y_pred: list[int]) -> dict[str, float]:
    """y_true/y_pred are +1 (LONG) / -1 (SHORT) directional labels."""
    if not y_true:
        return {"accuracy": float("nan"), "precision": float("nan"), "recall": float("nan"),
                "f1": float("nan"), "n": 0}
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    acc = float(np.mean(np.sign(y_pred) == np.sign(y_true)))
    pos = y_pred == 1
    tp = int(np.sum((y_pred == 1) & (y_true == 1)))
    fp = int(np.sum((y_pred == 1) & (y_true != 1)))
    fn = int(np.sum((y_pred != 1) & (y_true == 1)))
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) and not math.isnan(precision + recall) else float("nan")
    return {"accuracy": acc, "precision": precision, "recall": recall, "f1": f1, "n": len(y_true)}


def block_bootstrap_ci(values: list[float], block: int = 6, n_boot: int = 2000, ci: float = 0.95) -> tuple[float, float]:
    """Block bootstrap (resample contiguous blocks) for autocorrelated samples."""
    x = np.asarray(values, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(42)
    n = len(x)
    block = min(block, n)
    n_blocks = int(np.ceil(n / block))
    stats_ = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, n - block + 1, size=n_blocks)
        sample = np.concatenate([x[s:s + block] for s in starts])[:n]
        stats_[b] = float(np.mean(sample))
    lo = (1 - ci) / 2
    return float(np.percentile(stats_, lo * 100)), float(np.percentile(stats_, (1 - lo) * 100))


def fmt(x: float | None, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{digits}f}"