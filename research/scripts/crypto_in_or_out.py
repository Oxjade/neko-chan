"""A beginner's rule for crypto: be in the market, or stay out. No shorting, no leverage.

Each weekend the rule answers one question for the coming week: hold coins,
or hold cash? Several ways of deciding are compared with simply holding all
the time:

  mood        in when the Fear & Greed index is 50 or more
  trend       in when the price is above its 10-week average
  momentum    in when the last four weeks were up
  both        in only when mood and momentum agree
  either      in when mood or momentum says so

For each: how the money grew, the worst fall from a peak along the way, and
how much of the time it was invested. Judged on the whole history and on each
half. A switch between coins and cash costs 0.1% each way (a typical spot
exchange fee).

Weeks run Monday 00:00 UTC to Monday 00:00 UTC; the decision uses only what
was known at the start of the week.

    python research/scripts/crypto_in_or_out.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crypto_weekly_search as cws  # noqa: E402

OUT = cws.fws.REPO / "research" / "exports" / "tables" / "crypto_in_or_out.json"
SWITCH_COST_PCT = 0.1
BASKETS = {"BTC and ETH": ["BTC", "ETH"], "BTC only": ["BTC"], "Ten coins": list(cws.COINS)}


def signals(close: pd.DataFrame, mood: pd.Series) -> dict[str, pd.DataFrame]:
    """1 = hold coins for the coming week, 0 = hold cash. One column per coin."""
    weekly_mood = mood.resample("W-SUN").last().reindex(close.index)
    mood_in = pd.DataFrame({c: (weekly_mood >= 50).astype(float) for c in close.columns})
    trend_in = (close > close.rolling(10).mean()).astype(float)
    mom_in = (close / close.shift(4) > 1).astype(float)
    return {
        "Always hold": pd.DataFrame(1.0, index=close.index, columns=close.columns),
        "In when the mood is greedy": mood_in,
        "In when above the 10-week average": trend_in,
        "In when the last 4 weeks were up": mom_in,
        "In when mood and 4 weeks agree": mood_in * mom_in,
        "In when mood or 4 weeks say so": ((mood_in + mom_in) > 0).astype(float),
    }


def run(close: pd.DataFrame, position: pd.DataFrame, coins: list[str]) -> pd.Series:
    """Weekly return (as a fraction) of an equal split across the coins."""
    fwd = close[coins].pct_change().shift(-1)
    pos = position[coins].where(fwd.notna(), 0.0)
    switches = pos.diff().abs().fillna(pos)             # entering or leaving costs once
    weekly = (pos * fwd - switches * SWITCH_COST_PCT / 100).mean(axis=1)
    return weekly.iloc[:-1]                              # the last week has no outcome yet


def describe(weekly: pd.Series, position: pd.DataFrame, coins: list[str]) -> dict:
    growth = (1 + weekly).cumprod()
    peak = growth.cummax()
    invested = position[coins].reindex(weekly.index).mean(axis=1)
    years = len(weekly) / 52
    in_weeks = weekly[invested > 0]
    return {
        "weeks": int(len(weekly)),
        "grew_to": float(growth.iloc[-1]),                       # 1.0 = no change
        "per_year_pct": float((growth.iloc[-1] ** (1 / years) - 1) * 100) if years > 0 else None,
        "worst_fall_pct": float(((growth / peak) - 1).min() * 100),
        "worst_week_pct": float(weekly.min() * 100),
        "time_invested": float(invested.mean()),
        "switches_per_year": float(position[coins].reindex(weekly.index).diff().abs().sum().mean()
                                   / years) if years > 0 else None,
        "weeks_up_when_in": float((in_weeks > 0).mean()) if len(in_weeks) else None,
    }


def main() -> int:
    refresh = "--refresh" in sys.argv
    close = cws.daily_closes(refresh).resample("W-SUN").last().dropna(how="all")
    mood = cws.fear_greed(refresh)
    sig = signals(close, mood)
    split = close.index[len(close) // 2]
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "from": str(close.index[0].date()), "to": str(close.index[-1].date()),
        "split": str(split.date()), "switch_cost_pct": SWITCH_COST_PCT, "baskets": {},
    }
    for basket, coins in BASKETS.items():
        rows = []
        for name, position in sig.items():
            weekly = run(close, position, coins)
            rows.append({
                "rule": name,
                "all": describe(weekly, position, coins),
                "first_half": describe(weekly[weekly.index < split], position, coins),
                "second_half": describe(weekly[weekly.index >= split], position, coins),
            })
        result["baskets"][basket] = rows
    OUT.write_text(json.dumps(result, indent=1) + "\n")

    print(f"weekly, {result['from']} to {result['to']}; halves split at {result['split']}; "
          f"switching costs {SWITCH_COST_PCT}% each way")
    for basket, rows in result["baskets"].items():
        print(f"\n=== {basket} ===")
        print(f"{'rule':<36} {'1 USD became':>12} {'per year':>9} {'worst fall':>11} {'invested':>9}"
              f" | {'1st half':>9} {'fall':>6} | {'2nd half':>9} {'fall':>6}")
        for r in rows:
            a, f, s = r["all"], r["first_half"], r["second_half"]
            print(f"{r['rule']:<36} {a['grew_to']:>12.2f} {a['per_year_pct']:>8.0f}% "
                  f"{a['worst_fall_pct']:>10.0f}% {a['time_invested'] * 100:>8.0f}%"
                  f" | {f['grew_to']:>9.2f} {f['worst_fall_pct']:>5.0f}%"
                  f" | {s['grew_to']:>9.2f} {s['worst_fall_pct']:>5.0f}%")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
