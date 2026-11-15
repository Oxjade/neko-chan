"""Weekly crypto rules, including sentiment, tested on six years of daily prices.

Same question as the forex test: if you plan one trade per coin at the
weekend and hold it for the week, does any simple rule pick the direction
better than a coin flip? Two kinds of rule are tried:

  price      follow the trend, fade the last move, breakouts, moving averages
  sentiment  the Crypto Fear & Greed index (market-wide mood), and funding
             rates (whether leveraged traders are crowded long or short)

Crypto has risen over the period, so "always long" is shown as the yardstick:
a rule is only interesting if it does better than simply holding.

Each rule is judged on the first half of the history ("choose") and the
second half ("check"), without costs and with them.

Data: Hyperliquid daily candles (Aug 2020 on) for ten coins, the Fear & Greed
index from alternative.me, Hyperliquid funding history (May 2023 on) for BTC,
ETH and SOL. Weeks run Monday 00:00 UTC to Monday 00:00 UTC.

    python research/scripts/crypto_weekly_search.py
    python research/scripts/crypto_weekly_search.py --refresh
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
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forex_weekly_search as fws  # noqa: E402

DATA = fws.REPO / "research" / "exports" / "price_history"
OUT = fws.REPO / "research" / "exports" / "tables" / "crypto_weekly_search.json"
HL = "https://api.hyperliquid.xyz/info"
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "AVAX", "LINK", "LTC", "ADA")
FUNDING_COINS = ("BTC", "ETH", "SOL")
COST_PCT = 0.13


def _post(payload: dict):
    for attempt in range(6):
        resp = requests.post(HL, json=payload, timeout=30)
        if resp.status_code == 429:
            time.sleep(2 + attempt * 2)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Hyperliquid kept rate-limiting the request")


def daily_closes(refresh: bool) -> pd.DataFrame:
    path = DATA / "crypto_1d_closes.csv"
    if refresh or not path.exists():
        now = int(time.time() * 1000)
        cols = {}
        for coin in COINS:
            rows = _post({"type": "candleSnapshot", "req": {
                "coin": coin, "interval": "1d", "startTime": 0, "endTime": now}})
            cols[coin] = pd.Series({pd.Timestamp(int(r["t"]), unit="ms"): float(r["c"])
                                    for r in rows[:-1]})        # drop the forming day
            time.sleep(0.2)
        pd.DataFrame(cols).sort_index().to_csv(path, index_label="date")
    return pd.read_csv(path, parse_dates=["date"], index_col="date")


def fear_greed(refresh: bool) -> pd.Series:
    path = DATA / "crypto_fear_greed.csv"
    if refresh or not path.exists():
        data = requests.get("https://api.alternative.me/fng/?limit=0&format=json", timeout=30).json()["data"]
        s = pd.Series({pd.Timestamp(int(d["timestamp"]), unit="s"): int(d["value"]) for d in data})
        s.sort_index().to_csv(path, index_label="date", header=["value"])
    return pd.read_csv(path, parse_dates=["date"], index_col="date")["value"]


def funding(refresh: bool) -> pd.DataFrame:
    """Average hourly funding rate per day, per coin."""
    path = DATA / "crypto_funding_daily.csv"
    if refresh or not path.exists():
        cols = {}
        for coin in FUNDING_COINS:
            rows, start = [], 0
            while True:
                page = _post({"type": "fundingHistory", "coin": coin, "startTime": start})
                if not page:
                    break
                rows += page
                if len(page) < 500:
                    break
                start = page[-1]["time"] + 1
                time.sleep(0.15)
            s = pd.Series({pd.Timestamp(r["time"], unit="ms"): float(r["fundingRate"]) for r in rows})
            cols[coin] = s.resample("1D").mean()
        pd.DataFrame(cols).to_csv(path, index_label="date")
    return pd.read_csv(path, parse_dates=["date"], index_col="date")


def evaluate(close: pd.DataFrame, position: pd.DataFrame, cost: float, split) -> dict:
    fwd = close.pct_change().shift(-1) * 100
    pos = position.reindex_like(fwd).where(fwd.notna(), 0.0).fillna(0.0)
    gross = pos * fwd
    net = gross - cost * (pos != 0)
    out = {}
    for name, mask in (("choose", close.index < split), ("check", close.index >= split)):
        g, n_, p = gross[mask], net[mask], pos[mask]
        active = p != 0
        trades = int(active.values.sum())
        decided = int(((g != 0) & active).values.sum())
        weekly = n_.where(active).mean(axis=1).dropna()
        sd = weekly.std(ddof=1)
        out[name] = {
            "coin_weeks": trades,
            "accuracy": int(((g > 0) & active).values.sum()) / decided if decided else None,
            "avg_pct": float(n_.where(active).stack().mean()) if trades else None,
            "t": float(weekly.mean() / (sd / math.sqrt(len(weekly)))) if sd and sd > 0 else None,
        }
    return out


def same_for_all(series: pd.Series, close: pd.DataFrame) -> pd.DataFrame:
    """A market-wide signal applied to every coin."""
    s = series.reindex(close.index)
    return pd.DataFrame({coin: s for coin in close.columns})


def mood_rules(mood: pd.Series, close: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    weekly = mood.resample("W-SUN").last()                 # the reading on the week's last day
    change = weekly.diff()
    extreme = pd.Series(0.0, index=weekly.index)
    extreme[weekly < 25] = 1.0
    extreme[weekly > 75] = -1.0
    return [
        ("Buy extreme fear, sell extreme greed", same_for_all(extreme, close)),
        ("Sell extreme fear, buy extreme greed", same_for_all(-extreme, close)),
        ("Long when greedy, short when fearful", same_for_all(np.sign(weekly - 50), close)),
        ("Long when fearful, short when greedy", same_for_all(-np.sign(weekly - 50), close)),
        ("Follow the week's change in mood", same_for_all(np.sign(change), close)),
        ("Fade the week's change in mood", same_for_all(-np.sign(change), close)),
    ]


def funding_rules(fund: pd.DataFrame, close: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    weekly = fund.rolling(7).mean().resample("W-SUN").last().reindex(close.index)
    lo = weekly.rolling(52, min_periods=26).min()
    hi = weekly.rolling(52, min_periods=26).max()
    rank = (weekly - lo) / (hi - lo)
    crowded = pd.DataFrame(np.where(rank > 0.9, -1.0, np.where(rank < 0.1, 1.0, 0.0)),
                           index=weekly.index, columns=weekly.columns)
    empty = pd.DataFrame(0.0, index=close.index, columns=close.columns)

    def widen(frame):
        out = empty.copy()
        out[frame.columns] = frame.fillna(0.0)
        return out
    return [
        ("Fade crowded funding (1-year extreme)", widen(crowded)),
        ("Trade with the funding sign", widen(np.sign(weekly))),
        ("Trade against the funding sign", widen(-np.sign(weekly))),
    ]


def main() -> int:
    refresh = "--refresh" in sys.argv
    daily = daily_closes(refresh)
    close = daily.resample("W-SUN").last().dropna(how="all")
    split = close.index[len(close) // 2]
    groups = [("yardstick", [("Always long", pd.DataFrame(1.0, index=close.index, columns=close.columns))])]
    groups.append(("price", [(name, fn(close)) for name, fn in fws.rules()]))
    groups.append(("mood", mood_rules(fear_greed(refresh), close)))
    groups.append(("funding", funding_rules(funding(refresh), close)))

    rows = []
    for kind, items in groups:
        for name, position in items:
            rows.append({"kind": kind, "rule": name,
                         "no_costs": evaluate(close, position, 0.0, split),
                         "with_costs": evaluate(close, position, COST_PCT, split)})
    base = rows[0]["no_costs"]
    for r in rows[1:]:
        a, b = r["no_costs"]["choose"], r["no_costs"]["check"]
        r["beats_holding"] = bool(
            a["avg_pct"] is not None and b["avg_pct"] is not None
            and a["avg_pct"] > base["choose"]["avg_pct"] and b["avg_pct"] > base["check"]["avg_pct"]
            and a["accuracy"] > 0.5 and b["accuracy"] > 0.5)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coins": list(COINS), "funding_coins": list(FUNDING_COINS),
        "from": str(close.index[0].date()), "to": str(close.index[-1].date()),
        "split": str(split.date()), "cost_pct": COST_PCT,
        "beat_holding": sum(r.get("beats_holding", False) for r in rows), "tried": len(rows) - 1,
        "rows": rows,
    }
    OUT.write_text(json.dumps(result, indent=1) + "\n")

    print(f"{len(COINS)} coins, weekly, {result['from']} to {result['to']}, halves split at {result['split']}")
    print(f"{'rule (no costs)':<42} {'first half: weeks right    avg     t':>38} "
          f"{'second half: weeks right    avg     t':>38}  beats holding")
    for r in rows:
        a, b = r["no_costs"]["choose"], r["no_costs"]["check"]
        if not a["coin_weeks"] and not b["coin_weeks"]:
            continue

        def cell(s):
            if not s["coin_weeks"]:
                return f"{'no data':>38}"
            return f"{s['coin_weeks']:>16} {s['accuracy'] * 100:5.1f}% {s['avg_pct']:+6.2f}% {s['t'] or 0:+5.1f}"
        print(f"{r['rule']:<42} {cell(a)} {cell(b)}  {'YES' if r.get('beats_holding') else ''}")
    print(f"beat holding in both halves: {result['beat_holding']} of {result['tried']}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
