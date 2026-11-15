"""Stop loss, take profit and limit entry for the weekly crypto calls: what helps, what hurts.

The weekly calls (research/scripts/crypto_long_short.py) open on Monday and
close the next Monday. This script asks whether adding a stop loss, a take
profit or a limit entry improves them, by replaying every traded week for the
sides the platform actually calls (long Bitcoin, long Ether, short Ether) at
the suggested 3x leverage.

Levels are measured in "normal weeks": the size of a typical weekly move for
that coin over the previous half year, known before the week starts. A stop
of 1.0 means one normal week's move against the trade.

  stop loss     none, or 0.5 / 0.75 / 1 / 1.5 / 2 normal weeks from entry
  take profit   none, or 1 / 1.5 / 2 / 3 normal weeks from entry
  limit entry   at Monday's open, or 0.1 / 0.2 / 0.3 normal weeks better,
                filled only if the price gets there on Monday or Tuesday

Daily candles are used, so when a day touches both the stop and the target
the stop is counted first, and a limit fill is checked against the same
day's worst price. Fees are charged each way; longs pay a typical funding
cost for the days held.

A choice is only recommended if it is at least as good as plain "hold to
next Monday" in BOTH halves of the history.

    python research/scripts/crypto_exits.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crypto_long_short as cls  # noqa: E402
import crypto_weekly_search as cws  # noqa: E402

OUT = cws.fws.REPO / "research" / "exports" / "tables" / "crypto_exits.json"
LEVERAGE = 3
CALLED = (("BTC", "long"), ("ETH", "long"), ("ETH", "short"))
STOPS = (None, 0.5, 0.75, 1.0, 1.5, 2.0)
TARGETS = (None, 1.0, 1.5, 2.0, 3.0)
LIMITS = (0.0, 0.1, 0.2, 0.3)
VOL_WEEKS = 26


def traded_weeks(coin: str, side: str, refresh: bool) -> list[dict]:
    """Each week the side was signalled: the seven daily candles and the normal weekly move."""
    close = cws.daily_closes(refresh).resample("W-SUN").last().dropna(how="all")
    position = cls.sides(close, cws.fear_greed(refresh))[cls.LONG if side == "long" else cls.SHORT][coin]
    normal = (close[coin].pct_change() * 100).rolling(VOL_WEEKS).std()
    daily = cls.ohlc(coin, refresh)
    weeks = []
    for sunday, flag in position.items():
        if flag == 0 or np.isnan(normal.get(sunday, np.nan)):
            continue
        days = daily[(daily.index > sunday) & (daily.index <= sunday + pd.Timedelta(days=7))]
        if len(days) < 7:
            continue
        weeks.append({"monday": days.index[0], "normal": float(normal[sunday]) / 100,
                      "days": days[["open", "high", "low", "close"]].to_numpy()})
    return weeks


def play(week: dict, side: str, coin: str, stop: float | None, target: float | None,
         limit: float = 0.0, leverage: float = LEVERAGE) -> float | None:
    """Result of one week per 1 USD of stake, or None if a limit entry never filled."""
    sign = 1.0 if side == "long" else -1.0
    days, normal = week["days"], week["normal"]
    entry, first_day = days[0][0], 0
    if limit > 0:
        want = entry * (1 - sign * limit * normal)          # a better price than the open
        first_day = next((i for i in (0, 1)
                          if (days[i][2] <= want if side == "long" else days[i][1] >= want)), None)
        if first_day is None:
            return None
        entry = want
    liquidation = cls.MAX_LEVERAGE[coin] and (1.0 / leverage - 1.0 / (2 * cls.MAX_LEVERAGE[coin]))
    stop_move = min(stop * normal, liquidation) if stop else liquidation
    target_move = target * normal if target else None
    held_days, move, closed = 0, None, False
    for i in range(first_day, 7):
        _, high, low, close = days[i]
        against = (1 - low / entry) if side == "long" else (high / entry - 1)
        favour = (high / entry - 1) if side == "long" else (1 - low / entry)
        held_days += 1
        if against >= stop_move:                             # the stop is counted before the target
            move = -1.0 / leverage if (not stop or stop * normal >= liquidation) else -stop_move
            closed = True
            break
        if target_move is not None and favour >= target_move:
            move, closed = target_move, True
            break
    if not closed:
        move = (days[6][3] / entry - 1) * sign
    funding = held_days * cls.FUNDING_PER_DAY if side == "long" else 0.0
    return max(leverage * (move - 2 * cls.FEE_PER_SIDE - funding), -1.0)


def summarise(results: list[tuple[pd.Timestamp, float]], split) -> dict:
    def part(rows):
        r = np.array([v for _, v in rows])
        if not len(r):
            return {"weeks": 0, "ahead": None, "avg": None, "worst": None, "total": 0.0}
        return {"weeks": int(len(r)), "ahead": float((r > 0).mean()), "avg": float(r.mean()),
                "worst": float(r.min()), "total": float(r.sum())}
    return {"all": part(results), "first_half": part([x for x in results if x[0] < split]),
            "second_half": part([x for x in results if x[0] >= split])}


def label(stop, target, limit=0.0) -> str:
    s = "no stop" if stop is None else f"stop {stop:g}"
    t = "no target" if target is None else f"target {target:g}"
    return f"{s}, {t}" + (f", limit {limit:g} better" if limit else "")


def main() -> int:
    refresh = "--refresh" in sys.argv
    weeks = {(coin, side): traded_weeks(coin, side, refresh) for coin, side in CALLED}
    all_mondays = sorted(w["monday"] for ws in weeks.values() for w in ws)
    split = all_mondays[len(all_mondays) // 2]

    def run(stop, target, limit=0.0) -> dict:
        pooled, by_call = [], {}
        for (coin, side), ws in weeks.items():
            rows = [(w["monday"], r) for w in ws
                    if (r := play(w, side, coin, stop, target, limit)) is not None]
            by_call[f"{side} {coin}"] = summarise(rows, split)
            pooled += rows
        return {"stop": stop, "target": target, "limit": limit, "pooled": summarise(pooled, split),
                "by_call": by_call}

    grid = [run(s, t) for s in STOPS for t in TARGETS]
    base = grid[0]["pooled"]

    def at_least_as_good(row) -> bool:
        p = row["pooled"]
        return (p["first_half"]["avg"] >= base["first_half"]["avg"]
                and p["second_half"]["avg"] >= base["second_half"]["avg"])

    print(f"called sides at {LEVERAGE}x, halves split at {split.date()}; result per 1 USD of weekly stake")
    print(f"{'exit':<28} {'weeks':>6} {'ahead':>6} {'avg week':>9} {'worst':>7} {'1st half':>9} "
          f"{'2nd half':>9}  as good as holding")
    for row in grid:
        p = row["pooled"]
        print(f"{label(row['stop'], row['target']):<28} {p['all']['weeks']:>6} {p['all']['ahead'] * 100:>5.0f}% "
              f"{p['all']['avg']:>+9.3f} {p['all']['worst']:>+7.2f} {p['first_half']['avg']:>+9.3f} "
              f"{p['second_half']['avg']:>+9.3f}  {'YES' if at_least_as_good(row) and row is not grid[0] else ''}")

    # Protective stop: the tightest stop that keeps at least 80% of plain holding's average
    # and stays profitable in both halves. It is there to cap a bad week, not to add profit.
    protective = None
    for stop in sorted(x for x in STOPS if x):
        row = next(r for r in grid if r["stop"] == stop and r["target"] is None)
        p = row["pooled"]
        if (p["all"]["avg"] >= 0.8 * base["all"]["avg"] and p["first_half"]["avg"] > 0
                and p["second_half"]["avg"] > 0):
            protective = row
            break
    better = [r for r in grid[1:] if at_least_as_good(r)]
    chosen = max(better, key=lambda r: min(r["pooled"]["first_half"]["avg"],
                                           r["pooled"]["second_half"]["avg"])) if better else None
    # An optional far target: the nearest one that, with the stop, does not lower the average.
    stop_used = (chosen or protective or grid[0])["stop"]
    alone = next(r for r in grid if r["stop"] == stop_used and r["target"] is None)
    optional = next((r for r in sorted((r for r in grid if r["stop"] == stop_used and r["target"]),
                                       key=lambda r: r["target"])
                     if r["pooled"]["all"]["avg"] >= alone["pooled"]["all"]["avg"]), None)

    exit_row = chosen or protective or grid[0]
    limits = [run(exit_row["stop"], exit_row["target"], lim) for lim in LIMITS]
    print(f"\nlimit entry, with {label(exit_row['stop'], exit_row['target'])}:")
    print(f"{'entry':<28} {'weeks':>6} {'ahead':>6} {'avg week':>9} {'total':>8} {'1st half':>9} {'2nd half':>9}")
    for row in limits:
        p = row["pooled"]
        name = "at Monday's open" if not row["limit"] else f"limit {row['limit']:g} better"
        print(f"{name:<28} {p['all']['weeks']:>6} {p['all']['ahead'] * 100:>5.0f}% {p['all']['avg']:>+9.3f} "
              f"{p['all']['total']:>+8.1f} {p['first_half']['avg']:>+9.3f} {p['second_half']['avg']:>+9.3f}")
    # A limit must not lower the total: missed weeks are missed winners as often as losers.
    market = limits[0]["pooled"]
    limit_ok = [r for r in limits[1:]
                if r["pooled"]["all"]["total"] >= market["all"]["total"]
                and r["pooled"]["first_half"]["total"] >= market["first_half"]["total"]
                and r["pooled"]["second_half"]["total"] >= market["second_half"]["total"]]
    best_limit = max(limit_ok, key=lambda r: r["pooled"]["all"]["total"]) if limit_ok else None

    recommendation = {
        "stop": exit_row["stop"], "target": exit_row["target"],
        "limit": best_limit["limit"] if best_limit else 0.0,
        "optional_target": optional["target"] if optional and not (chosen and chosen["target"]) else None,
        "optional_target_with": optional["pooled"] if optional else None,
        "improves_on_holding": bool(chosen),
        "stop_is_protective_only": bool(not chosen and protective),
        "with": (best_limit or limits[0])["pooled"], "holding": base,
        "by_call": (best_limit or limits[0])["by_call"],
    }
    print("\nrecommendation:", label(recommendation["stop"], recommendation["target"], recommendation["limit"]),
          "| improves on holding:", recommendation["improves_on_holding"],
          "| protective only:", recommendation["stop_is_protective_only"],
          "| optional far target:", recommendation["optional_target"])
    w, hold = recommendation["with"], recommendation["holding"]
    print(f"with it: avg {w['all']['avg']:+.3f} worst {w['all']['worst']:+.2f} ahead {w['all']['ahead'] * 100:.0f}% "
          f"| plain holding: avg {hold['all']['avg']:+.3f} worst {hold['all']['worst']:+.2f} "
          f"ahead {hold['all']['ahead'] * 100:.0f}%")
    for call, st in recommendation["by_call"].items():
        print(f"  {call:<10} weeks {st['all']['weeks']:>4} ahead {st['all']['ahead'] * 100:>3.0f}% "
              f"avg {st['all']['avg']:+.3f} worst {st['all']['worst']:+.2f}")
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "leverage": LEVERAGE, "split": str(split.date()), "vol_weeks": VOL_WEEKS,
        "grid": grid, "limits": limits, "recommendation": recommendation}, indent=1) + "\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
