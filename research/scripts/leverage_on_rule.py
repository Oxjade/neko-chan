"""What happens if the weekly hold-or-cash rule is traded with leverage on perps?

Every week the rule says "hold", a fresh stake is put into a leveraged long
on Bitcoin at Monday 00:00 UTC and closed the following Monday. If the price
falls far enough during the week the position is liquidated and the stake is
lost. Weeks the rule says "cash" are skipped.

Liquidation uses each day's low. The exchange liquidates before the whole
stake is gone: on Hyperliquid the maintenance margin for Bitcoin is 1.25% of
the position, so at 30x a fall of about 2.1% ends the trade, not 3.3%.

Costs: 0.045% of the position each way, and funding at a typical 0.03% of
the position per day (longs usually pay it in the greedy markets this rule
buys). Both scale with leverage.

    python research/scripts/leverage_on_rule.py --stake 18 --leverage 30
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crypto_in_or_out as cio  # noqa: E402
import crypto_weekly_search as cws  # noqa: E402

DATA = cws.DATA
OUT = cws.fws.REPO / "research" / "exports" / "tables" / "leverage_on_rule.json"
COIN = "BTC"
MAINTENANCE = 0.0125           # Hyperliquid, Bitcoin: half the initial margin at its 40x maximum
FEE_PER_SIDE = 0.00045
FUNDING_PER_DAY = 0.0003
RULE = "In when mood and 4 weeks agree"


def daily_ohlc(refresh: bool) -> pd.DataFrame:
    path = DATA / f"{COIN}_1d_ohlc.csv"
    if refresh or not path.exists():
        rows = cws._post({"type": "candleSnapshot", "req": {
            "coin": COIN, "interval": "1d", "startTime": 0, "endTime": int(time.time() * 1000)}})
        df = pd.DataFrame([{"date": pd.Timestamp(int(r["t"]), unit="ms"), "open": float(r["o"]),
                            "high": float(r["h"]), "low": float(r["l"]), "close": float(r["c"])}
                           for r in rows[:-1]])
        df.to_csv(path, index=False)
    return pd.read_csv(path, parse_dates=["date"]).set_index("date")


def weeks_in(refresh: bool) -> pd.DataFrame:
    """One row per week the rule held Bitcoin: entry, the week's low and its close."""
    close = cws.daily_closes(refresh).resample("W-SUN").last().dropna(how="all")
    hold = cio.signals(close, cws.fear_greed(refresh))[RULE][COIN]
    ohlc = daily_ohlc(refresh)
    rows = []
    for sunday, flag in hold.items():
        if flag != 1:
            continue
        week = ohlc[(ohlc.index > sunday) & (ohlc.index <= sunday + pd.Timedelta(days=7))]
        if len(week) < 7:
            continue
        entry = week["open"].iloc[0]
        rows.append({"week": week.index[0], "entry": entry,
                     "low_move": week["low"].min() / entry - 1,
                     "close_move": week["close"].iloc[-1] / entry - 1})
    return pd.DataFrame(rows)


def outcomes(weeks: pd.DataFrame, leverage: float, stake: float) -> pd.Series:
    """Profit or loss in dollars for each week, on a fresh stake."""
    wipe = -(1.0 / leverage - MAINTENANCE) if leverage > 1 else -1.0
    costs = leverage * (2 * FEE_PER_SIDE + (7 * FUNDING_PER_DAY if leverage > 1 else 0.0))
    result = stake * (leverage * weeks["close_move"] - costs)
    result = result.where(weeks["low_move"] > wipe, -stake)
    return result.clip(lower=-stake)


def summarise(result: pd.Series, stake: float, years: float) -> dict:
    lost_all = result <= -stake + 1e-9
    return {
        "weeks": int(len(result)),
        "lost_whole_stake": float(lost_all.mean()),
        "weeks_with_a_gain": float((result > 0).mean()),
        "average_per_week": float(result.mean()),
        "typical_week": float(result.median()),
        "best_week": float(result.max()),
        "total": float(result.sum()),
        "put_in": float(stake * len(result)),
        "per_year": float(result.sum() / years),
    }


def weekly_picture(weeks: pd.DataFrame, result: pd.Series, refresh: bool) -> dict:
    """What a week, a month and a quarter looked like, counting cash weeks too."""
    close = cws.daily_closes(refresh).resample("W-SUN").last().dropna(how="all")
    every = pd.Series(0.0, index=close.index[:-1] + pd.Timedelta(days=1))    # each week's Monday
    every.loc[weeks["week"].values] = result.values
    month, quarter = every.rolling(4).sum().dropna(), every.rolling(13).sum().dropna()
    return {
        "all_weeks": int(len(every)),
        "weeks_ahead": float((every > 0).mean()),
        "weeks_behind": float((every < 0).mean()),
        "weeks_no_trade": float((every == 0).mean()),
        "months_ahead": float((month > 0).mean()),
        "months_behind": float((month < 0).mean()),
        "quarters_ahead": float((quarter > 0).mean()),
        "quarters_behind": float((quarter < 0).mean()),
        "longest_losing_run_weeks": int(max((len(list(g)) for k, g in __import__("itertools").groupby(
            every[every != 0] < 0) if k), default=0)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stake", type=float, default=18.0)
    ap.add_argument("--leverage", type=float, default=30.0)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    weeks = weeks_in(args.refresh)
    years = (weeks["week"].iloc[-1] - weeks["week"].iloc[0]).days / 365.25
    levels = sorted({1.0, 3.0, 5.0, 10.0, 20.0, args.leverage})
    table = {}
    for lev in levels:
        res = outcomes(weeks, lev, args.stake)
        table[f"{lev:g}x"] = {**summarise(res, args.stake, years),
                              "picture": weekly_picture(weeks, res, args.refresh)}
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coin": COIN, "rule": RULE, "stake_usd": args.stake,
        "from": str(weeks["week"].iloc[0].date()), "to": str(weeks["week"].iloc[-1].date()),
        "years": years, "maintenance_margin": MAINTENANCE,
        "fee_per_side": FEE_PER_SIDE, "funding_per_day": FUNDING_PER_DAY, "by_leverage": table,
    }
    OUT.write_text(json.dumps(result, indent=1) + "\n")

    print(f"{COIN}, {len(weeks)} weeks the rule said hold, {result['from']} to {result['to']} "
          f"({years:.1f} years). Fresh {args.stake:g} USD each of those weeks.")
    print(f"{'leverage':>8} {'lost it all':>12} {'weeks up':>9} {'average week':>13} {'typical week':>13} "
          f"{'best week':>10} {'total result':>13} {'money put in':>13}")
    for name, s in table.items():
        print(f"{name:>8} {s['lost_whole_stake'] * 100:>11.0f}% {s['weeks_with_a_gain'] * 100:>8.0f}% "
              f"{s['average_per_week']:>+12.2f}$ {s['typical_week']:>+12.2f}$ {s['best_week']:>+9.2f}$ "
              f"{s['total']:>+12.2f}$ {s['put_in']:>12.2f}$")
    print(f"\n{'leverage':>8} {'weeks ahead':>12} {'behind':>7} {'no trade':>9} | {'months ahead':>13} "
          f"{'behind':>7} | {'quarters ahead':>15} {'behind':>7} | longest losing run")
    for name, s in table.items():
        p = s["picture"]
        print(f"{name:>8} {p['weeks_ahead'] * 100:>11.0f}% {p['weeks_behind'] * 100:>6.0f}% "
              f"{p['weeks_no_trade'] * 100:>8.0f}% | {p['months_ahead'] * 100:>12.0f}% "
              f"{p['months_behind'] * 100:>6.0f}% | {p['quarters_ahead'] * 100:>14.0f}% "
              f"{p['quarters_behind'] * 100:>6.0f}% | {p['longest_losing_run_weeks']} weeks")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
