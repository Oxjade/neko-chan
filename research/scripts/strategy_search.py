"""Search for an entry rule that survives costs, and check it on data it never saw.

A small, fixed family of textbook rules (breakout, moving-average trend,
time-series momentum, two mean-reversion rules) is run over hourly and
4-hourly prices for five coins. Each rule is judged on the first 60% of the
history ("choose") and then on the last 40% ("check"). A rule only counts if
it is profitable after costs in BOTH parts.

Rules are fixed before looking at results; no parameter is tuned on the check
part. With this many variants a few will look good by luck, so the output
also reports how many passed out of how many were tried, and a t-statistic
for the check part.

Fills: signal on a bar's close, entry at the next bar's open. Stops and
targets are checked against each bar's high and low; a bar that touches both
counts as the stop. Not modelled: funding payments, liquidation, slippage
beyond the cost figure.

    python research/scripts/strategy_search.py            # both timeframes
    python research/scripts/strategy_search.py --interval 4h
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "research" / "exports" / "price_history"
OUT = REPO / "research" / "exports" / "tables" / "strategy_search.json"
COINS = ("BTC", "ETH", "SOL", "SUI", "HYPE")
SPLIT = 0.60

# Round-trip cost, percent of position size.
COSTS = {"venue_only": 0.13, "with_platform_fee": 1.13}


def load(coin: str, interval: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / f"{coin}_{interval}.csv", parse_dates=["t"])
    df.columns = [c.lower() for c in df.columns]
    return df.reset_index(drop=True)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


# ------------------------------------------------------------------ signals
# Each returns (long_entry, short_entry, long_exit, short_exit) boolean Series
# evaluated on bar closes, plus exit settings.

def donchian(df, n, bars_per_day):
    hi = df["high"].rolling(n).max().shift(1)
    lo = df["low"].rolling(n).min().shift(1)
    half_hi = df["high"].rolling(n // 2).max().shift(1)
    half_lo = df["low"].rolling(n // 2).min().shift(1)
    return (df["close"] > hi, df["close"] < lo, df["close"] < half_lo, df["close"] > half_hi,
            {"stop_atr": 3.0})


def ema_trend(df, fast, slow, bars_per_day):
    f = df["close"].ewm(span=fast, adjust=False).mean()
    s = df["close"].ewm(span=slow, adjust=False).mean()
    up = f > s
    return (up & ~up.shift(1, fill_value=False), ~up & up.shift(1, fill_value=True),
            ~up, up, {"stop_atr": 4.0})


def ts_momentum(df, days, bars_per_day):
    look = days * bars_per_day
    ret = df["close"] / df["close"].shift(look) - 1
    decide = (np.arange(len(df)) % bars_per_day) == 0     # once a day
    up = (ret > 0) & decide
    down = (ret < 0) & decide
    return (up, down, down, up, {"stop_atr": None})


def rsi_dip(df, level, bars_per_day):
    r = rsi(df["close"])
    trend = df["close"].ewm(span=200, adjust=False).mean()
    return (((r < level) & (df["close"] > trend)), ((r > 100 - level) & (df["close"] < trend)),
            r > 55, r < 45, {"stop_atr": 2.0, "take_atr": 2.0, "max_hold": 3 * bars_per_day})


def band_fade(df, width, bars_per_day):
    mid = df["close"].rolling(20).mean()
    sd = df["close"].rolling(20).std()
    return (df["close"] < mid - width * sd, df["close"] > mid + width * sd,
            df["close"] >= mid, df["close"] <= mid,
            {"stop_atr": 2.5, "max_hold": 2 * bars_per_day})


def variants(interval: str) -> list[tuple[str, callable, tuple]]:
    day = 24 if interval == "1h" else 6
    out = []
    for d in (2, 5, 10, 20):
        out.append((f"Breakout of the {d}-day high/low", donchian, (d * day,)))
    for f, s in ((1, 4), (2, 8), (5, 20), (10, 40)):
        out.append((f"Moving-average trend {f}d/{s}d", ema_trend, (f * day, s * day)))
    for d in (3, 7, 14, 30):
        out.append((f"Momentum over {d} days", ts_momentum, (d,)))
    for lvl in (20, 25, 30):
        out.append((f"Buy dips in an uptrend (RSI<{lvl})", rsi_dip, (lvl,)))
    for w in (2.0, 2.5, 3.0):
        out.append((f"Fade moves beyond {w} bands", band_fade, (w,)))
    return out


# ---------------------------------------------------------------- simulation

def simulate(df: pd.DataFrame, sig, long_only: bool) -> list[dict]:
    le, se, lx, sx, opts = sig
    le, se, lx, sx = (x.fillna(False).to_numpy(bool) for x in (le, se, lx, sx))
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    a = atr(df).to_numpy(float)
    stop_atr, take_atr, max_hold = opts.get("stop_atr"), opts.get("take_atr"), opts.get("max_hold")
    trades, pos = [], None
    for i in range(1, len(df)):
        if pos is not None:
            d = pos["dir"]
            exit_px = None
            if pos["stop"] is not None and (l[i] <= pos["stop"] if d > 0 else h[i] >= pos["stop"]):
                exit_px = min(o[i], pos["stop"]) if d > 0 else max(o[i], pos["stop"])  # gaps fill worse
            elif pos["take"] is not None and (h[i] >= pos["take"] if d > 0 else l[i] <= pos["take"]):
                exit_px = pos["take"]
            if exit_px is None:
                want_out = (lx[i - 1] if d > 0 else sx[i - 1]) or \
                           (max_hold is not None and i - pos["i"] >= max_hold)
                flip = (se[i - 1] if d > 0 else le[i - 1]) and not long_only
                if want_out or flip:
                    exit_px = o[i]
            if exit_px is not None:
                trades.append({"i": pos["i"], "exit_i": i, "dir": d,
                               "gross": (exit_px / pos["px"] - 1) * 100 * d})
                pos = None
        if pos is None and not math.isnan(a[i - 1]):
            d = 1 if le[i - 1] else (-1 if se[i - 1] and not long_only else 0)
            if d:
                px = o[i]
                pos = {"i": i, "dir": d, "px": px,
                       "stop": px - d * stop_atr * a[i - 1] if stop_atr else None,
                       "take": px + d * take_atr * a[i - 1] if take_atr else None}
    return trades


def stats(gross: list[float], cost: float) -> dict:
    n = len(gross)
    if not n:
        return {"trades": 0, "win_rate": None, "avg_net": None, "total_net": 0.0, "t": None}
    net = np.array(gross) - cost
    sd = net.std(ddof=1) if n > 1 else 0.0
    return {"trades": n, "win_rate": float((net > 0).mean()), "avg_net": float(net.mean()),
            "total_net": float(net.sum()),
            "t": float(net.mean() / (sd / math.sqrt(n))) if sd > 0 else None}


def run(interval: str) -> list[dict]:
    frames = {coin: load(coin, interval) for coin in COINS}
    day = 24 if interval == "1h" else 6
    rows = []
    for name, fn, args in variants(interval):
        for long_only in (False, True):
            choose, check = [], []
            for coin, df in frames.items():
                cut = int(len(df) * SPLIT)
                for t in simulate(df, fn(df, *args, day), long_only):
                    (choose if t["i"] < cut else check).append(t["gross"])
            row = {"interval": interval, "rule": name,
                   "sides": "long only" if long_only else "long and short"}
            for cname, cost in COSTS.items():
                row[cname] = {"choose": stats(choose, cost), "check": stats(check, cost)}
            rows.append(row)
    return rows


def passed(row: dict, cost: str) -> bool:
    a, b = row[cost]["choose"], row[cost]["check"]
    return bool(a["trades"] >= 20 and b["trades"] >= 20
                and a["avg_net"] > 0 and b["avg_net"] > 0)


def buy_and_hold(interval: str) -> dict:
    out = {}
    for coin in COINS:
        df = load(coin, interval)
        cut = int(len(df) * SPLIT)
        out[coin] = {"choose": float((df["close"][cut - 1] / df["close"][0] - 1) * 100),
                     "check": float((df["close"].iloc[-1] / df["close"][cut] - 1) * 100)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--interval", choices=("1h", "4h", "both"), default="both")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    intervals = ("1h", "4h") if args.interval == "both" else (args.interval,)

    rows = [r for iv in intervals for r in run(iv)]
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coins": COINS, "split": SPLIT, "costs_pct_round_trip": COSTS,
        "variants_tried": len(rows),
        "passed": {c: sum(passed(r, c) for r in rows) for c in COSTS},
        "buy_and_hold_pct": {iv: buy_and_hold(iv) for iv in intervals},
        "rows": rows,
    }
    Path(args.out).write_text(json.dumps(result, indent=1) + "\n")

    for cost in COSTS:
        print(f"\n=== cost {COSTS[cost]:.2f}% per round trip ({cost}) ===")
        print(f"{'tf':<3} {'rule':<36} {'sides':<15} {'choose: n  win   avg':>22} "
              f"{'check: n  win   avg    t':>27}  pass")
        ordered = sorted(rows, key=lambda r: -(r[cost]["choose"]["avg_net"] or -99))
        for r in ordered:
            a, b = r[cost]["choose"], r[cost]["check"]
            if not a["trades"] or not b["trades"]:
                continue
            print(f"{r['interval']:<3} {r['rule']:<36} {r['sides']:<15} "
                  f"{a['trades']:>9} {a['win_rate'] * 100:4.0f}% {a['avg_net']:+6.2f}% "
                  f"{b['trades']:>9} {b['win_rate'] * 100:4.0f}% {b['avg_net']:+6.2f}% "
                  f"{(b['t'] or 0):+5.1f}  {'YES' if passed(r, cost) else ''}")
        print(f"passed both parts: {result['passed'][cost]} of {len(rows)} variants")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
