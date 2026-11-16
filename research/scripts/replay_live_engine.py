"""Replay the scenario engine that the live agent actually runs, with real costs.

Why this exists: the other backtests in this folder test the daily momentum20
rule, which the live loop never calls. This script walks the real
`scenario_matrix` + RSI/EMA entry gates + `pick_best_scenario` over 5-minute
bars and resolves every entry against the stop and the target on later bars.

It answers three questions, with sample sizes and confidence intervals:
  1. How often does the engine's pick hit its target before its stop?
  2. What win rate does it need to break even at each cost level?
  3. Is it better than entering at the same moments in a random direction?

Deliberately conservative:
  - when a bar touches both the stop and the target, the stop is counted;
  - the decision uses closes up to bar i and fills at the close of bar i;
  - a trade still open at the end of the data is marked to the last close.

Not modelled: funding, liquidation, stop slippage, the LLM's choice among the
top rows (the engine's own highest-conviction pick is used instead).

Offline. Reads research/exports/agent_price_cache/CRYPTO_*_5m.csv.

    python research/scripts/replay_live_engine.py
    python research/scripts/replay_live_engine.py --csv BTC=path.csv --csv SOL=path.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "service" / "agent"))

from quant_strategy import (  # noqa: E402
    RSI_ENTRY_THRESHOLD,
    momentum_confirmed,
    pick_best_scenario,
    rsi,
    scenario_matrix,
)

CACHE = REPO / "research" / "exports" / "agent_price_cache"
OUT = REPO / "research" / "exports" / "tables" / "live_engine_replay.json"

BARS_PER_YEAR = 288 * 365.0
WINDOW = 72               # 6h of 5m closes, as the live loop feeds the engine
DECIDE_EVERY = 3          # one decision per 15 min (the live LLM cooldown)
CONVICTION_FLOOR = 0.0008  # live_agent.CONVICTION_FLOOR
MAX_DAILY_TRADES = 8      # "balanced" preset

# Round-trip cost as a percent of notional.
VENUE_RT = 2 * 0.045 + 0.04           # Aftermath taker both legs + limit offsets
PLATFORM_RT = 2 * 0.50                # 0.5% platform fee on each fill
COST_LEVELS = {
    "gross": 0.0,
    "venue_only": VENUE_RT,
    "with_platform_fee": VENUE_RT + PLATFORM_RT,
}


def load(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "t": r["t"],
                "high": float(r["High"]),
                "low": float(r["Low"]),
                "close": float(r["Close"]),
            })
    return rows


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def resolve(bars: list[dict], i: int, direction: str, stop_pct: float,
            take_pct: float) -> tuple[str, float, int]:
    """Walk forward from bar i. Returns (outcome, gross move % in our favour, exit index)."""
    entry = bars[i]["close"]
    if direction == "long":
        stop, target = entry * (1 - stop_pct / 100), entry * (1 + take_pct / 100)
    else:
        stop, target = entry * (1 + stop_pct / 100), entry * (1 - take_pct / 100)
    for j in range(i + 1, len(bars)):
        hi, lo = bars[j]["high"], bars[j]["low"]
        hit_stop = lo <= stop if direction == "long" else hi >= stop
        hit_target = hi >= target if direction == "long" else lo <= target
        if hit_stop:                      # stop wins a same-bar tie
            return ("loss", -stop_pct, j)
        if hit_target:
            return ("win", take_pct, j)
    last = bars[-1]["close"]
    move = (last / entry - 1) * 100 * (1 if direction == "long" else -1)
    return ("open", move, len(bars) - 1)


def engine_pick(closes_by_symbol: dict, prices: dict, blocked: set):
    matrix = scenario_matrix(closes_by_symbol, prices, trader_type="scalp",
                             bars_per_year=BARS_PER_YEAR)
    keep = []
    for s in matrix:
        if s.symbol in blocked:
            continue
        closes = closes_by_symbol[s.symbol]
        r = rsi(closes)
        if s.direction == "long" and r < RSI_ENTRY_THRESHOLD:
            continue
        if s.direction == "short" and r > 100 - RSI_ENTRY_THRESHOLD:
            continue
        if not momentum_confirmed(closes, s.direction):
            continue
        keep.append(s)
    return pick_best_scenario(keep, {}, {}, conviction_floor=CONVICTION_FLOOR)


def replay(series: dict[str, list[dict]]) -> list[dict]:
    """One position at a time across symbols, one entry per symbol per UTC day."""
    idx = {sym: {b["t"]: k for k, b in enumerate(bars)} for sym, bars in series.items()}
    ref = max(series, key=lambda s: len(series[s]))
    times = [b["t"] for b in series[ref]]
    trades: list[dict] = []
    busy_until = ""                       # timestamp the open trade exits at
    day_count: dict[str, int] = {}
    day_syms: dict[str, set] = {}
    for n, t in enumerate(times):
        if n < WINDOW or n % DECIDE_EVERY or t <= busy_until:
            continue
        day = t[:10]
        if day_count.get(day, 0) >= MAX_DAILY_TRADES:
            continue
        closes_by, prices = {}, {}
        for sym, bars in series.items():
            k = idx[sym].get(t)
            if k is None or k < WINDOW:
                continue
            closes_by[sym] = [b["close"] for b in bars[k - WINDOW + 1:k + 1]]
            prices[sym] = bars[k]["close"]
        if not closes_by:
            continue
        pick = engine_pick(closes_by, prices, day_syms.get(day, set()))
        if pick is None:
            continue
        bars = series[pick.symbol]
        k = idx[pick.symbol][t]
        stop_pct = abs(pick.stop / pick.entry - 1) * 100
        take_pct = abs(pick.target / pick.entry - 1) * 100
        outcome, move, j = resolve(bars, k, pick.direction, stop_pct, take_pct)
        trades.append({
            "t": t, "symbol": pick.symbol, "direction": pick.direction,
            "entry": pick.entry, "stop_pct": round(stop_pct, 4),
            "take_pct": round(take_pct, 4), "p_win_model": round(pick.p_win, 4),
            "outcome": outcome, "gross_pct": round(move, 4),
            "exit_t": bars[j]["t"], "bars_held": j - k, "_k": k,
        })
        busy_until = bars[j]["t"]
        day_count[day] = day_count.get(day, 0) + 1
        day_syms.setdefault(day, set()).add(pick.symbol)
    return trades


def summarise(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["outcome"] != "open"]
    n = len(closed)
    wins = sum(1 for t in closed if t["outcome"] == "win")
    lo, hi = wilson(wins, n)
    stop = sum(t["stop_pct"] for t in closed) / n if n else 0.0
    take = sum(t["take_pct"] for t in closed) / n if n else 0.0
    out = {
        "trades_closed": n,
        "trades_open_at_end": len(trades) - n,
        "wins": wins,
        "win_rate": wins / n if n else None,
        "win_rate_ci95": [lo, hi],
        "avg_stop_pct": stop,
        "avg_take_pct": take,
        "avg_model_p_win": sum(t["p_win_model"] for t in closed) / n if n else None,
        "avg_hold_hours": sum(t["bars_held"] for t in closed) * 5 / 60 / n if n else None,
        "by_cost": {},
    }
    for name, cost in COST_LEVELS.items():
        nets = [t["gross_pct"] - cost for t in closed]
        out["by_cost"][name] = {
            "round_trip_cost_pct": cost,
            "breakeven_win_rate": (stop + cost) / (stop + take) if n else None,
            "avg_net_pct_of_notional": sum(nets) / n if n else None,
            "total_net_pct_of_notional": sum(nets),
        }
    return out


def random_direction_baseline(series: dict, trades: list[dict], seeds: int = 500) -> dict:
    """Same entry bars, stops and targets; direction chosen by coin flip."""
    closed = [t for t in trades if t["outcome"] != "open"]
    if not closed:
        return {"seeds": seeds, "mean_win_rate": None}
    rates = []
    for seed in range(seeds):
        rng = random.Random(seed)
        w = n = 0
        for t in closed:
            d = rng.choice(("long", "short"))
            o, _, _ = resolve(series[t["symbol"]], t["_k"], d, t["stop_pct"], t["take_pct"])
            if o == "open":
                continue
            n += 1
            w += o == "win"
        if n:
            rates.append(w / n)
    rates.sort()
    actual = sum(1 for t in closed if t["outcome"] == "win") / len(closed)
    return {
        "seeds": seeds,
        "mean_win_rate": sum(rates) / len(rates),
        "p05": rates[int(0.05 * len(rates))],
        "p95": rates[int(0.95 * len(rates)) - 1],
        "share_of_random_runs_at_or_above_engine": sum(r >= actual for r in rates) / len(rates),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv", action="append", default=[],
                    help="SYMBOL=path to a 5m OHLC csv (t,Open,High,Low,Close); repeatable")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    if args.csv:
        sources = dict(a.split("=", 1) for a in args.csv)
    else:
        sources = {p.stem.split("_")[1]: str(p) for p in sorted(CACHE.glob("CRYPTO_*_5m.csv"))}
    series = {sym: load(Path(p)) for sym, p in sources.items()}
    if not series:
        print("no price data found", file=sys.stderr)
        return 1

    trades = replay(series)
    summary = summarise(trades)
    baseline = random_direction_baseline(series, trades)
    for t in trades:
        t.pop("_k", None)
    first = min(b[0]["t"] for b in series.values())
    last = max(b[-1]["t"] for b in series.values())
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "scenario_matrix(scalp) + RSI gate + EMA8/21 gate + pick_best_scenario",
        "data": {"symbols": sorted(series), "bar": "5m", "from": first, "to": last,
                 "bars": {s: len(b) for s, b in series.items()}},
        "assumptions": {
            "same_bar_tie": "stop counted first",
            "one_position_at_a_time": True,
            "one_entry_per_symbol_per_day": True,
            "max_daily_trades": MAX_DAILY_TRADES,
            "not_modelled": ["funding", "liquidation", "stop slippage", "LLM choice among top rows"],
        },
        "summary": summary,
        "random_direction_baseline": baseline,
        "trades": trades,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")

    s = summary
    print(f"data      {first}  ->  {last}   {', '.join(sorted(series))}")
    print(f"trades    {s['trades_closed']} closed, {s['trades_open_at_end']} open at end")
    if s["trades_closed"]:
        print(f"win rate  {s['win_rate']*100:.1f}%   95% CI {s['win_rate_ci95'][0]*100:.1f}-"
              f"{s['win_rate_ci95'][1]*100:.1f}%   (model predicted {s['avg_model_p_win']*100:.1f}%)")
        print(f"levels    stop {s['avg_stop_pct']:.2f}%  target {s['avg_take_pct']:.2f}%  "
              f"avg hold {s['avg_hold_hours']:.1f}h")
        for name, c in s["by_cost"].items():
            print(f"  {name:<18} cost {c['round_trip_cost_pct']:.2f}%  break-even "
                  f"{c['breakeven_win_rate']*100:.1f}%  avg net {c['avg_net_pct_of_notional']:+.2f}% "
                  f"per trade  total {c['total_net_pct_of_notional']:+.1f}%")
        print(f"random    coin-flip direction at the same entries wins "
              f"{baseline['mean_win_rate']*100:.1f}% on average "
              f"(5th-95th pct {baseline['p05']*100:.1f}-{baseline['p95']*100:.1f}%)")
    print(f"wrote     {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
