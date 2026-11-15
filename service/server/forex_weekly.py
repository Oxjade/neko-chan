"""Weekly forex planner: what a plan of a given shape has done on a pair.

No rule tested in research/scripts/forex_weekly_search.py picks a forex
pair's direction for the week better than a coin flip, so this module does
not pick one. It takes the trader's own idea (pair, direction, stop, target)
and answers what can be measured:

  - how far the pair normally moves in a week right now;
  - over every week since 1999, how often a plan with this stop and target,
    scaled to that week's normal move, reached the target first, reached the
    stop first, or ran out the week;
  - the win rate that shape needs to break even, beside the win rate it had;
  - the position size for a chosen account risk.

Data: the European Central Bank's daily reference rates (one fixing a day).
Stops and targets are therefore checked on daily fixings, not intraday
prices, so real touches happen a little more often than counted here.
"""

from __future__ import annotations

import io
import threading
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import web_core

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
DISK_CACHE = Path(__file__).resolve().parent / "data" / "ecb_eurofxref.csv"
REPO_COPY = (Path(__file__).resolve().parents[2] / "research" / "exports" / "price_history"
             / "ecb_eurofxref.csv")
REFRESH_SECONDS = 6 * 3600

# pair -> (numerator column, denominator column or None); price = quote per base
PAIRS = {
    "EURUSD": ("USD", None), "GBPUSD": ("USD", "GBP"), "USDJPY": ("JPY", "USD"),
    "USDCHF": ("CHF", "USD"), "AUDUSD": ("USD", "AUD"), "NZDUSD": ("USD", "NZD"),
    "USDCAD": ("CAD", "USD"), "EURGBP": ("GBP", None), "EURJPY": ("JPY", None),
    "EURCHF": ("CHF", None), "GBPJPY": ("JPY", "GBP"), "AUDJPY": ("JPY", "AUD"),
    "EURAUD": ("AUD", None), "AUDNZD": ("NZD", "AUD"),
}
VOL_WEEKS = 26            # the "normal week" is measured over the last half year
COST_PCT = 0.04           # spread and commission for one round trip
LOT_UNITS = 100_000

_lock = threading.Lock()
_state: dict = {"loaded_at": 0.0, "daily": None}


class DataError(Exception):
    """Exchange-rate history could not be loaded."""


def _read(source) -> pd.DataFrame:
    df = pd.read_csv(source, na_values=["N/A"], parse_dates=["Date"], index_col="Date")
    return df.sort_index()


def daily_rates() -> pd.DataFrame:
    """ECB daily rates, refreshed every few hours; falls back to the last copy on disk."""
    with _lock:
        if _state["daily"] is not None and time.time() - _state["loaded_at"] < REFRESH_SECONDS:
            return _state["daily"]
    df = None
    try:
        resp = requests.get(ECB_URL, timeout=30)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            raw = z.read(z.namelist()[0])
        df = _read(io.BytesIO(raw))
        DISK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        DISK_CACHE.write_bytes(raw)
    except (requests.RequestException, zipfile.BadZipFile, ValueError, OSError):
        for path in (DISK_CACHE, REPO_COPY):
            if path.exists():
                df = _read(path)
                break
    if df is None or df.empty:
        raise DataError("Exchange-rate history is unavailable right now.")
    with _lock:
        _state.update(daily=df, loaded_at=time.time())
    return df


def pair_series(daily: pd.DataFrame, pair: str) -> pd.Series:
    num, den = PAIRS[pair]
    series = daily[num] if den is None else daily[num] / daily[den]
    return series.dropna()


def base_in_usd(daily: pd.DataFrame, pair: str) -> float:
    """Value of one unit of the pair's base currency in US dollars, latest fixing."""
    base = pair[:3]
    last = daily.dropna(subset=["USD"]).iloc[-1]
    if base == "USD":
        return 1.0
    return float(last["USD"]) if base == "EUR" else float(last["USD"] / last[base])


def weekly_frame(series: pd.Series) -> pd.DataFrame:
    """One row per completed week: Friday close, that week's move, and the
    normal weekly move (in percent) known at that Friday."""
    close = series.resample("W-FRI").last().dropna()
    ret = close.pct_change() * 100
    return pd.DataFrame({"close": close, "ret": ret,
                         "normal": ret.rolling(VOL_WEEKS).std()}).dropna()


def replay_shape(series: pd.Series, direction: str, stop_mult: float, target_mult: float) -> list[dict]:
    """Every past week traded with this shape, in units of that week's normal move.

    Entry at one Friday's fixing; each later daily fixing up to the next
    Friday is checked for the stop, then the target; otherwise the trade is
    closed at that Friday's fixing.
    """
    weeks = weekly_frame(series)
    sign = 1.0 if direction == "long" else -1.0
    trend = np.sign(weeks["close"] / weeks["close"].shift(13) - 1)
    out = []
    fridays = weeks.index
    for k in range(len(fridays) - 1):
        start, end = fridays[k], fridays[k + 1]
        entry, normal = weeks["close"].iloc[k], weeks["normal"].iloc[k]
        if not normal or np.isnan(normal):
            continue
        days = series[(series.index > start) & (series.index <= end)]
        if days.empty:
            continue
        result, how = None, "week ended"
        for price in days:
            move = (price / entry - 1) * 100 * sign / normal
            if move <= -stop_mult:
                result, how = -stop_mult, "stop"
                break
            if move >= target_mult:
                result, how = target_mult, "target"
                break
        if result is None:
            result = (days.iloc[-1] / entry - 1) * 100 * sign / normal
        t = trend.iloc[k]
        out.append({"closed_at": end.isoformat(), "how": how, "gross": float(result),
                    "net": float(result - COST_PCT / normal), "fees": float(COST_PCT / normal),
                    "with_trend": None if np.isnan(t) or t == 0 else bool(t == sign)})
    return out


def _summary(rows: list[dict], normal_now: float) -> dict:
    perf = web_core.performance(rows, 100.0)
    n = len(rows) or 1
    by = {how: [r for r in rows if r["how"] == how] for how in ("target", "stop", "week ended")}
    ended = by["week ended"]
    return {
        "weeks": len(rows),
        "win_rate": perf["win_rate"], "win_rate_low": perf["win_rate_low"],
        "win_rate_high": perf["win_rate_high"], "wins": perf["wins"],
        "breakeven_win_rate": perf["breakeven_win_rate"],
        "target_first": len(by["target"]) / n, "stop_first": len(by["stop"]) / n,
        "week_ended": len(ended) / n,
        "week_ended_avg_pct": (sum(r["net"] for r in ended) / len(ended) * normal_now) if ended else None,
        "avg_result_pct": (perf["expectancy"] or 0.0) * normal_now,
    }


def overview() -> dict:
    """This week's picture for every pair."""
    daily = daily_rates()
    rows = []
    for pair in PAIRS:
        series = pair_series(daily, pair)
        weeks = weekly_frame(series)
        if len(weeks) < 60:
            continue
        last = weeks.iloc[-1]
        price = float(series.iloc[-1])
        normal = float(last["normal"])
        inside = (weeks["ret"].abs().shift(-1) <= weeks["normal"]).iloc[:-1]
        close = weeks["close"]
        rows.append({
            "pair": pair, "price": price, "as_of": series.index[-1].date().isoformat(),
            "last_week_pct": float(last["ret"]), "normal_week_pct": normal,
            "range_low": price * (1 - normal / 100), "range_high": price * (1 + normal / 100),
            "range_held": float(inside.mean()),
            "trend_13w": "up" if close.iloc[-1] > close.iloc[-14] else "down",
            "trend_52w": "up" if close.iloc[-1] > close.iloc[-53] else "down",
        })
    return {"pairs": rows, "as_of": max(r["as_of"] for r in rows) if rows else None,
            "cost_pct": COST_PCT, "history_from": str(daily.index[0].date())}


def plan(pair: str, direction: str, stop_pct: float, target_pct: float,
         account: float, risk_pct: float) -> dict:
    daily = daily_rates()
    series = pair_series(daily, pair)
    weeks = weekly_frame(series)
    price = float(series.iloc[-1])
    normal = float(weeks["normal"].iloc[-1])
    stop_mult, target_mult = stop_pct / normal, target_pct / normal
    rows = replay_shape(series, direction, stop_mult, target_mult)
    if len(rows) < 50:
        raise DataError(f"{pair} has too little history to judge a plan.")
    sign = 1.0 if direction == "long" else -1.0
    trend_now = weeks["close"].iloc[-1] > weeks["close"].iloc[-14]
    with_now = bool(trend_now == (direction == "long"))

    risk_usd = account * risk_pct / 100
    position_usd = risk_usd / ((stop_pct + COST_PCT) / 100)
    units = position_usd / base_in_usd(daily, pair)
    result = {
        "pair": pair, "direction": direction, "price": price,
        "as_of": series.index[-1].date().isoformat(),
        "normal_week_pct": normal, "stop_pct": stop_pct, "target_pct": target_pct,
        "stop_price": price * (1 - sign * stop_pct / 100),
        "target_price": price * (1 + sign * target_pct / 100),
        "stop_in_weeks": stop_mult, "target_in_weeks": target_mult,
        "all": _summary(rows, normal),
        "with_trend": _summary([r for r in rows if r["with_trend"] is True], normal),
        "against_trend": _summary([r for r in rows if r["with_trend"] is False], normal),
        "trend_13w": "up" if trend_now else "down", "plan_is_with_trend": with_now,
        "sizing": {
            "position_usd": position_usd, "units": units, "lots": units / LOT_UNITS,
            "leverage": position_usd / account,
            "loss_at_stop_usd": position_usd * (stop_pct + COST_PCT) / 100,
            "gain_at_target_usd": position_usd * (target_pct - COST_PCT) / 100,
        },
        "cost_pct": COST_PCT, "history_from": str(weeks.index[0].date()),
    }
    result["avg_result_usd"] = position_usd * result["all"]["avg_result_pct"] / 100
    return result
