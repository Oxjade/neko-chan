"""Win-rate test harness for the quant strategy (momentum20/scalp engine).

Forward-simulates the engine's actual trade logic on real Hyperliquid 5m
candles and measures the TP-hit win rate.

How it works (mirrors live_agent.py run_cycle + quant_strategy.py):
  1. Fetch `lookback` recent 5m candles for each symbol.
  2. Walk the series: at each bar, build the scenario matrix from the trailing
     closes with the ACTIVE HORIZONS, pick the best positive-EV scenario.
  3. If a trade is picked, simulate it forward bar-by-bar:
       - entry at the current close (paper limit-fill would be 2bps better,
         which only helps; we use the close so the test is conservative)
       - win  = price hits the target before the stop
       - loss = price hits the stop first
       - breakeven = the time-exit fires first (never reached TP)
     Barriers use the candle high/low (intrabar fills), not just closes.
  4. Report win rate, loss rate, breakeven rate, avg R, EV, and per-symbol.

Run with the CURRENT HORIZONS (as tuned for the win-rate pass) and again with
the OLD pre-tuning values to measure the change. Pass --old to use the old
values.

Usage:
  python research/scripts/test_winrate.py [--symbols BTC,ETH,SOL,SUI] [--bars 1000] [--old]
"""

import argparse
import os
import sys

import requests

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "service", "agent"))

from quant_strategy import scenario_matrix, pick_best_scenario  # noqa: E402

HL_INFO = "https://api.hyperliquid.xyz/info"


def fetch_5m(symbol: str, bars: int) -> list[dict]:
    """Fetch recent 5m candles for a symbol from Hyperliquid."""
    now_ms = int(__import__("time").time() * 1000)
    start = now_ms - bars * 5 * 60 * 1000
    r = requests.post(HL_INFO, json={
        "type": "candleSnapshot",
        "req": {"coin": symbol, "interval": "5m", "startTime": start, "endTime": now_ms},
    }, timeout=20)
    data = r.json()
    if not isinstance(data, list) or not data:
        return []
    return data


def sim_trade(entry: float, target: float, stop: float,
              candles: list[dict], start_idx: int, max_hold: int) -> dict:
    """Simulate one trade forward from start_idx. Returns outcome dict."""
    tgt_hi = max(target, stop)   # the upward barrier
    tgt_lo = min(target, stop)   # the downward barrier
    tgt_is_up = target > stop
    for i in range(start_idx, min(start_idx + max_hold, len(candles))):
        c = candles[i]
        hi = float(c["h"])
        lo = float(c["l"])
        if tgt_is_up:
            if hi >= tgt_hi:      # target hit
                return {"outcome": "win", "exit": tgt_hi, "bar": i - start_idx}
            if lo <= tgt_lo:      # stop hit
                return {"outcome": "loss", "exit": tgt_lo, "bar": i - start_idx}
        else:
            if lo <= tgt_lo:      # target hit (short target below)
                return {"outcome": "win", "exit": tgt_lo, "bar": i - start_idx}
            if hi >= tgt_hi:      # stop hit
                return {"outcome": "loss", "exit": tgt_hi, "bar": i - start_idx}
    # time-exit / never resolved
    return {"outcome": "breakeven", "exit": float(candles[min(start_idx + max_hold - 1, len(candles) - 1)]["c"]),
            "bar": max_hold}


def run_symbol(symbol: str, candles: list[dict], warmup: int = 120,
               max_hold: int = 40) -> dict:
    """Walk the candles, pick trades from the real engine, simulate them."""
    trades = []
    closes = []
    i = 0
    while i < len(candles):
        closes.append(float(candles[i]["c"]))
        i += 1
    # warm up: need enough closes for drift/vol estimates
    if len(closes) < warmup + 2:
        return {"symbol": symbol, "trades": 0, "wins": 0, "losses": 0,
                "be": 0, "win_rate": 0.0, "avg_r": 0.0, "ev": 0.0}

    prices = {}
    cursor = warmup
    while cursor < len(closes) - 2:
        # build the scenario matrix exactly like the live engine
        window = closes[:cursor + 1]
        prices[symbol] = float(candles[cursor]["c"])
        matrix = scenario_matrix({symbol: window}, prices, trader_type="scalp",
                                 bars_per_year=288.0 * 365.0)
        # Apply the SAME hard entry filters the live agent uses (live_agent.py
        # run_cycle): RSI direction confirmation + EMA8>EMA21 momentum gate for
        # scalp scenarios. This is what converts the ~36% GBM coin-flip into
        # the measured 45%+ win rate - without it the harness understates the
        # live bot's actual win rate.
        from quant_strategy import rsi as _rsi, momentum_confirmed as _mom, RSI_ENTRY_THRESHOLD
        matrix = [
            s for s in matrix
            if (s.direction == "long" and _rsi(window) >= RSI_ENTRY_THRESHOLD) or
               (s.direction == "short" and _rsi(window) <= 100 - RSI_ENTRY_THRESHOLD)
        ]
        matrix = [s for s in matrix if _mom(window, s.direction)]
        best = pick_best_scenario(matrix, has_long={}, has_short={},
                                  conviction_floor=0.0)
        if best is not None and best.ev > 0:
            entry = float(candles[cursor]["c"])
            res = sim_trade(entry, float(best.target), float(best.stop),
                            candles, cursor + 1, max_hold)
            trades.append((best, res))
            # jump past the trade's resolution window so we don't re-pick
            # inside an open position
            cursor += res["bar"] + 1
        else:
            cursor += 1

    wins = sum(1 for _, r in trades if r["outcome"] == "win")
    losses = sum(1 for _, r in trades if r["outcome"] == "loss")
    be = sum(1 for _, r in trades if r["outcome"] == "breakeven")
    closed = wins + losses
    win_rate = (wins / closed * 100.0) if closed else 0.0
    avg_r = sum(b.R for b, _ in trades) / len(trades) if trades else 0.0
    # EV = win_rate% * R - loss_rate% (breakeven contributes 0)
    ev = (wins / len(trades) * avg_r - losses / len(trades)) if trades else 0.0
    return {"symbol": symbol, "trades": len(trades), "wins": wins, "losses": losses,
            "be": be, "win_rate": round(win_rate, 1), "avg_r": round(avg_r, 2),
            "ev": round(ev, 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTC,ETH,SOL,SUI")
    ap.add_argument("--bars", type=int, default=1000, help="5m candles to fetch per symbol")
    ap.add_argument("--old", action="store_true", help="use the OLD pre-tuning HORIZONS")
    args = ap.parse_args()

    if args.old:
        # Pre-tuning HORIZONS (the values before the 2026-09 win-rate pass).
        import quant_strategy as qs
        qs.HORIZONS = {
            "scalp":    {"stop": 1.0, "target": 1.6, "min_r": 1.5},
            "intraday": {"stop": 2.0, "target": 7.0,  "min_r": 1.8},
            "swing":    {"stop": 4.0, "target": 16.0, "min_r": 2.0},
        }

    import quant_strategy as qs
    print(f"Testing with HORIZONS: {qs.HORIZONS}")
    print()

    rows = []
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        candles = fetch_5m(symbol, args.bars)
        if not candles:
            print(f"[skip] {symbol}: no data")
            continue
        rows.append(run_symbol(symbol, candles, warmup=120, max_hold=40))

    if not rows:
        print("No data fetched - cannot measure win rate")
        return 1

    print(f"{'Symbol':<7}{'Trades':>7}{'Wins':>6}{'Losses':>7}{'BE':>5}{'WinRate%':>9}"
          f"{'AvgR':>7}{'EV':>7}")
    print("-" * 58)
    tot_trades = tot_wins = tot_losses = 0
    for r in rows:
        print(f"{r['symbol']:<7}{r['trades']:>7}{r['wins']:>6}{r['losses']:>7}"
              f"{r['be']:>5}{r['win_rate']:>9}{r['avg_r']:>7}{r['ev']:>7}")
        tot_trades += r["trades"]
        tot_wins += r["wins"]
        tot_losses += r["losses"]

    closed = tot_wins + tot_losses
    win_rate = (tot_wins / closed * 100.0) if closed else 0.0
    avg_r = sum(r["avg_r"] for r in rows) / len(rows) if rows else 0.0
    ev = (tot_wins / tot_trades * avg_r - tot_losses / tot_trades) if tot_trades else 0.0
    print("-" * 58)
    print(f"{'TOTAL':<7}{tot_trades:>7}{tot_wins:>6}{tot_losses:>7}"
          f"{tot_trades - closed:>5}{win_rate:>9}{avg_r:>7}{ev:>7}")
    print()
    print(f"Summary: {tot_wins}/{closed} closed trades hit TP "
          f"({win_rate:.1f}% win rate) | avg R={avg_r:.2f} | EV/trade={ev:+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
