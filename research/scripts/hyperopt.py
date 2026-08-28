"""Hyperopt: optimize stop/target multipliers, RSI filter, time-decay exits.

Runs the scenario engine over 90 days of 5m data, testing parameter combinations
to find the highest profit factor. Reports per-symbol optimal params + combined PF.

Parameters tested:
  - stop_mult: 0.8 to 3.0 (× sigma)  [current: 1.2]
  - target_mult: 2.0 to 8.0 (× sigma) [current: 4.0]
  - rsi_threshold: 0 (off) to 60       [current: off]
  - time_decay: 0 (off) or 1 (on)      [current: off]
"""

import math
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, "/home/carnage/tradebotpro/service/agent")

from quant_strategy import (
    build_scenarios, pick_best_scenario, HORIZON_BY_TYPE,
    _expected_target_minutes, estimate_drift_vol,
)

SYMBOLS = ["BTC", "ETH", "SOL", "SUI", "HYPE", "SEI", "NEAR", "ATOM"]
FEE_RT = 0.0009
HOURS = 90 * 24  # 90 days of 5m data

# Grid search space
STOP_MULTS = [0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]
TARGET_MULTS = [2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]
RSI_THRESHOLDS = [0, 30, 40, 50, 60]
TIME_DECAYS = [0, 1]


def fetch_5m(symbol, hours=HOURS):
    now = int(time.time() * 1000)
    r = requests.post("https://api.hyperliquid.xyz/info", json={
        "type": "candleSnapshot", "req": {"coin": symbol, "interval": "5m",
        "startTime": now - hours * 3600 * 1000, "endTime": now}}, timeout=15)
    return [{"ts": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
             "l": float(c["l"]), "c": float(c["c"])} for c in r.json()]


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def simulate(symbol, bars, stop_mult, target_mult, rsi_thresh, time_decay):
    """Run one parameter combo on one symbol. Returns (trades, wins, profit_factor)."""
    trades = []
    i = 0
    while i < len(bars) - 40:
        bar = bars[i]
        closes = [b["c"] for b in bars[:i]]
        if len(closes) < 40:
            i += 1
            continue
        px = bar["c"]
        if px <= 0:
            i += 1
            continue
        # RSI filter
        if rsi_thresh > 0:
            r = rsi(closes)
            if r < rsi_thresh:
                i += 1
                continue
        # Build scenario with custom stop/target multipliers
        drift, vol = estimate_drift_vol(closes)
        daily_vol = (vol / math.sqrt(365.0)) * 100.0 if vol > 0 else 1.0
        sigma_5m = daily_vol / math.sqrt(288.0)
        stop_pct = max(0.12, min(0.6, sigma_5m * stop_mult))
        take_pct = max(0.25, min(1.5, sigma_5m * target_mult))
        if take_pct / stop_pct < 1.5:
            take_pct = stop_pct * 1.5
        sc = build_scenarios(symbol, closes, px, stop_pct=stop_pct, take_pct=take_pct)
        allowed = HORIZON_BY_TYPE.get("auto", ["scalp", "intraday", "swing"])
        best = pick_best_scenario(
            [s for s in sc if s.horizon in allowed],
            has_long={}, has_short={})
        if best is None or best.ev <= 0:
            i += 1
            continue
        entry = px
        stop, target = best.stop, best.target
        win = loss = 0
        resolved = False
        expected_bars = max(6, int(_expected_target_minutes(entry, best.target, 2.5) / 5 * 2.5))
        # time decay: ratchet down the target as time passes
        decay_target = target
        if time_decay:
            pass  # use the bar loop below
        for j in range(i, min(i + expected_bars, len(bars))):
            hb, lb = bars[j]["h"], bars[j]["l"]
            if time_decay:
                # reduce target by 30% every 1/3 of expected time
                elapsed = j - i
                if elapsed > expected_bars * 0.66:
                    decay_target = entry + (target - entry) * 0.5
                elif elapsed > expected_bars * 0.33:
                    decay_target = entry + (target - entry) * 0.75
            if best.direction == "long":
                if hb >= decay_target:
                    win = 1; resolved = True; break
                if lb <= stop:
                    loss = 1; resolved = True; break
            else:
                if lb <= decay_target:
                    win = 1; resolved = True; break
                if hb >= stop:
                    loss = 1; resolved = True; break
        if not resolved:
            final_px = bars[min(i + expected_bars - 1, len(bars) - 1)]["c"]
            if best.direction == "long":
                win = 1 if final_px > entry else 0
            else:
                win = 1 if final_px < entry else 0
        R = take_pct / stop_pct
        gross = R if win else -1.0
        net = gross - FEE_RT * 2
        trades.append({"win": win, "R": R, "net": net})
        i += expected_bars
    return trades


def score(trades):
    if not trades:
        return 0, 0, 0.0
    n = len(trades)
    wins = sum(1 for t in trades if t["win"])
    wr = wins / n
    gross_profit = sum(t["R"] for t in trades if t["win"])
    gross_loss = sum(1 for t in trades if not t["win"])
    pf = gross_profit / max(gross_loss, 1)
    exp = sum(t["net"] for t in trades) / n
    return wr, pf, exp


def main():
    print("=== HYPEROPT: optimizing stop/target/RSI/time-decay ===")
    print(f"Symbols: {SYMBOLS}")
    print(f"Parameter grid: {len(STOP_MULTS)} stop × {len(TARGET_MULTS)} target × "
          f"{len(RSI_THRESHOLDS)} RSI × {len(TIME_DECAYS)} decay = "
          f"{len(STOP_MULTS)*len(TARGET_MULTS)*len(RSI_THRESHOLDS)*len(TIME_DECAYS)} combos\n")
    
    all_data = {}
    for sym in SYMBOLS:
        print(f"  Fetching {sym}...", end=" ", flush=True)
        all_data[sym] = fetch_5m(sym)
        print(f"{len(all_data[sym])} bars")

    best_overall = {"pf": 0.0, "combo": None}
    per_symbol_best = {}

    total_combos = len(STOP_MULTS) * len(TARGET_MULTS) * len(RSI_THRESHOLDS) * len(TIME_DECAYS)
    combo = 0

    for stop_m in STOP_MULTS:
        for target_m in TARGET_MULTS:
            for rsi_t in RSI_THRESHOLDS:
                for decay in TIME_DECAYS:
                    combo += 1
                    all_trades = []
                    sym_metrics = {}
                    for sym in SYMBOLS:
                        t = simulate(sym, all_data[sym], stop_m, target_m, rsi_t, decay)
                        all_trades.extend(t)
                        sym_metrics[sym] = score(t)
                    wr, pf, exp = score(all_trades)
                    if pf > best_overall["pf"]:
                        best_overall["pf"] = pf
                        best_overall["combo"] = (stop_m, target_m, rsi_t, decay, wr, pf, exp, sym_metrics)
                    if combo % 10 == 0 or combo == 1:
                        print(f"  [{combo}/{total_combos}] best PF so far: {best_overall['pf']:.3f} "
                              f"(stop={best_overall['combo'][0]}, target={best_overall['combo'][1]}, "
                              f"RSI={best_overall['combo'][2]}, decay={best_overall['combo'][3]})")

    print("\n" + "=" * 60)
    print("BEST OVERALL RESULT")
    print("=" * 60)
    c = best_overall["combo"]
    print(f"Stop mult: {c[0]} | Target mult: {c[1]} | RSI threshold: {c[2]} | Time decay: {'ON' if c[3] else 'OFF'}")
    print(f"Win rate: {c[4]*100:.1f}% | PF: {c[5]:.3f} | Expectancy: {c[6]:+.3f}R/trade")
    print(f"\nPer-symbol:")
    for sym, (wr, pf, exp) in c[7].items():
        print(f"  {sym:5s}: WR={wr*100:.1f}% PF={pf:.3f} Exp={exp:+.3f}R")

    # Also show the current default (1.2, 4.0, 0, 0)
    print("\n--- Current default (1.2, 4.0, RSI=0, decay=off) ---")
    cur_trades = []
    for sym in SYMBOLS:
        cur_trades.extend(simulate(sym, all_data[sym], 1.2, 4.0, 0, 0))
    wr, pf, exp = score(cur_trades)
    print(f"WR={wr*100:.1f}% PF={pf:.3f} Exp={exp:+.3f}R (n={len(cur_trades)})")
    print(f"\nPF improvement: {best_overall['pf'] / max(pf, 0.001):.2f}x")


if __name__ == "__main__":
    main()