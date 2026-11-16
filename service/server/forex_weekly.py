"""Weekly forex planner: what a plan of a given shape has done on a pair.

No rule tested in research/scripts/forex_weekly_search.py picks a forex
pair's direction for the week better than a coin flip, so this module does
not pick one. It takes the trader's own idea (pair, direction, stop and
target in pips) and answers what can be measured:

  - how far the pair normally moves in a week right now;
  - over every week since 1999, how often a plan with this stop and target,
    scaled to that week's normal move, reached the target first, reached the
    stop first, or ran out the week;
  - the win rate that shape needs to break even, beside the win rate it had;
  - the position size for a chosen account risk, and what one pip is worth.

Distances are given and shown in pips, the unit forex brokers quote: the
fourth decimal place of the price, or the second for pairs quoted in yen.
Inside, a distance is a percentage of the current price, so the replay
compares like with like across 27 years of very different price levels.

Weeks run from one Friday's fixing to the next. Everything measured per week
(last week's move, the normal week, the trends, the plan's history) uses
finished weeks only; the week still being traded is reported apart, as "this
week so far".

Data: the European Central Bank's daily reference rates (one fixing a day).
Stops and targets are therefore checked on daily fixings, not intraday
prices, so real touches happen a little more often than counted here.
"""

from __future__ import annotations

import io
import threading
import time
import zipfile
from datetime import date, datetime, timezone
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
LOT_UNITS = 100_000       # one standard lot, in units of the base currency
MAX_PIPS = 5000           # the furthest stop or target the planner accepts

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


def pip_size(pair: str) -> float:
    """One pip in price terms: 0.01 for pairs quoted in yen, 0.0001 for the rest."""
    return 0.01 if pair.endswith("JPY") else 0.0001


def pips_per_percent(pair: str, price: float) -> float:
    """How many pips one percent of the price is."""
    return price / pip_size(pair) / 100


def plan_problem(pair: str, direction: str, stop_pips: float, target_pips: float,
                 spread_pips: float = 0.0) -> str | None:
    """What is wrong with a plan request, in plain words, or None when it can be checked."""
    if pair not in PAIRS:
        return "That pair is not on the list."
    if direction not in ("long", "short"):
        return "Choose long or short."
    if not 0 < stop_pips <= MAX_PIPS:
        return f"Enter the stop distance in pips: more than 0 and at most {MAX_PIPS}."
    if not 0 < target_pips <= MAX_PIPS:
        return f"Enter the target distance in pips: more than 0 and at most {MAX_PIPS}."
    if not 0 <= spread_pips < stop_pips:
        return "Enter your broker's spread in pips: 0 or more, and smaller than the stop."
    return None


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


def today_utc() -> date:
    return datetime.fromtimestamp(time.time(), timezone.utc).date()


def week_in_progress(friday: date, last_fixing: date, today: date) -> bool:
    """Whether the week that ends on this Friday is still being traded.

    It is until that Friday's fixing is in. Once the Friday is behind us the
    week counts as finished even if the Friday had no fixing (a holiday)."""
    return friday >= today and last_fixing < friday


def weekly_frame(series: pd.Series, today: date | None = None) -> pd.DataFrame:
    """One row per completed week: Friday close, that week's move, and the
    normal weekly move (in percent) known at that Friday.

    From Monday's first fixing the newest week is still in progress; it is
    left out, so a day or two of trading is never counted as a whole week."""
    close = series.resample("W-FRI").last().dropna()
    if len(close):
        friday, last_fixing = close.index[-1].date(), series.index[-1].date()
        if week_in_progress(friday, last_fixing, today or today_utc()):
            close = close.iloc[:-1]
    ret = close.pct_change() * 100
    return pd.DataFrame({"close": close, "ret": ret,
                         "normal": ret.rolling(VOL_WEEKS).std()}).dropna()


def replay_shape(series: pd.Series, direction: str, stop_mult: float, target_mult: float,
                 cost_pct: float = 0.0) -> list[dict]:
    """Every past week traded with this shape, in units of that week's normal move.

    cost_pct is the round-trip trading cost as a percent of the position; it
    is zero unless the trader supplies their broker's spread.

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
                    "net": float(result - cost_pct / normal), "fees": float(cost_pct / normal),
                    "with_trend": None if np.isnan(t) or t == 0 else bool(t == sign)})
    return out


def _summary(rows: list[dict], normal_now: float, pips_per_pct: float) -> dict:
    """How a set of past weeks went, in percent of the price and in pips
    (pips_per_pct is how many pips one percent of the current price is)."""
    perf = web_core.performance(rows, 100.0)
    n = len(rows) or 1
    by = {how: [r for r in rows if r["how"] == how] for how in ("target", "stop", "week ended")}
    ended = by["week ended"]
    ended_avg = (sum(r["net"] for r in ended) / len(ended) * normal_now) if ended else None
    avg = (perf["expectancy"] or 0.0) * normal_now
    return {
        "weeks": len(rows),
        "win_rate": perf["win_rate"], "win_rate_low": perf["win_rate_low"],
        "win_rate_high": perf["win_rate_high"], "wins": perf["wins"],
        "breakeven_win_rate": perf["breakeven_win_rate"],
        "target_first": len(by["target"]) / n, "stop_first": len(by["stop"]) / n,
        "week_ended": len(ended) / n,
        "week_ended_avg_pct": ended_avg,
        "avg_result_pct": avg,
        "week_ended_avg_pips": None if ended_avg is None else ended_avg * pips_per_pct,
        "avg_result_pips": avg * pips_per_pct,
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
        pip = pip_size(pair)
        # The week being traded now: the latest fixing against the last finished Friday close.
        last_close = float(close.iloc[-1])
        started = series.index[-1] > weeks.index[-1]
        rows.append({
            "pair": pair, "price": price, "as_of": series.index[-1].date().isoformat(),
            "last_week_pct": float(last["ret"]),
            "this_week_pct": (price / last_close - 1) * 100 if started else None,
            "normal_week_pct": normal,
            "pip_size": pip,
            # last week's move as it happened, and a normal week at today's price
            "last_week_pips": float(close.iloc[-1] - close.iloc[-2]) / pip,
            "this_week_pips": (price - last_close) / pip if started else None,
            "normal_week_pips": normal * pips_per_percent(pair, price),
            "range_low": price * (1 - normal / 100), "range_high": price * (1 + normal / 100),
            "range_held": float(inside.mean()),
            "trend_13w": "up" if close.iloc[-1] > close.iloc[-14] else "down",
            "trend_52w": "up" if close.iloc[-1] > close.iloc[-53] else "down",
        })
    return {"pairs": rows, "as_of": max(r["as_of"] for r in rows) if rows else None,
            "history_from": str(daily.index[0].date())}


def plan(pair: str, direction: str, stop_pips: float, target_pips: float,
         account: float, risk_pct: float, spread_pips: float = 0.0) -> dict:
    """What a plan of this shape has done, and how big to trade it.

    The stop, the target and the broker's round-trip spread are in pips."""
    daily = daily_rates()
    series = pair_series(daily, pair)
    weeks = weekly_frame(series)
    price = float(series.iloc[-1])
    normal = float(weeks["normal"].iloc[-1])
    pip = pip_size(pair)
    pct_per_pip = pip / price * 100           # one pip as a percent of the current price
    stop_pct = stop_pips * pct_per_pip
    target_pct = target_pips * pct_per_pip
    cost_pct = spread_pips * pct_per_pip
    stop_mult, target_mult = stop_pct / normal, target_pct / normal
    rows = replay_shape(series, direction, stop_mult, target_mult, cost_pct)
    if len(rows) < 50:
        raise DataError(f"{pair} has too little history to judge a plan.")
    sign = 1.0 if direction == "long" else -1.0
    trend_now = weeks["close"].iloc[-1] > weeks["close"].iloc[-14]
    with_now = bool(trend_now == (direction == "long"))

    pips_per_pct = pips_per_percent(pair, price)
    base_usd = base_in_usd(daily, pair)
    quote_usd = base_usd / price              # one unit of the quote currency, in dollars
    risk_usd = account * risk_pct / 100
    position_usd = risk_usd / ((stop_pct + cost_pct) / 100)
    units = position_usd / base_usd
    pip_value_usd = units * pip * quote_usd
    result = {
        "pair": pair, "direction": direction, "price": price,
        "as_of": series.index[-1].date().isoformat(),
        "pip_size": pip, "stop_pips": stop_pips, "target_pips": target_pips, "spread_pips": spread_pips,
        "normal_week_pct": normal, "normal_week_pips": normal * pips_per_pct,
        "stop_pct": stop_pct, "target_pct": target_pct,
        "stop_price": price * (1 - sign * stop_pct / 100),
        "target_price": price * (1 + sign * target_pct / 100),
        "stop_in_weeks": stop_mult, "target_in_weeks": target_mult,
        "all": _summary(rows, normal, pips_per_pct),
        "with_trend": _summary([r for r in rows if r["with_trend"] is True], normal, pips_per_pct),
        "against_trend": _summary([r for r in rows if r["with_trend"] is False], normal, pips_per_pct),
        "trend_13w": "up" if trend_now else "down", "plan_is_with_trend": with_now,
        "sizing": {
            "position_usd": position_usd, "units": units, "lots": units / LOT_UNITS,
            "leverage": position_usd / account,
            "pip_value_usd": pip_value_usd,
            "pip_value_per_lot_usd": LOT_UNITS * pip * quote_usd,
            "loss_at_stop_usd": (stop_pips + spread_pips) * pip_value_usd,
            "gain_at_target_usd": (target_pips - spread_pips) * pip_value_usd,
        },
        "cost_pct": cost_pct, "history_from": str(weeks.index[0].date()),
    }
    result["avg_result_usd"] = position_usd * result["all"]["avg_result_pct"] / 100
    return result
