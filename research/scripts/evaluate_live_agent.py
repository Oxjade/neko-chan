"""
Statistical evaluation of the LIVE opencode-go agent's decisions.

Reads research/exports/live_agent_log.csv (written by service/agent/live_agent.py)
and applies the same rigor as evaluate_momentum_model.py:

  1. SIGNAL QUALITY  - were the agent's BUY decisions predictive of the next-day
                       direction? (precision / recall / F1 / IC vs realized)
  2. PROFITABILITY   - replay the executed trades at the recorded fill prices
                       with the platform's 0.1% fee, compare vs buy-and-hold
  3. DISCRETION      - how often did it hold, trade, and respect risk rules?
"""

import argparse
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.metrics import precision_recall_fscore_support

from evaluate_momentum_model import load_ohlc, block_bootstrap_ci

TRADE_FEE_RATE = 0.001
LOG_PATH = "research/exports/live_agent_log.csv"


def load_log(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["dt"] = df["ts"].dt.date
    return df


def evaluate(log: pd.DataFrame, ohlc: pd.DataFrame) -> dict:
    close = ohlc["Close"]
    fwd = close.pct_change().shift(-1)  # next-day return after the decision day

    trades = log[log["action"].isin(["buy", "sell"])].copy()
    buys = trades[trades["action"] == "buy"].copy()

    # 1) classification: buy decision (within market hours) vs next-day up
    y_true = []
    y_pred = []
    prices = []
    for _, row in buys.iterrows():
        day = row["ts"].date()
        target = pd.Timestamp(day, tz=close.index.tz).normalize()
        try:
            idx = close.index.get_indexer([target], method="nearest")[0]
        except Exception:
            continue
        if close.index[idx].date() != day:
            continue
        nxt = fwd.iloc[idx]
        if np.isnan(nxt):
            continue
        y_true.append(1 if nxt > 0 else 0)
        y_pred.append(1)  # agent went long
        prices.append(float(close.iloc[idx]))

    quality = {}
    if y_true:
        base = float(np.mean(y_true))
        precision, recall, f1, _ = precision_recall_fscore_support(
            y_true, y_pred, average="binary", pos_label=1, zero_division=0)
        acc = float(np.mean(np.array(y_true) == np.array(y_pred)))
        nextday = []
        for _, row in buys.iterrows():
            day = row["ts"].date()
            target = pd.Timestamp(day, tz="UTC").normalize()
            idx = close.index.get_indexer([target], method="nearest")[0]
            nxt = fwd.iloc[idx]
            if not np.isnan(nxt):
                nextday.append(nxt)
        quality = {
            "n_buys": len(y_true),
            "base_rate_up": round(base, 4),
            "accuracy": round(acc, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "buy_nextday_avg_pct": round(float(np.mean(nextday)) * 100, 4) if nextday else None,
        }

    # 2) profitability: replay executed buys/sells with fees
    cash = 100_000.0
    qty = 0.0
    entry = 0.0
    equity = []
    fees = 0.0
    fills = trades[trades["fill_ok"] == True] if "fill_ok" in trades.columns else trades
    for _, row in fills.iterrows():
        px = float(row["price"]) if pd.notna(row["price"]) and row["price"] > 0 else None
        if px is None:
            continue
        if row["action"] == "buy":
            cost = px * (1 + TRADE_FEE_RATE)
            qty = float(row["quantity"])
            cash -= qty * cost
            fees += qty * px * TRADE_FEE_RATE
            entry = px
        elif row["action"] == "sell" and qty > 0:
            proceeds = qty * px * (1 - TRADE_FEE_RATE)
            cash += proceeds
            fees += qty * px * TRADE_FEE_RATE
            qty = 0.0
        equity.append(cash + qty * (px or 0))

    # mark remaining position at last available close
    if qty > 0:
        cash += qty * float(close.iloc[-1])

    total_return = (cash / 100_000.0 - 1) * 100
    bh = (float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100

    daily = log.groupby("dt").size()
    return {
        "quality": quality,
        "profit": {
            "agent_return_pct": round(total_return, 2),
            "buyhold_pct": round(bh, 2),
            "excess_pct": round(total_return - bh, 2),
            "fees_paid": round(fees, 2),
            "n_fills": int(len(fills)),
        },
        "discretion": {
            "n_decisions": int(len(log)),
            "n_holds": int((log["action"] == "hold").sum()),
            "n_buys": int((log["action"] == "buy").sum()),
            "n_sells": int((log["action"] == "sell").sum()),
            "risk_violations": int((log["error"] != "").sum()),
            "first": str(log["ts"].min()),
            "last": str(log["ts"].max()),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=LOG_PATH)
    ap.add_argument("--start", default="2021-01-01")
    args = ap.parse_args()

    try:
        log = load_log(args.log)
    except FileNotFoundError:
        print(f"[skip] no decision log yet at {args.log} — run service/agent/live_agent.py first")
        return

    print("=" * 100)
    print("LIVE AGENT EVALUATION (opencode-go via AI-Trader paper platform)")
    print("=" * 100)

    for symbol in ("BTC-USD", "ETH-USD"):
        sym_log = log[log["symbol"] == symbol.replace("-USD", "")]
        if sym_log.empty:
            continue
        try:
            ohlc = load_ohlc(symbol, args.start, None)
        except Exception:
            continue
        res = evaluate(sym_log, ohlc)
        print(f"\n### {symbol}  ({res['discretion']['first']} .. {res['discretion']['last']})")
        d, q, p = res["discretion"], res["quality"], res["profit"]
        print(f"Decisions: {d['n_decisions']} (holds {d['n_holds']}, buys {d['n_buys']}, "
              f"sells {d['n_sells']}, guard-trips {d['risk_violations']})")
        if q:
            print(f"Buy precision: {q['precision']:.3f} | recall: {q['recall']:.3f} | "
                  f"F1: {q['f1']:.3f} | accuracy: {q['accuracy']:.3f} "
                  f"(base rate {q['base_rate_up']:.3f}, n={q['n_buys']})")
            print(f"Avg next-day return after a buy: {q['buy_nextday_avg_pct']:.4f}%")
        else:
            print("Not enough buys for classification metrics yet.")
        print(f"P&L: {p['agent_return_pct']}% vs buy&hold {p['buyhold_pct']}% "
              f"(excess {p['excess_pct']:+.2f}%), fees ${p['fees_paid']}, {p['n_fills']} fills")
        print("NOTE: live experiment just started — statistics are preliminary until "
              "a meaningful sample of decisions and next-day outcomes accumulates.")


if __name__ == "__main__":
    main()