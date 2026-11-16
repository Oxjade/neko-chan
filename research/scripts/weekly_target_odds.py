"""What are the odds of turning 10 USD into 30 USD in one week?

Tripling an account in a week needs leverage, and leverage brings
liquidation. This script measures the trade-off on real history instead of
guessing: for every week in the 4-hour price history of five coins it places
one leveraged position, closes it the moment the account reaches the target,
and counts the account as lost if the price first moves far enough the other
way to liquidate it.

Two ways of choosing the direction are compared:
  trend   follow the 10-day/40-day average (the rule from strategy_search.py)
  random  no information (the average of going long and going short)

The mathematics sets a ceiling. In a game with no edge, the chance of
multiplying a stake by k before losing it is at most 1/k: one in three for a
triple. Costs push it lower; a real edge pushes it a little higher.

Conservative choices: a bar that touches both the liquidation price and the
target counts as a liquidation; liquidation happens at a full 1/leverage
adverse move (real exchanges liquidate slightly earlier). Not modelled:
funding payments.

    python research/scripts/weekly_target_odds.py
    python research/scripts/weekly_target_odds.py --start 10 --target 30 --days 7
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategy_search as ss  # noqa: E402

OUT = ss.REPO / "research" / "exports" / "tables" / "weekly_target_odds.json"
BARS_PER_DAY = 6
LEVERAGES = (1, 3, 5, 10, 20, 40)
FAST, SLOW = 60, 240          # 10-day and 40-day averages on 4h bars


def one_week(o, h, l, c, i, direction, lev, multiple, cost, bars):
    """Outcome of one leveraged position opened at bar i+1. Returns final/start."""
    entry = o[i + 1]
    need = (multiple - 1) / lev + cost / 100.0     # favourable move that nets the target
    wipe = 1.0 / lev                               # adverse move that liquidates
    end = min(i + bars, len(c) - 1)
    for j in range(i + 1, end + 1):
        up, down = h[j] / entry - 1, l[j] / entry - 1
        good, bad = (up, -down) if direction > 0 else (-down, -up)
        if bad >= wipe:
            return 0.0
        if good >= need:
            return multiple
    move = (c[end] / entry - 1) * direction
    return max(0.0, 1 + lev * (move - cost / 100.0))


def study(start: float, target: float, days: int) -> dict:
    multiple = target / start
    bars = days * BARS_PER_DAY
    out = {}
    for cost_name, cost in ss.COSTS.items():
        rows = []
        for lev in LEVERAGES:
            trend, rand = [], []
            for coin in ss.COINS:
                df = ss.load(coin, "4h")
                o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
                fast = df["close"].ewm(span=FAST, adjust=False).mean().to_numpy()
                slow = df["close"].ewm(span=SLOW, adjust=False).mean().to_numpy()
                for i in range(SLOW * 2, len(c) - bars - 1, bars):     # non-overlapping weeks
                    d = 1 if fast[i] > slow[i] else -1
                    trend.append(one_week(o, h, l, c, i, d, lev, multiple, cost, bars))
                    rand.append(one_week(o, h, l, c, i, 1, lev, multiple, cost, bars))
                    rand.append(one_week(o, h, l, c, i, -1, lev, multiple, cost, bars))

            def summary(results):
                r = np.array(results)
                return {
                    "weeks": int(len(r)),
                    "hit_target": float((r >= multiple).mean()),
                    "lost_everything": float((r <= 0).mean()),
                    "lost_half_or_more": float((r <= 0.5).mean()),
                    "median_final_usd": float(np.median(r) * start),
                    "average_final_usd": float(r.mean() * start),
                }
            rows.append({"leverage": lev, "trend": summary(trend), "random": summary(rand)})
        out[cost_name] = rows
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", type=float, default=10.0)
    ap.add_argument("--target", type=float, default=30.0)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    results = study(args.start, args.target, args.days)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "start_usd": args.start, "target_usd": args.target, "days": args.days,
        "fair_game_ceiling": args.start / args.target,
        "coins": ss.COINS, "costs_pct_round_trip": ss.COSTS, "results": results,
    }
    Path(args.out).write_text(json.dumps(payload, indent=1) + "\n")

    print(f"{args.start:g} USD -> {args.target:g} USD in {args.days} days. "
          f"No-edge ceiling: {args.start / args.target * 100:.0f}% of attempts.")
    for cost_name, rows in results.items():
        print(f"\n=== cost {ss.COSTS[cost_name]:.2f}% of position per round trip ({cost_name}) ===")
        print(f"{'lev':>4}  {'direction':<9} {'weeks':>5} {'hit target':>10} {'lost all':>9} "
              f"{'lost half+':>10} {'median end':>11} {'average end':>12}")
        for row in rows:
            for kind in ("trend", "random"):
                s = row[kind]
                print(f"{row['leverage']:>3}x  {kind:<9} {s['weeks']:>5} "
                      f"{s['hit_target'] * 100:>9.1f}% {s['lost_everything'] * 100:>8.1f}% "
                      f"{s['lost_half_or_more'] * 100:>9.1f}% "
                      f"{s['median_final_usd']:>10.2f}$ {s['average_final_usd']:>11.2f}$")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
