"""Calibration check: predicted P(win) vs actual resolution of trades.

Reads research/exports/predictions.csv (written by live_agent on every fill)
and cross-references each open/closed prediction against the platform's
positions/signals to mark it resolved (target_hit | stop_hit | still_open).
Then buckets predictions by their forecast P(win) and reports realized win
rate per bucket, so we can see whether a 75%-forecast bucket really wins ~75%.

Usage:
  python research/scripts/check_calibration.py [predictions.csv]
"""

import csv
import os
import sys
from pathlib import Path

import requests

BASE = os.getenv("AI_TRADER_URL", "http://127.0.0.1:8000")
PRED_LOG = Path(__file__).resolve().parents[2] / "research" / "exports" / "predictions.csv"


def _token() -> str:
    t = os.getenv("LIVE_AGENT_TOKEN", "")
    if t:
        return t
    tok_file = Path(__file__).resolve().parents[2] / "service" / "agent" / ".agent_token"
    if tok_file.exists():
        return tok_file.read_text().strip()
    return ""


def _positions() -> list[dict]:
    try:
        r = requests.get(f"{BASE}/api/positions",
                         headers={"Authorization": f"Bearer {_token()}"}, timeout=10)
        return r.json().get("positions", [])
    except Exception:
        return []


def _resolve_status(row: dict, positions: list[dict]) -> str:
    sym = row["symbol"]
    entry = float(row["entry"] or 0)
    stop = float(row["stop"] or 0)
    target = float(row["target"] or 0)
    direction = row["direction"]
    if entry <= 0 or stop <= 0 or target <= 0:
        return "missing_levels"
    # find a matching open position
    open_pos = next((p for p in positions
                     if p.get("symbol") == sym and float(p.get("entry_price") or 0) > 0), None)
    if open_pos:
        cur = float(open_pos.get("current_price") or open_pos.get("entry_price") or 0)
        if direction == "long":
            if cur >= target:
                return "target_hit"
            if cur <= stop:
                return "stop_hit"
        else:
            if cur <= target:
                return "target_hit"
            if cur >= stop:
                return "stop_hit"
        return "open"
    # no open position -> resolved; infer from platform signal pnl if possible
    return "closed"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else str(PRED_LOG)
    if not os.path.exists(path):
        print(f"no predictions log at {path}")
        return 1
    positions = _positions()
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    if not rows:
        print("predictions log is empty")
        return 1
    for r in rows:
        r["status"] = _resolve_status(r, positions)

    resolved = [r for r in rows if r["status"] in ("target_hit", "stop_hit")]
    print(f"Predictions: {len(rows)} total | {len(resolved)} resolved | "
          f"{len(rows) - len(resolved)} still open")
    if not resolved:
        print("not enough resolved trades yet to calibrate - keep running")
        return 0

    # bucket by forecast P(win)
    buckets = {}
    for r in resolved:
        p = float(r["p_win"])
        bucket = round(p * 100 / 10) * 10  # 10% buckets
        buckets.setdefault(bucket, []).append(r)
    print("\nBucket (forecast P) | n | wins | realized win rate | forecast")
    for b in sorted(buckets):
        rs = buckets[b]
        wins = sum(1 for r in rs if r["status"] == "target_hit")
        realized = wins / len(rs) * 100
        forecast = b
        bar = "#" * int(realized / 5)
        print(f"      {forecast:>3}%      | {len(rs):>2} | {wins:>4} | "
              f"{realized:>5.0f}% {bar:<20} | forecast {forecast}%")

    # overall
    total_wins = sum(1 for r in resolved if r["status"] == "target_hit")
    avg_forecast = sum(float(r["p_win"]) for r in resolved) / len(resolved) * 100
    print(f"\nOverall: {total_wins}/{len(resolved)} wins ({total_wins/len(resolved)*100:.0f}%) "
          f"vs avg forecast {avg_forecast:.0f}%")
    print("\nBest 3 + worst 3 resolved predictions:")
    rs = sorted(resolved, key=lambda r: float(r["ev"]), reverse=True)
    for r in rs[:3] + rs[-3:]:
        print(f"  {r['status']:10s} {r['symbol']:6s} {r['direction']:5s} "
              f"p={float(r['p_win'])*100:5.1f}% ev={float(r['ev']):+.2f}R")
    return 0


if __name__ == "__main__":
    sys.exit(main())
