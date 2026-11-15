"""Weekly forex rules, tested on 27 years of daily rates.

The question: if you plan one trade per pair at the weekend and hold it for
the week, does any simple rule pick the direction better than a coin flip,
after costs?

Data: the European Central Bank's daily reference rates since 1999, turned
into Friday-to-Friday weekly closes for 14 pairs. Each rule decides at one
Friday's close and is judged on the move to the next Friday's close.

Rules are fixed in advance and judged twice: on 1999-2012 ("choose") and on
2013 to now ("check"). A rule counts only if it is profitable after costs in
both. Pairs move together, so the significance figure is computed on the
weekly average across pairs, not on each pair-week as if independent.

Limits: reference rates are one fixing a day, so there are no intraday highs
and lows and no stops are modelled; overnight financing (swap) is not
included.

    python research/scripts/forex_weekly_search.py
"""

from __future__ import annotations

import io
import json
import math
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

REPO = Path(__file__).resolve().parents[2]
CACHE = REPO / "research" / "exports" / "price_history" / "ecb_eurofxref.csv"
OUT = REPO / "research" / "exports" / "tables" / "forex_weekly_search.json"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"

# pair -> (numerator column, denominator column or None); price = quote per base
PAIRS = {
    "EURUSD": ("USD", None), "GBPUSD": ("USD", "GBP"), "USDJPY": ("JPY", "USD"),
    "USDCHF": ("CHF", "USD"), "AUDUSD": ("USD", "AUD"), "NZDUSD": ("USD", "NZD"),
    "USDCAD": ("CAD", "USD"), "EURGBP": ("GBP", None), "EURJPY": ("JPY", None),
    "EURCHF": ("CHF", None), "GBPJPY": ("JPY", "GBP"), "AUDJPY": ("JPY", "AUD"),
    "EURAUD": ("AUD", None), "AUDNZD": ("NZD", "AUD"),
}
COST_PCT = 0.04          # spread and commission for one week's round trip
SPLIT = "2013-01-01"


def load_daily(refresh: bool = False) -> pd.DataFrame:
    """ECB daily rates (units of each currency per euro), oldest first."""
    if refresh or not CACHE.exists():
        resp = requests.get(ECB_URL, timeout=60)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_bytes(z.read(z.namelist()[0]))
    df = pd.read_csv(CACHE, na_values=["N/A"], parse_dates=["Date"], index_col="Date")
    return df.sort_index()


def weekly_closes(daily: pd.DataFrame | None = None) -> pd.DataFrame:
    daily = load_daily() if daily is None else daily
    out = {}
    for pair, (num, den) in PAIRS.items():
        series = daily[num] if den is None else daily[num] / daily[den]
        out[pair] = series
    frame = pd.DataFrame(out).resample("W-FRI").last().dropna(how="all")
    return frame


# ---- rules: each returns the position (+1, -1 or 0) decided at each Friday close

def momentum(close: pd.DataFrame, weeks: int) -> pd.DataFrame:
    return np.sign(close / close.shift(weeks) - 1)


def reversal(close: pd.DataFrame) -> pd.DataFrame:
    return -np.sign(close / close.shift(1) - 1)


def fade_big_move(close: pd.DataFrame, sigmas: float) -> pd.DataFrame:
    ret = close.pct_change()
    vol = ret.rolling(26).std().shift(1)
    return (-np.sign(ret)).where(ret.abs() > sigmas * vol, 0.0)


def above_average(close: pd.DataFrame, weeks: int) -> pd.DataFrame:
    return np.sign(close - close.rolling(weeks).mean())


def breakout(close: pd.DataFrame, weeks: int) -> pd.DataFrame:
    hi = close.rolling(weeks).max()
    lo = close.rolling(weeks).min()
    raw = pd.DataFrame(np.where(close >= hi, 1.0, np.where(close <= lo, -1.0, np.nan)),
                       index=close.index, columns=close.columns)
    return raw.ffill().fillna(0.0)          # hold until the opposite extreme


def rules() -> list[tuple[str, callable]]:
    out = [(f"Follow the last {w} week{'s' if w > 1 else ''}", lambda c, w=w: momentum(c, w))
           for w in (1, 4, 13, 26, 52)]
    out.append(("Fade last week", reversal))
    out += [(f"Fade a move bigger than {s:g} normal weeks", lambda c, s=s: fade_big_move(c, s))
            for s in (1.5, 2.0)]
    out += [(f"Side of the {w}-week average", lambda c, w=w: above_average(c, w))
            for w in (10, 20, 40)]
    out += [(f"Breakout of the {w}-week range", lambda c, w=w: breakout(c, w)) for w in (4, 13, 26)]
    return out


def evaluate(close: pd.DataFrame, position: pd.DataFrame) -> dict:
    """Judge positions decided at week t on the move to week t+1."""
    fwd = close.pct_change().shift(-1) * 100            # next week's move, percent
    pos = position.where(fwd.notna(), 0.0).fillna(0.0)
    gross = pos * fwd
    net = gross - COST_PCT * (pos != 0)
    out = {}
    for name, mask in (("choose", close.index < SPLIT), ("check", close.index >= SPLIT)):
        g, n_, p = gross[mask], net[mask], pos[mask]
        active = p != 0
        trades = int(active.values.sum())
        decided = (g != 0) & active
        right = int(((g > 0) & active).values.sum())
        weekly = n_.where(active).mean(axis=1).dropna()      # average across pairs each week
        sd = weekly.std(ddof=1)
        out[name] = {
            "pair_weeks": trades,
            "accuracy": right / int(decided.values.sum()) if decided.values.sum() else None,
            "avg_net_pct": float(n_.where(active).stack().mean()) if trades else None,
            "weeks": int(len(weekly)),
            "t": float(weekly.mean() / (sd / math.sqrt(len(weekly)))) if sd > 0 else None,
            "yearly_pct": float(weekly.mean() * 52) if len(weekly) else None,
            "sharpe": float(weekly.mean() / sd * math.sqrt(52)) if sd > 0 else None,
        }
    return out


def main() -> int:
    close = weekly_closes(load_daily(refresh="--refresh" in sys.argv))
    rows = []
    for name, fn in rules():
        stats = evaluate(close, fn(close))
        passed = all(stats[k]["avg_net_pct"] is not None and stats[k]["avg_net_pct"] > 0
                     for k in ("choose", "check"))
        rows.append({"rule": name, **stats, "passed": passed})
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ECB euro foreign exchange reference rates",
        "pairs": list(PAIRS), "from": str(close.index[0].date()), "to": str(close.index[-1].date()),
        "cost_pct_per_week": COST_PCT, "split": SPLIT,
        "passed": sum(r["passed"] for r in rows), "tried": len(rows), "rows": rows,
    }
    OUT.write_text(json.dumps(result, indent=1) + "\n")

    print(f"{len(PAIRS)} pairs, weekly, {result['from']} to {result['to']}, "
          f"cost {COST_PCT}% per week traded")
    print(f"{'rule':<40} {'1999-2012: right   avg     t':>30} {'2013-now: right   avg     t':>30}  pass")
    for r in sorted(rows, key=lambda r: -(r["check"]["avg_net_pct"] or -9)):
        a, b = r["choose"], r["check"]
        print(f"{r['rule']:<40} {a['accuracy'] * 100:>17.1f}% {a['avg_net_pct']:+.3f}% {a['t']:+5.1f} "
              f"{b['accuracy'] * 100:>16.1f}% {b['avg_net_pct']:+.3f}% {b['t']:+5.1f}  "
              f"{'YES' if r['passed'] else ''}")
    print(f"passed both periods: {result['passed']} of {result['tried']}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
