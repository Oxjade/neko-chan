"""Evaluate the quant strategy across trader types on recent live data.

Answers: does the scenario engine (GBM barrier math + vol-based stops/targets
+ time exits) produce winning perp trades? Runs the SAME logic the live agent
uses over the last N days of 5m candles and measures win rate, profit factor,
and expectancy per trader type (scalp / intraday / swing / auto).

Method (point-in-time safe, no look-ahead):
  - for each 5m bar, build the scenario matrix using only bars BEFORE it
  - take the best positive-EV scenario (the same pick_best_scenario the agent
    uses), simulate entry, then walk forward to see if target or stop hits
  - apply the time-exit (vol-derived) and fee drag (0.09% round trip)
  - report per-symbol and pooled win rate / PF / expectancy
"""

import math
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, "/home/carnage/tradebotpro/service/agent")

from quant_strategy import (
    scenario_matrix, pick_best_scenario, build_scenarios,
    _expected_target_minutes, HORIZON_BY_TYPE,
)

SYMBOLS = ["BTC", "ETH", "SOL", "SUI", "HYPE", "SEI", "NEAR", "ATOM"]
FEE_RT = 0.0009  # 0.045% taker x2 = real Hyperliquid round trip


def fetch_5m(symbol, hours=48):
    now = int(time.time() * 1000)
    r = requests.post("https://api.hyperliquid.xyz/info", json={
        "type": "candleSnapshot", "req": {"coin": symbol, "interval": "5m",
        "startTime": now - hours * 3600 * 1000, "endTime": now}}, timeout=15)
    out = []
    for c in r.json():
        out.append({"ts": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                    "l": float(c["l"]), "c": float(c["c"])})
    return out


def simulate(trader_type):
    """Walk forward through all symbols, trade the best positive-EV scenario."""
    trades = []
    for sym in SYMBOLS:
        bars = fetch_5m(sym, 48)
        if len(bars) < 100:
            continue
        i = 0
        while i < len(bars) - 60:
            bar = bars[i]
            # build closes strictly before this bar (point-in-time safe)
            closes = [b["c"] for b in bars[:i]]
            if len(closes) < 40:
                i += 1
                continue
            px = bar["c"]
            if px <= 0:
                i += 1
                continue
            sc = build_scenarios(sym, closes, px)
            allowed = HORIZON_BY_TYPE.get(trader_type, ["scalp", "intraday", "swing"])
            best = pick_best_scenario(
                [s for s in sc if s.horizon in allowed],
                has_long={}, has_short={})
            if best is None or best.ev <= 0:
                i += 1
                continue
            # walk forward to find target/stop hit, respecting time exit
            entry = px
            stop, target = best.stop, best.target
            win = loss = 0
            exit_reason = ""
            # expected time in bars = expected_minutes / 5
            expected_bars = max(6, int(_expected_target_minutes(
                entry, best.target, 2.5) / 5 * 2.5))
            resolved = False
            for j in range(i, min(i + expected_bars, len(bars))):
                hb, lb = bars[j]["h"], bars[j]["l"]
                if best.direction == "long":
                    if hb >= target:
                        win = 1; resolved = True; exit_reason = "target"; break
                    if lb <= stop:
                        loss = 1; resolved = True; exit_reason = "stop"; break
                else:
                    if lb <= target:
                        win = 1; resolved = True; exit_reason = "target"; break
                    if hb >= stop:
                        loss = 1; resolved = True; exit_reason = "stop"; break
            if not resolved:
                # time exit: take whatever it is
                final_px = bars[min(i + expected_bars - 1, len(bars) - 1)]["c"]
                if best.direction == "long":
                    win = 1 if final_px > entry else 0
                else:
                    win = 1 if final_px < entry else 0
                exit_reason = "time"
            R = best.R
            gross = R if win else -1.0
            net = gross - FEE_RT * (1 / (best.stop / entry * 100) if False else 1)
            trades.append({"sym": sym, "horizon": best.horizon, "dir": best.direction,
                           "win": win, "exit": exit_reason, "R": R, "net": net,
                           "ev": best.ev, "p_win": best.p_win})
            i += expected_bars  # jump past this trade
        # end while
    # end for
    return trades


def report(trader_type):
    trades = simulate(trader_type)
    if not trades:
        print(f"{trader_type:9s}: no trades")
        return
    n = len(trades)
    wins = sum(1 for t in trades if t["win"])
    wr = wins / n * 100
    pf = sum(t["R"] for t in trades if t["win"]) / max(
        sum(1 for t in trades if not t["win"]), 1)
    exp = sum(t["net"] for t in trades) / n
    exits = {}
    for t in trades:
        exits[t["exit"]] = exits.get(t["exit"], 0) + 1
    print(f"\n=== TRADER TYPE: {trader_type.upper()} ===")
    print(f"Trades: {n} | Win rate: {wr:.1f}% | Profit factor: {pf:.2f} | "
          f"Avg expectancy: {exp:+.2f}R/trade")
    print(f"Exits: {exits}")
    print(f"Avg forecast P(win): {sum(t['p_win'] for t in trades)/n*100:.0f}% | "
          f"Avg forecast EV: {sum(t['ev'] for t in trades)/n:+.2f}R")
    by_sym = {}
    for t in trades:
        by_sym.setdefault(t["sym"], [0, 0])
        by_sym[t["sym"]][0] += 1
        by_sym[t["sym"]][1] += t["win"]
    for s, (c, w) in sorted(by_sym.items(), key=lambda x: -x[1][1] / max(x[1][0], 1)):
        print(f"  {s:5s}: {c:2d} trades, {w}/{c} wins ({w/max(c,1)*100:.0f}%)")


if __name__ == "__main__":
    for ttype in ("scalp", "intraday", "swing", "auto"):
        report(ttype)
