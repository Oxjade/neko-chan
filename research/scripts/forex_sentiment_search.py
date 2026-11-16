"""Does positioning ("sentiment") help pick a forex pair's direction for the week?

Sentiment here is the US regulator's weekly Commitments of Traders report:
how large speculators, commercial hedgers and small traders are positioned in
each currency future. It is the one free sentiment source with decades of
history, so it can be tested instead of assumed.

Timing is strict. A report describes Tuesday's positions and is published on
Friday evening, after that day's rate fixing. So a report is used no earlier
than the following Monday's fixing, and the trade is held to the Monday
after.

Each rule is judged on 1999-2012 ("choose") and on 2013 to now ("check"),
without costs and with them. A rule counts only if it is right more than half
the time and profitable in both periods.

    python research/scripts/forex_sentiment_search.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forex_weekly_search as fws  # noqa: E402

COT_CACHE = fws.REPO / "research" / "exports" / "price_history" / "cftc_cot_fx.csv"
OUT = fws.REPO / "research" / "exports" / "tables" / "forex_sentiment_search.json"
COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

# currency future -> (contract code, pair, +1 if the pair rises when the currency rises)
FUTURES = {
    "EUR": ("099741", "EURUSD", 1), "GBP": ("096742", "GBPUSD", 1), "AUD": ("232741", "AUDUSD", 1),
    "NZD": ("112741", "NZDUSD", 1), "JPY": ("097741", "USDJPY", -1), "CHF": ("092741", "USDCHF", -1),
    "CAD": ("090741", "USDCAD", -1),
}
FIELDS = ("report_date_as_yyyy_mm_dd,cftc_contract_market_code,open_interest_all,"
          "noncomm_positions_long_all,noncomm_positions_short_all,comm_positions_long_all,"
          "comm_positions_short_all,nonrept_positions_long_all,nonrept_positions_short_all")
COST_PCT = 0.04
SPLIT = "2013-01-01"
PUBLISH_LAG_DAYS = 6          # Tuesday's positions are usable from the next Monday


def load_cot(refresh: bool = False) -> pd.DataFrame:
    if refresh or not COT_CACHE.exists():
        codes = ",".join(f"'{code}'" for code, _, _ in FUTURES.values())
        rows, offset = [], 0
        while True:
            resp = requests.get(COT_URL, params={
                "$select": FIELDS, "$where": f"cftc_contract_market_code in({codes})",
                "$order": "report_date_as_yyyy_mm_dd", "$limit": 50000, "$offset": offset}, timeout=120)
            resp.raise_for_status()
            page = resp.json()
            rows += page
            if len(page) < 50000:
                break
            offset += 50000
        pd.DataFrame(rows).to_csv(COT_CACHE, index=False)
    df = pd.read_csv(COT_CACHE, dtype={"cftc_contract_market_code": str},
                     parse_dates=["report_date_as_yyyy_mm_dd"])
    return df.rename(columns={"report_date_as_yyyy_mm_dd": "date"})


def positioning(cot: pd.DataFrame, currency: str) -> pd.DataFrame:
    """Net positions as a share of open interest, indexed by report date."""
    code = FUTURES[currency][0]
    d = cot[cot["cftc_contract_market_code"] == code].drop_duplicates("date").set_index("date").sort_index()
    oi = d["open_interest_all"].replace(0, np.nan)
    out = pd.DataFrame({
        "spec": (d["noncomm_positions_long_all"] - d["noncomm_positions_short_all"]) / oi,
        "hedger": (d["comm_positions_long_all"] - d["comm_positions_short_all"]) / oi,
        "small": (d["nonrept_positions_long_all"] - d["nonrept_positions_short_all"]) / oi,
    })
    for name in ("spec", "small"):
        lo = out[name].rolling(156, min_periods=52).min()
        hi = out[name].rolling(156, min_periods=52).max()
        out[f"{name}_rank"] = (out[name] - lo) / (hi - lo)      # 0 = most short in 3 years
    out["spec_change_1w"] = out["spec"].diff(1)
    out["spec_change_4w"] = out["spec"].diff(4)
    return out


def monday_closes() -> pd.DataFrame:
    daily = fws.load_daily()
    out = {}
    for currency, (_, pair, _) in FUTURES.items():
        num, den = fws.PAIRS[pair]
        out[currency] = daily[num] if den is None else daily[num] / daily[den]
    return pd.DataFrame(out).resample("W-MON").last().dropna(how="all")


def aligned(cot: pd.DataFrame, close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """For each currency, the latest positioning usable at each Monday."""
    out = {}
    mondays = pd.DataFrame({"monday": close.index})
    for currency in FUTURES:
        pos = positioning(cot, currency).reset_index()
        pos["usable"] = pos["date"] + pd.Timedelta(days=PUBLISH_LAG_DAYS)
        merged = pd.merge_asof(mondays, pos.sort_values("usable"), left_on="monday",
                               right_on="usable", tolerance=pd.Timedelta(days=21))
        out[currency] = merged.set_index("monday")
    return out


def extremes(rank: pd.Series, fade: bool) -> pd.Series:
    side = pd.Series(0.0, index=rank.index)
    side[rank > 0.9] = -1.0 if fade else 1.0
    side[rank < 0.1] = 1.0 if fade else -1.0
    return side


RULES = [
    ("Trade with large speculators", lambda p: np.sign(p["spec"])),
    ("Trade against large speculators", lambda p: -np.sign(p["spec"])),
    ("Fade speculators at a 3-year extreme", lambda p: extremes(p["spec_rank"], True)),
    ("Join speculators at a 3-year extreme", lambda p: extremes(p["spec_rank"], False)),
    ("Follow last week's change in speculator position", lambda p: np.sign(p["spec_change_1w"])),
    ("Follow the 4-week change in speculator position", lambda p: np.sign(p["spec_change_4w"])),
    ("Trade against small traders", lambda p: -np.sign(p["small"])),
    ("Fade small traders at a 3-year extreme", lambda p: extremes(p["small_rank"], True)),
    ("Trade with commercial hedgers", lambda p: np.sign(p["hedger"])),
]


def evaluate(close: pd.DataFrame, side: pd.DataFrame, cost: float) -> dict:
    """side: +1/-1/0 for each currency against the dollar, decided at each Monday."""
    fwd = close.pct_change().shift(-1) * 100            # currency vs dollar, next week
    # USDJPY, USDCHF, USDCAD are quoted dollar-first, so the currency's move is the inverse.
    for currency, (_, _, sign) in FUTURES.items():
        if sign < 0:
            fwd[currency] = -fwd[currency]
    pos = side.reindex_like(fwd).where(fwd.notna(), 0.0).fillna(0.0)
    gross = pos * fwd
    net = gross - cost * (pos != 0)
    out = {}
    for name, mask in (("choose", close.index < SPLIT), ("check", close.index >= SPLIT)):
        g, n_, p = gross[mask], net[mask], pos[mask]
        active = p != 0
        trades = int(active.values.sum())
        decided = int(((g != 0) & active).values.sum())
        weekly = n_.where(active).mean(axis=1).dropna()
        sd = weekly.std(ddof=1)
        out[name] = {
            "pair_weeks": trades,
            "accuracy": int(((g > 0) & active).values.sum()) / decided if decided else None,
            "avg_pct": float(n_.where(active).stack().mean()) if trades else None,
            "t": float(weekly.mean() / (sd / math.sqrt(len(weekly)))) if sd and sd > 0 else None,
        }
    return out


def main() -> int:
    cot = load_cot(refresh="--refresh" in sys.argv)
    close = monday_closes()
    close = close[close.index >= "1999-01-01"]
    pos = aligned(cot, close)
    rows = []
    for name, fn in RULES:
        side = pd.DataFrame({c: fn(pos[c]) for c in FUTURES}).reindex(close.index)
        row = {"rule": name, "no_costs": evaluate(close, side, 0.0),
               "with_costs": evaluate(close, side, COST_PCT)}
        row["passed"] = all(row["no_costs"][k]["accuracy"] and row["no_costs"][k]["accuracy"] > 0.5
                            and row["with_costs"][k]["avg_pct"] > 0 for k in ("choose", "check"))
        rows.append(row)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "CFTC Commitments of Traders, legacy futures only; ECB reference rates",
        "currencies": list(FUTURES), "from": str(close.index[0].date()), "to": str(close.index[-1].date()),
        "cost_pct": COST_PCT, "split": SPLIT, "passed": sum(r["passed"] for r in rows),
        "tried": len(rows), "rows": rows,
    }
    OUT.write_text(json.dumps(result, indent=1) + "\n")

    print(f"{len(FUTURES)} currencies vs the dollar, weekly, {result['from']} to {result['to']}")
    print(f"{'rule (no costs)':<50} {'1999-2012: weeks right    avg     t':>36} "
          f"{'2013-now: weeks right    avg     t':>36}  pass")
    for r in rows:
        a, b = r["no_costs"]["choose"], r["no_costs"]["check"]
        print(f"{r['rule']:<50} {a['pair_weeks']:>14} {a['accuracy'] * 100:5.1f}% {a['avg_pct']:+.3f}% "
              f"{a['t'] or 0:+5.1f} {b['pair_weeks']:>14} {b['accuracy'] * 100:5.1f}% "
              f"{b['avg_pct']:+.3f}% {b['t'] or 0:+5.1f}  {'YES' if r['passed'] else ''}")
    print(f"passed both periods: {result['passed']} of {result['tried']}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
