"""Can a simple weekly rule trade gold better than just holding it?

Gold is tested the way forex was (research/scripts/forex_weekly_search.py):
a rule looks at the weekly closes so far, goes long, short or stays out for
the next week, and is judged on that week's move.

Data: weekly prices of PAXG, the gold-backed token, in US dollars from
Kraken's public API, 2020 to now. PAXG is what can actually be traded around
the clock (Hyperliquid lists it as a perpetual), and it tracks the gold price
closely. The first months of trading in 2019 are left out because the token
was too thinly traded for its weekly ranges to mean anything.

Each rule is run two ways: long or short every week, and long or in cash (a
"hold or sit out" version, since gold has risen over the period and shorting
it fights that). The yardstick is simply holding gold.

Costs: 0.045% of the position each time it changes (the exchange fee each
way). Funding on a perpetual is reported separately, because it depends on
how the trade is held (a spot holder pays none).

A rule passes only if, in BOTH halves of the history, it made money after
fees and its average week was at least as good as holding gold.

    python research/scripts/gold_weekly_search.py [--refresh]
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forex_weekly_search as fws  # noqa: E402

DATA = fws.REPO / "research" / "exports" / "price_history" / "gold_paxg_kraken_1w.csv"
OUT = fws.REPO / "research" / "exports" / "tables" / "gold_weekly_search.json"
KRAKEN = "https://api.kraken.com/0/public/OHLC"
START = "2020-01-01"
FEE_PER_SIDE = 0.045            # percent of the position, each time it changes
FUNDING_PER_WEEK = 0.21         # percent a week a leveraged long typically pays on a perpetual


def weekly(refresh: bool) -> pd.DataFrame:
    if refresh or not DATA.exists():
        rows = requests.get(KRAKEN, params={"pair": "PAXGUSD", "interval": 10080, "since": 0},
                            timeout=40).json()["result"]
        rows = next(v for k, v in rows.items() if k != "last")[:-1]          # drop the forming week
        pd.DataFrame([{"date": pd.Timestamp(int(r[0]), unit="s"), "open": float(r[1]), "high": float(r[2]),
                       "low": float(r[3]), "close": float(r[4])} for r in rows]).to_csv(DATA, index=False)
    frame = pd.read_csv(DATA, parse_dates=["date"]).set_index("date")
    return frame[frame.index >= START]


def judge(close: pd.Series, position: pd.Series, split) -> dict:
    """Positions decided at one weekly close, judged on the move to the next."""
    fwd = close.pct_change().shift(-1) * 100
    pos = position.where(fwd.notna(), 0.0).fillna(0.0)
    fees = FEE_PER_SIDE * pos.diff().abs().fillna(pos.abs())
    net = pos * fwd - fees
    out = {}
    for name, mask in (("first_half", close.index < split), ("second_half", close.index >= split),
                       ("all", close.index == close.index)):
        n, p, g = net[mask], pos[mask], (pos * fwd)[mask]
        active = p != 0
        weeks = int(active.sum())
        sd = n[active].std(ddof=1) if weeks > 1 else 0.0
        out[name] = {
            "weeks_in_market": weeks, "weeks": int(mask.sum()),
            "right": float((g[active] > 0).mean()) if weeks else None,
            "avg_week_pct": float(n.mean()),                      # over every week, in or out
            "total_pct": float(n.sum()),
            "t": float(n[active].mean() / (sd / math.sqrt(weeks))) if weeks > 1 and sd > 0 else None,
            "long_weeks": int((p > 0).sum()), "short_weeks": int((p < 0).sum()),
        }
    return out


def main() -> int:
    frame = weekly("--refresh" in sys.argv)
    close = frame["close"]
    panel = close.to_frame("GOLD")
    split = close.index[len(close) // 2]
    hold = judge(close, pd.Series(1.0, index=close.index), split)
    rows = [{"rule": "Always hold gold", "style": "hold", **hold}]
    for name, fn in fws.rules():
        both = fn(panel)["GOLD"]
        rows.append({"rule": name, "style": "long or short", **judge(close, both, split)})
        rows.append({"rule": name, "style": "long or cash", **judge(close, both.clip(lower=0.0), split)})

    def passes(r) -> bool:
        return all(r[h]["total_pct"] > 0 and r[h]["avg_week_pct"] >= hold[h]["avg_week_pct"]
                   for h in ("first_half", "second_half"))

    years = (close.index[-1] - close.index[0]).days / 365.25
    print(f"gold (PAXG/USD weekly, Kraken) {close.index[0].date()} to {close.index[-1].date()}: "
          f"{len(close)} weeks, {close.iloc[0]:.0f} -> {close.iloc[-1]:.0f} "
          f"({(close.iloc[-1] / close.iloc[0] - 1) * 100:+.0f}% in {years:.1f} years); halves split at {split.date()}")
    print(f"{'rule':<42} {'style':<14} {'in mkt':>6} {'right':>6} {'avg wk':>7} {'total':>7} "
          f"{'1st half':>9} {'2nd half':>9}  passes")
    for r in sorted(rows, key=lambda r: -r["all"]["total_pct"]):
        a = r["all"]
        r["passes"] = bool(r["style"] != "hold" and passes(r))
        print(f"{r['rule']:<42} {r['style']:<14} {a['weeks_in_market']:>6} "
              f"{(a['right'] or 0) * 100:>5.0f}% {a['avg_week_pct']:>+6.2f}% {a['total_pct']:>+6.0f}% "
              f"{r['first_half']['total_pct']:>+8.0f}% {r['second_half']['total_pct']:>+8.0f}%  "
              f"{'YES' if r['passes'] else ''}")
    passed = [r["rule"] + " (" + r["style"] + ")" for r in rows if r.get("passes")]
    print(f"\npassed: {len(passed)} of {len(rows) - 1}", passed)
    print(f"holding gold as a perpetual would also pay about {FUNDING_PER_WEEK}% a week in funding "
          f"({FUNDING_PER_WEEK * 52:.0f}% a year), against an average week of {hold['all']['avg_week_pct']:+.2f}%")
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Kraken PAXGUSD weekly", "from": str(close.index[0].date()), "to": str(close.index[-1].date()),
        "weeks": len(close), "split": str(split.date()), "fee_per_side_pct": FEE_PER_SIDE,
        "funding_per_week_pct": FUNDING_PER_WEEK, "hold": hold, "tried": len(rows) - 1,
        "passed": passed, "rows": rows}, indent=1) + "\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
