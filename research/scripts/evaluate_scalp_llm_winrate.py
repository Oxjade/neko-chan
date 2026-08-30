"""Win-rate eval for the scalp perp agent, including the LLM decision path.

Simulates what the live agent does every cycle on REAL 5m candles:
  1. build the LONG/SHORT scenario matrix (5m closes, bars_per_year=288*365)
  2. RSI direction filter + conviction floor (0.015)
  3. pick the highest-conviction positive-EV scenario (the LLM is told to pick
     the highest-conviction actionable scenario - this is the deterministic
     proxy for that instruction)
  4. size with balance_aware_size() (risk 1% / stop, leverage clamped to venue)
  5. walk forward: target/stop/time-exit, apply fee drag

Reports win rate, profit factor, expectancy per direction and per leverage,
so we can see whether the chosen leverage is profitable.
"""

import math
import sys
import time

import requests

sys.path.insert(0, "/home/carnage/tradebotpro/service/agent")

from quant_strategy import build_scenarios, rsi as rsi_fn
from quant_strategy import RSI_ENTRY_THRESHOLD, momentum_confirmed
from live_agent import balance_aware_size

SYMBOLS = ["BTC", "ETH", "SOL", "SUI", "HYPE", "SEI", "NEAR", "ATOM"]
CONVICTION_FLOOR = 0.015
FEE_RT = 0.0009  # 0.045% taker x2
BARS_PER_YEAR_5M = 288 * 365
LOOKBACK = 60
MAX_HOLD_BARS = 48  # 4h scalp shelf life


def fetch_5m(symbol, hours=12):
    now = int(time.time() * 1000)
    r = requests.post("https://api.hyperliquid.xyz/info", json={
        "type": "candleSnapshot", "req": {"coin": symbol, "interval": "5m",
        "startTime": now - hours * 3600 * 1000, "endTime": now}}, timeout=15)
    out = []
    for c in r.json():
        out.append({"ts": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                    "l": float(c["l"]), "c": float(c["c"])})
    return out


def simulate(balance):
    trades = []
    for sym in SYMBOLS:
        bars = fetch_5m(sym, 12)
        if len(bars) < LOOKBACK + 30:
            continue
        i = LOOKBACK
        while i < len(bars) - 30:
            closes = [b["c"] for b in bars[:i]]
            px = bars[i]["c"]
            if px <= 0:
                i += 1
                continue
            sc = [s for s in build_scenarios(sym, closes, px,
                                             bars_per_year=BARS_PER_YEAR_5M)
                  if s.horizon == "scalp"]
            # momentum confirmation (EMA8>EMA21 long / EMA8<EMA21 short)
            sc = [s for s in sc if momentum_confirmed(closes, s.direction)]
            # RSI direction filter (mirrors live_agent)
            rsi = rsi_fn(closes)
            sc = [s for s in sc if
                  (s.direction == "long" and rsi >= RSI_ENTRY_THRESHOLD) or
                  (s.direction == "short" and rsi <= 100 - RSI_ENTRY_THRESHOLD)]
            # conviction floor
            sc = [s for s in sc if s.ev > 0 and s.conviction >= CONVICTION_FLOOR]
            if not sc:
                i += 1
                continue
            best = max(sc, key=lambda s: s.conviction)
            # size with the live leverage logic
            stop_pct = abs(best.entry - best.stop) / best.entry * 100
            qty, lev, _ = balance_aware_size(balance, balance, px, stop_pct, sym)
            if qty <= 0:
                i += 1
                continue
            notional = qty * px
            # walk forward
            entry = px
            stop, target = best.stop, best.target
            win = None
            exit_reason = ""
            for j in range(i + 1, min(i + MAX_HOLD_BARS, len(bars))):
                hb, lb = bars[j]["h"], bars[j]["l"]
                if best.direction == "long":
                    if hb >= target:
                        win = True; exit_reason = "target"; break
                    if lb <= stop:
                        win = False; exit_reason = "stop"; break
                else:
                    if lb <= target:
                        win = True; exit_reason = "target"; break
                    if hb >= stop:
                        win = False; exit_reason = "stop"; break
            if win is None:
                final = bars[min(i + MAX_HOLD_BARS, len(bars) - 1)]["c"]
                win = (final > entry) if best.direction == "long" else (final < entry)
                exit_reason = "time"
            R = best.R
            gross = R if win else -1.0
            net = gross - FEE_RT
            trades.append({"sym": sym, "dir": best.direction, "win": win,
                           "exit": exit_reason, "R": R, "net": net,
                           "p_win": best.p_win, "conv": best.conviction,
                           "lev": lev, "notional": notional,
                           "notional_pct": notional / balance * 100})
            i += 1
    return trades


def report(balance):
    trades = simulate(balance)
    if not trades:
        print(f"balance ${balance}: no trades")
        return
    n = len(trades)
    wins = sum(1 for t in trades if t["win"])
    wr = wins / n * 100
    gross = sum(t["R"] for t in trades if t["win"])
    losses = sum(1 for t in trades if not t["win"])
    pf = gross / max(losses, 1)
    exp = sum(t["net"] for t in trades) / n
    print(f"\n=== BALANCE ${balance:,} ===")
    print(f"Trades: {n} | Win rate: {wr:.1f}% | PF: {pf:.2f} | "
          f"Expectancy: {exp:+.2f}R/trade")
    print(f"Avg leverage: {sum(t['lev'] for t in trades)/n:.1f}x | "
          f"Avg notional: {sum(t['notional_pct'] for t in trades)/n:.0f}% of balance")
    # per direction
    for d in ("long", "short"):
        dt = [t for t in trades if t["dir"] == d]
        if not dt:
            continue
        dw = sum(1 for t in dt if t["win"])
        print(f"  {d:5s}: {len(dt):3d} trades, {dw/len(dt)*100:.0f}% win, "
              f"{sum(t['net'] for t in dt)/len(dt):+.2f}R exp")
    # per leverage bucket
    buckets = {}
    for t in trades:
        b = int(t["lev"] // 10 * 10)
        buckets.setdefault(b, []).append(t)
    print("  leverage buckets:")
    for b in sorted(buckets):
        bt = buckets[b]
        bw = sum(1 for t in bt if t["win"])
        print(f"    {b}-{b+9}x: {len(bt)} trades, {bw/len(bt)*100:.0f}% win, "
              f"{sum(t['net'] for t in bt)/len(bt):+.2f}R exp")


if __name__ == "__main__":
    for bal in (1000, 5000, 10000):
        report(bal)
