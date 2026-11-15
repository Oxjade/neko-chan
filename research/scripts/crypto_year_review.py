"""One calendar year of the weekly crypto calls, exactly as the app plays them now.

Every week that starts in the chosen year is replayed with the current rules:
long when the mood is greedy and the coin rose over four weeks, short when
fearful and it fell, only on the sides that are called, at the suggested
leverage, with the stop one normal week away, the take profit on shorts, fees
each way and funding on longs. A call whose position would be under the
exchange's 10 USD minimum is left out, as the app leaves it out.

The year is part of the history the rules were chosen from, so this is a
description of how they did, not an independent test of them.

    python research/scripts/crypto_year_review.py            # this year, 40 and 18 USD a week
    python research/scripts/crypto_year_review.py 2025 60    # another year or weekly amount
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crypto_exits as ce  # noqa: E402
import crypto_take_profit as tp  # noqa: E402
import crypto_weekly_search as cws  # noqa: E402

TABLES = cws.fws.REPO / "research" / "exports" / "tables"
MIN_ORDER_USD = 10.0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    year = int(args[0]) if args else datetime.now(timezone.utc).year
    stakes = [float(a) for a in args[1:]] or [40.0, 18.0]
    refresh = "--refresh" in sys.argv
    calls = json.loads((TABLES / "crypto_long_short.json").read_text())["calls"]
    targets = {side: s["target"] for side, s in
               json.loads((TABLES / "crypto_take_profit.json").read_text())["by_side"].items()}
    called = [(coin, side, c["suggested_leverage"]) for coin, sides in calls.items()
              for side, c in sides.items() if c["allowed"]]
    coins = len(calls)

    rows = []                                   # one per traded coin-week
    for coin, side, lev in called:
        rule = tp.plan("now", final=targets.get(side))
        old = tp.plan("stop only")
        for week in ce.traded_weeks(coin, side, refresh):
            if week["monday"].year != year:
                continue
            result, flags = tp.play(week, side, coin, lev, rule)
            rows.append({"monday": week["monday"], "coin": coin, "side": side, "leverage": lev,
                         "result": result, "stopped": flags["stopped"], "took_profit": flags["final_target"],
                         "stop_only": tp.play(week, side, coin, lev, old)[0]})
    if not rows:
        print(f"no traded weeks in {year}")
        return 1
    first, last = min(r["monday"] for r in rows), max(r["monday"] for r in rows)
    all_mondays = sorted({w["monday"] for coin, side, _ in called
                          for w in ce.traded_weeks(coin, side, False) if w["monday"].year == year})
    new_year = pd.Timestamp(year=year, month=1, day=1)
    first_monday = new_year + pd.Timedelta(days=(7 - new_year.weekday()) % 7)
    # Mondays of the year that the price history covers, with or without a call
    covered = max(w["monday"] for coin, side, _ in called for w in ce.traded_weeks(coin, side, False))
    end = min(covered, pd.Timestamp(year=year, month=12, day=31))
    weeks_in_year = int((end - first_monday).days / 7) + 1

    def line(name, sub):
        n = len(sub)
        won = sum(r["result"] > 0 for r in sub)
        total = sum(r["result"] for r in sub)
        print(f"  {name:<12} {n:>3} weeks  won {won:>2} lost {n - won:>2}  ({won / n * 100:>3.0f}% won)  "
              f"avg {total / n * 100:>+6.1f}%  total {total * 100:>+7.0f}%  worst {min(r['result'] for r in sub) * 100:>+5.0f}%  "
              f"stopped {sum(r['stopped'] for r in sub):>2}  took profit {sum(r['took_profit'] for r in sub):>2}")
        return {"weeks": n, "won": int(won), "avg": float(total / n), "total": float(total),
                "worst": float(min(r["result"] for r in sub)),
                "stopped": int(sum(r["stopped"] for r in sub)),
                "took_profit": int(sum(r["took_profit"] for r in sub))}

    print(f"{year}: weeks starting {first_monday.date()} to "
          f"{(first_monday + pd.Timedelta(weeks=weeks_in_year - 1)).date()} ({weeks_in_year} weeks, "
          f"{len(all_mondays)} with at least one call). Percentages are of the money put on that one trade.")
    out = {"year": year, "from": str(first_monday.date()), "to": str(last.date()), "weeks": weeks_in_year,
           "by_call": {}, "by_side": {}, "portfolios": {}}
    for coin, side, lev in called:
        sub = [r for r in rows if r["coin"] == coin and r["side"] == side]
        if sub:
            out["by_call"][f"{side} {coin}"] = line(f"{side} {coin} {lev}x", sub)
    print()
    for side in ("long", "short"):
        sub = [r for r in rows if r["side"] == side]
        if sub:
            out["by_side"][side] = line(f"all {side}s", sub)
    shorts = [r for r in rows if r["side"] == "short"]
    if shorts:
        print(f"  shorts with the stop only (the rule before the take profit): "
              f"total {sum(r['stop_only'] for r in shorts) * 100:+.0f}%, "
              f"won {sum(r['stop_only'] > 0 for r in shorts)} of {len(shorts)}")

    by_week = defaultdict(list)
    for r in rows:
        by_week[r["monday"]].append(r)
    for stake in stakes:
        share = stake / coins
        results, skipped_small = {}, 0
        for monday, trades in sorted(by_week.items()):
            placed = [t for t in trades if share * t["leverage"] >= MIN_ORDER_USD]
            skipped_small += len(trades) - len(placed)
            if placed:
                results[monday] = sum(share * t["result"] for t in placed)
        values = list(results.values())
        total, ahead = sum(values), sum(v > 0 for v in values)
        running, low, peak, drawdown = 0.0, 0.0, 0.0, 0.0
        for v in values:
            running += v
            peak = max(peak, running)
            drawdown = min(drawdown, running - peak)
            low = min(low, running)
        months = defaultdict(float)
        for monday, v in results.items():
            months[monday.strftime("%b")] += v
        print(f"\n  {stake:g} USD a week, split over {coins} coins ({share:g} each):")
        print(f"    weeks with a trade {len(values)} of {weeks_in_year}; ahead {ahead}, behind {len(values) - ahead}; "
              f"calls left out as too small {skipped_small}")
        print(f"    total {total:+.2f} USD; best week {max(values):+.2f}, worst week {min(values):+.2f}; "
              f"deepest fall from a high {drawdown:+.2f}")
        print("    by month: " + "  ".join(f"{m} {v:+.2f}" for m, v in months.items()))
        out["portfolios"][f"{stake:g}"] = {
            "traded_weeks": len(values), "ahead": int(ahead), "behind": int(len(values) - ahead),
            "total": float(total), "best": float(max(values)), "worst": float(min(values)),
            "drawdown": float(drawdown), "too_small": int(skipped_small),
            "by_month": {m: float(v) for m, v in months.items()},
            "weeks": [{"monday": str(m.date()), "result": float(v)} for m, v in results.items()]}
    path = TABLES / f"crypto_year_review_{year}.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
