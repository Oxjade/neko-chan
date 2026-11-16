"""Does adding shorts to the weekly crypto rule help? Long side and short side, separately.

The long rule on the "This week" page: hold a coin when the mood is greedy
(Fear & Greed 50 or more) and the coin rose over the last four weeks.
The mirror image for shorts: short a coin when the mood is fearful (below
50) and the coin fell over the last four weeks. Otherwise stay in cash.

Each side is reported on its own, for each half of the history, so a strong
long side cannot hide a weak short side. Three ways of shorting are tried:

  mirror    fearful mood and four weeks down
  mood      fearful mood alone
  extreme   extreme fear (below 25) alone

Weeks run Monday 00:00 UTC to Monday 00:00 UTC. Results are per week traded,
before costs (shorts on perps also receive or pay funding, not modelled).
For Bitcoin and Ether the leveraged figures count a liquidation when the
week's high (for a short) or low (for a long) crosses the exchange's limit.

    python research/scripts/crypto_long_short.py
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crypto_weekly_search as cws  # noqa: E402

OUT = cws.fws.REPO / "research" / "exports" / "tables" / "crypto_long_short.json"
# coins the platform can call, with the exchange's maximum leverage for each
CALL_COINS = {"BTC": "Bitcoin", "ETH": "Ether", "SOL": "Solana", "SUI": "Sui"}
MAX_LEVERAGE = {"BTC": 40, "ETH": 25, "SOL": 20, "SUI": 10}
FEE_PER_SIDE = 0.00045
FUNDING_PER_DAY = 0.0003          # typical cost of a leveraged long; shorts usually receive it
LEVELS = (1, 2, 3, 5, 7, 10, 20, 30)
MAX_LIQUIDATED = 0.02             # suggested leverage: stake lost in at most 1 week in 50
LONG, SHORT = "Long: greedy mood and 4 weeks up", "Short: fearful mood and 4 weeks down"


def ohlc(coin: str, refresh: bool) -> pd.DataFrame:
    path = cws.DATA / f"{coin}_1d_ohlc.csv"
    if refresh or not path.exists():
        rows = cws._post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1d", "startTime": 0, "endTime": int(time.time() * 1000)}})
        pd.DataFrame([{"date": pd.Timestamp(int(r["t"]), unit="ms"), "open": float(r["o"]),
                       "high": float(r["h"]), "low": float(r["l"]), "close": float(r["c"])}
                      for r in rows[:-1]]).to_csv(path, index=False)
    return pd.read_csv(path, parse_dates=["date"]).set_index("date")


def sides(close: pd.DataFrame, mood: pd.Series) -> dict[str, pd.DataFrame]:
    """+1 long, -1 short, 0 cash, decided at each week's end for the next week."""
    m = mood.resample("W-SUN").last().reindex(close.index)
    greedy = pd.DataFrame({c: m >= 50 for c in close.columns})
    fearful = pd.DataFrame({c: m < 50 for c in close.columns})
    extreme = pd.DataFrame({c: m < 25 for c in close.columns})
    up = close / close.shift(4) > 1
    down = close / close.shift(4) < 1
    return {
        "Long: greedy mood and 4 weeks up": (greedy & up).astype(float),
        "Short: fearful mood and 4 weeks down": -(fearful & down).astype(float),
        "Short: fearful mood alone": -fearful.astype(float),
        "Short: extreme fear alone": -extreme.astype(float),
    }


def stats(gross: pd.DataFrame, active: pd.DataFrame) -> dict:
    values = gross.where(active).stack().dropna()      # only the weeks actually traded
    weekly = gross.where(active).mean(axis=1).dropna()
    sd = weekly.std(ddof=1) if len(weekly) > 1 else 0.0
    return {
        "coin_weeks": int(len(values)),
        "right": float((values > 0).mean()) if len(values) else None,
        "avg_pct": float(values.mean()) if len(values) else None,
        "sum_pct": float(values.sum()),
        "t": float(weekly.mean() / (sd / math.sqrt(len(weekly)))) if sd > 0 else None,
    }


def by_half(close: pd.DataFrame, position: pd.DataFrame, coins: list[str], split) -> dict:
    if len(coins) == 1:
        # one coin: split its own history in two, so a coin listed later is still judged on halves
        own = close[coins[0]].dropna().index
        split = own[len(own) // 2]
    fwd = close[coins].pct_change().shift(-1) * 100
    pos = position[coins].where(fwd.notna(), 0.0)
    gross, active = pos * fwd, pos != 0
    first, second = close.index < split, close.index >= split
    return {"first_half": stats(gross[first], active[first]),
            "second_half": stats(gross[second], active[second]),
            "all": stats(gross, active)}


def leveraged(coin: str, position: pd.Series, daily: pd.DataFrame, leverage: float) -> dict:
    """Fresh stake of 1 each traded week; result per week as a share of the stake."""
    leverage = min(leverage, MAX_LEVERAGE[coin])           # the exchange's own limit
    wipe = (1.0 / leverage - 1.0 / (2 * MAX_LEVERAGE[coin])) if leverage > 1 else 1.0
    results = []
    for sunday, side in position.items():
        if side == 0:
            continue
        week = daily[(daily.index > sunday) & (daily.index <= sunday + pd.Timedelta(days=7))]
        if len(week) < 7:
            continue
        entry = week["open"].iloc[0]
        against = (1 - week["low"].min() / entry) if side > 0 else (week["high"].max() / entry - 1)
        move = (week["close"].iloc[-1] / entry - 1) * side
        funding = 7 * FUNDING_PER_DAY if (side > 0 and leverage > 1) else 0.0
        res = leverage * (move - 2 * FEE_PER_SIDE - funding)
        results.append(-1.0 if against >= wipe else max(res, -1.0))
    r = np.array(results)
    return {"weeks": int(len(r)), "ahead": float((r > 0).mean()) if len(r) else None,
            "liquidated": float((r <= -1 + 1e-9).mean()) if len(r) else None,
            "avg_per_week": float(r.mean()) if len(r) else None, "total": float(r.sum())}


def main() -> int:
    refresh = "--refresh" in sys.argv
    daily = cws.daily_closes(refresh)
    for coin in CALL_COINS:                      # coins outside the ten-coin file come from their candles
        if coin not in daily.columns:
            daily = daily.join(ohlc(coin, refresh)["close"].rename(coin), how="outer")
    close = daily.resample("W-SUN").last().dropna(how="all")
    mood = cws.fear_greed(refresh)
    rules = sides(close, mood)
    split = close.index[len(close) // 2]
    baskets = {name: [coin] for coin, name in CALL_COINS.items()}
    baskets["Ten coins"] = list(cws.COINS)
    result = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "from": str(close.index[0].date()), "to": str(close.index[-1].date()),
              "split": str(split.date()), "unleveraged": {}, "leveraged": {},
              "coin_names": CALL_COINS, "max_leverage": MAX_LEVERAGE}
    print(f"weekly, {result['from']} to {result['to']}; halves split at {result['split']}; before costs")
    for basket, coins in baskets.items():
        print(f"\n=== {basket} ===")
        print(f"{'side':<40} {'first half: weeks right    avg':>32} {'second half: weeks right    avg':>33} "
              f"{'all: sum':>10}")
        result["unleveraged"][basket] = {}
        for name, position in rules.items():
            s = by_half(close, position, coins, split)
            result["unleveraged"][basket][name] = s
            a, b = s["first_half"], s["second_half"]

            def cell(x):
                if not x["coin_weeks"]:
                    return f"{'none':>32}"
                return f"{x['coin_weeks']:>14} {x['right'] * 100:5.1f}% {x['avg_pct']:+7.2f}%"
            print(f"{name:<40} {cell(a)} {cell(b)}  {s['all']['sum_pct']:+9.0f}%")

    print("\n=== With leverage, fresh stake each traded week (result as a multiple of the stake) ===")
    print(f"{'coin':<5} {'side':<40} {'lev':>4} {'weeks':>6} {'ahead':>6} {'liquidated':>11} "
          f"{'avg week':>9} {'total':>8}")
    result["calls"] = {}
    names = CALL_COINS
    for coin in CALL_COINS:
        daily = ohlc(coin, refresh)
        result["leveraged"][coin] = {}
        result["calls"][coin] = {}
        for side, name in (("long", LONG), ("short", SHORT)):
            result["leveraged"][coin][name] = {}
            for lev in LEVELS:
                s = leveraged(coin, rules[name][coin], daily, lev)
                result["leveraged"][coin][name][f"{lev}x"] = s
                print(f"{coin:<5} {name:<40} {lev:>3}x {s['weeks']:>6} {s['ahead'] * 100:>5.0f}% "
                      f"{s['liquidated'] * 100:>10.0f}% {s['avg_per_week']:>+9.3f} {s['total']:>+8.1f}")
            halves = result["unleveraged"][names[coin]][name]
            # A side is only called if it was right more often than not AND
            # made money in BOTH halves of the history.
            allowed = all(halves[h]["right"] is not None and halves[h]["right"] > 0.5
                          and halves[h]["avg_pct"] > 0 for h in ("first_half", "second_half"))
            by = result["leveraged"][coin][name]
            safe = [lev for lev in LEVELS if by[f"{lev}x"]["liquidated"] <= MAX_LIQUIDATED
                    and by[f"{lev}x"]["avg_per_week"] > 0]
            result["calls"][coin][side] = {
                "allowed": bool(allowed and safe),
                "suggested_leverage": max(safe) if safe else None,
                "first_half": halves["first_half"], "second_half": halves["second_half"],
                "by_leverage": by,
            }
    print("\n=== The calls the platform will make ===")
    for coin, sides_ in result["calls"].items():
        for side, c in sides_.items():
            print(f"{coin} {side:<5} {'CALL' if c['allowed'] else 'skip'}  suggested leverage "
                  f"{c['suggested_leverage']}x  right {c['first_half']['right'] * 100:.0f}% / "
                  f"{c['second_half']['right'] * 100:.0f}%")
    OUT.write_text(json.dumps(result, indent=1) + "\n")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
