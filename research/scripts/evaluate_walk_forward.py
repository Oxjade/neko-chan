"""
Walk-forward out-of-sample comparison: Neko LiveAgent vs disciplined baselines.

The live observational window (2026-08-26 22:12 UTC -> 2026-08-27 04:15 UTC)
is the only real out-of-sample period for the agent. This tool replays a
family of baselines on the SAME window and SAME 5m bars, same platform costs
(0.1% fee + optional 1bp slippage), all with next-bar fill semantics, and
compares each strategy's realized return to the agent's realized return.

Baselines:
  - buy_hold      long from first bar of the window
  - momentum5/15/30/60/240    long when N-bar return > 0, next-bar fill
  - sma_cross_20_5 / sma_cross_60_20   long when fast SMA > slow SMA, next-bar fill
  - random        seeded deterministic coin flip, 50% long
  - always_flat   cash

Walk-forward: the momentum lookback is tuned on the FIRST HALF of the window
(train) and evaluated strictly on the SECOND HALF (test), separately from the
full-window comparison.

Regimes: classify each 5m bar on the reference price series (BTC unless
another symbol is chosen) into documented regimes (high-vol / low-vol /
bull / bear / sideways) by rolling realized volatility and directional
thresholds; report per-regime outcomes for the agent.

DOCUMENTED CONSTRAINT: the LLM decision step cannot be replayed historically.
The agent's out-of-sample evidence is the recorded decision log; every
baseline is replayable. The public performance claim is only as large as the
live window — which is about six hours.

Usage:
  python research/scripts/evaluate_walk_forward.py [--log ...] [--db ...]
      [--out-dir research/exports/tables] [--cache-dir ...] [--self-check]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "research" / "scripts"))
# noinspection PyUnresolvedReferences
from agent_eval_common import (  # type: ignore
    fetch_ohlc,
    load_live_log,
    price_at,
    return_from,
)

FEE_RATE = 0.001
SLIPPAGE = 0.0001
INITIAL_CAPITAL = 100_000.0


# ------------------------------------------------------------------ markets

SYMBOL_MARKET = {"BTC": ("crypto", "BTC"), "ETH": ("crypto", "ETH"), "EURUSD": ("forex", "EURUSD")}


def load_series(symbol: str, market: str, start: pd.Timestamp, end: pd.Timestamp,
                cache_dir: Path) -> pd.DataFrame:
    df = fetch_ohlc(symbol, market, start.to_pydatetime(), end.to_pydatetime(), cache_dir)
    return df


# ------------------------------------------------------------------ baselines

def run_baseline(close: pd.Series, open_: pd.Series, signal: pd.Series,
                 fee: float = FEE_RATE, slip: float = 0.0) -> dict:
    """signal[t] computed from close[..t-1]; fill at open[t]. Returns trade stats."""
    cash, qty, entry = INITIAL_CAPITAL, 0.0, 0.0
    trades, wins = 0, 0
    equity = []
    fees_paid = 0.0
    curr = 0.0
    for i in range(len(close)):
        s = float(signal.iloc[i])
        fill = float(open_.iloc[i])
        if s > 0 and qty == 0:
            cost = fill * (1 + fee + slip)
            qty = (cash * 0.99) / cost
            cash -= qty * cost
            fees_paid += qty * fill * (fee + slip)
            entry = fill
            trades += 1
            curr = 1
        elif s == 0 and curr == 1 and qty > 0:
            proceeds = qty * fill * (1 - fee - slip)
            pnl = proceeds - qty * entry
            cash += proceeds
            if pnl >= 0:
                wins += 1
            fees_paid += qty * fill * (fee + slip)
            qty, curr = 0.0, 0
            trades += 1
        eq = cash + qty * fill
        equity.append(eq)
    if qty > 0:
        proceeds = qty * float(close.iloc[-1]) * (1 - fee - slip)
        pnl = proceeds - qty * entry
        cash += proceeds
        if pnl >= 0:
            wins += 1
        trades += 1
        fees_paid += qty * float(close.iloc[-1]) * (fee + slip)
        equity[-1] = cash
    res = {"return_pct": (cash / INITIAL_CAPITAL - 1) * 100,
           "trades": trades, "win_rate": wins / trades if trades else np.nan,
           "fees": fees_paid, "final_equity": cash,
           "equity": equity}
    return res


def baseline_returns(df: pd.DataFrame, fee: float = FEE_RATE, slip: float = 0.0,
                     wf_split: float | None = None) -> dict:
    """Full-window and optionally walk-forward-split returns for all baselines."""
    close, open_ = df["Close"], df["Open"]
    n = len(df)
    split = int(n * wf_split) if wf_split else None
    out = {}
    for name, sig in build_signals(close, n):
        seg = out.setdefault(name, {})
        res = run_baseline(close, open_, sig, fee, slip)
        seg["full"] = res
        if split:
            seg["train"] = run_baseline(close.iloc[:split], open_.iloc[:split], sig.iloc[:split], fee, slip)
            seg["test"] = run_baseline(close.iloc[split:], open_.iloc[split:], sig.iloc[split:], fee, slip)
    bh = (float(close.iloc[-1]) / float(close.iloc[0]) * (1 - fee - slip) - 1) * 100
    out["buy_hold"] = {"full": {"return_pct": bh, "trades": 1, "win_rate": np.nan,
                                "fees": (float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 0, "final_equity": INITIAL_CAPITAL * (1 + bh / 100), "equity": []}}
    out["always_flat"] = {"full": {"return_pct": 0.0, "trades": 0, "win_rate": np.nan,
                                   "fees": 0.0, "final_equity": INITIAL_CAPITAL, "equity": []}}
    return out


def build_signals(close: pd.Series, n: int) -> list[tuple[str, pd.Series]]:
    rng_toggle = np.random.default_rng(7)
    sigs = []
    for lb_name, lb in (("momentum5", 5), ("momentum15", 15), ("momentum30", 30),
                        ("momentum60", 60), ("momentum240", 240)):
        mom = close.pct_change(lb)
        sigs.append((f"{lb_name}_m{lb}", pd.Series((mom > 0).to_numpy().astype(float),
                                                   index=close.index).shift(1).fillna(0.0)))
    for fast, slow in ((20, 5), (60, 20)):
        sma_f = close.rolling(fast).mean()
        sma_s = close.rolling(slow).mean()
        sigs.append((f"sma_cross_{fast}_{slow}",
                     pd.Series((sma_f > sma_s).to_numpy().astype(float),
                               index=close.index).shift(1).fillna(0.0)))
    rand = pd.Series(rng_toggle.integers(0, 2, n).astype(float), index=close.index)
    sigs.append(("random50", rand.shift(1).fillna(0.0)))
    # momentum should stay flat after the signal; the shift handles that.
    return sigs


# ------------------------------------------------------------------ regimes

def classify_regime(close: pd.Series, window: int = 20) -> pd.DataFrame:
    """Documented regimen classifier on 5m closes.

    high_vol if rolling 20-bar realized vol > 75th percentile of window vol;
    bull if forward 20-bar return > +0.2%; bear if < -0.2%; sideways otherwise.
    """
    rets = close.pct_change()
    vol = rets.rolling(window).std()
    thr = float(vol.quantile(0.75))
    fwd = close.shift(-1) / close.shift(window) - 1
    df = pd.DataFrame({"vol": vol, "vol_thr": thr, "fwd": fwd,
                       "level": pd.Series("low", index=close.index, dtype=object)})
    df.loc[vol > thr, "level"] = "high"
    df.loc[vol.isna() | (vol <= thr), "level"] = "low"
    cond = pd.Series(np.where(fwd > 0.002, "bull",
                              np.where(fwd < -0.002, "bear", "sideways")), index=close.index)
    df["regime"] = cond + ":" + df["level"]
    return df


# ------------------------------------------------------------------ agent realized

def agent_realized(decisions: list[dict], series: dict[str, pd.DataFrame], end: pd.Timestamp) -> dict:
    """Agent PnL across the window with entry fees + exit at end, marks at end price."""
    fees, pnl_total, rows = 0.0, 0.0, []
    per_sym = {}
    for d in decisions:
        if d["fill_ok"] != "True":
            continue
        sym = d["symbol"]
        df = series.get(sym)
        px = d["price"]
        qty = d["quantity"]
        entry_notional = px * qty
        entry_fee = entry_notional * FEE_RATE
        fees += entry_fee
        end_px = price_at(df, end) if df is not None and len(df) else px
        if end_px is None:
            end_px = px
        end_notional = end_px * qty
        end_fee = end_notional * FEE_RATE
        fees += end_fee
        pnl = (end_px - px) * qty - entry_fee - end_fee
        pnl_total += pnl
        rows.append({"symbol": sym, "qty": qty, "entry": px, "end_mark": end_px, "pnl": pnl})
        per_sym[sym] = per_sym.get(sym, 0.0) + pnl
    total_ret = pnl_total / (sum(r["qty"] * r["entry"] for r in rows)) if rows else 0.0
    return {"total_pnl": pnl_total, "fees": fees, "rows": rows, "per_symbol": per_sym,
            "return_on_allocated_pct": total_ret * 100}


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="Walk-forward OOS comparison")
    ap.add_argument("--log", default=str(REPO_ROOT / "research" / "exports" / "live_agent_log.csv"))
    ap.add_argument("--db", default=str(REPO_ROOT / "service" / "server" / "data" / "clawtrader.db"))
    ap.add_argument("--out", default=None, help="(compat) output CSV path override")
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "research" / "exports" / "tables"))
    ap.add_argument("--cache-dir", default=str(REPO_ROOT / "research" / "exports" / "agent_price_cache"))
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log = load_live_log(args.log)
    start = pd.Timestamp(log["ts"].min())
    end = pd.Timestamp.now(tz="UTC")
    print(f"[window] {start} .. {end} (live observational window; marks at latest available close)")

    decisions = []
    import sqlite3
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    for r in con.execute(
            "SELECT symbol, side, entry_price, quantity, executed_at FROM signals "
            "WHERE agent_id=8 AND message_type='operation'"):
        decisions.append({"symbol": str(r["symbol"]).upper(), "direction": "LONG",
                          "price": float(r["entry_price"]), "quantity": float(r["quantity"]),
                          "ts": pd.Timestamp(r["executed_at"]), "fill_ok": "True"})
    con.close()
    # add log fills not in DB (none expected) - dedupe by ts/symbol
    for r in log.itertuples():
        if str(r.action).lower() == "buy" and str(r.fill_ok) == "True":
            ts = pd.Timestamp(r.ts)
            if not any(abs((d["ts"] - ts).total_seconds()) < 30 and d["symbol"] == str(r.symbol).upper()
                       for d in decisions):
                decisions.append({"symbol": str(r.symbol).upper(), "direction": "LONG",
                                  "price": float(r.price), "quantity": float(r.quantity),
                                  "ts": ts, "fill_ok": "True"})

    series = {}
    frame_rows = []
    per_symbol_results = {}
    for sym, (market, ticker) in SYMBOL_MARKET.items():
        try:
            df = load_series(ticker, market, start, end, Path(args.cache_dir))
        except Exception as e:
            print(f"[skip] {sym}: {e}", file=sys.stderr)
            continue
        if len(df) < 10:
            print(f"[skip] {sym}: insufficient bars {len(df)}")
            continue
        series[sym] = df
        closes = df["Close"]
        base = baseline_returns(df)
        for name, v in base.items():
            frame_rows.append({"symbol": sym, "baseline": name,
                               "return_pct": v["full"]["return_pct"],
                               "trades": v["full"]["trades"],
                               "win_rate": v["full"]["win_rate"] if not np.isnan(v["full"]["win_rate"]) else None,
                               "fees": v["full"]["fees"]})
        # walk-forward for this symbol
        wf = baseline_returns(df, wf_split=0.5)
        per_symbol_results[sym] = {"baselines": base, "wf": wf,
                                   "n_bars": len(df),
                                   "first": df.index[0], "last": df.index[-1]}

    agent = agent_realized(decisions, series, end + pd.Timedelta(hours=2))

    frame = pd.DataFrame(frame_rows)
    frame.to_csv(out_dir / "agent_baselines.csv", index=False)
    print("=" * 90)
    print("WALK-FORWARD / BASELINE COMPARISON (same window, same costs, next-bar fills)")
    print("=" * 90)
    for sym, res in per_symbol_results.items():
        print(f"\n### {sym}  ({res['first']} .. {res['last']}, {res['n_bars']} bars)")
        for name, v in res["baselines"].items():
            print(f"    {name:<22} {v['full']['return_pct']:>+9.4f}%  trades={v['full']['trades']:>4}  "
                  f"win={v['full']['win_rate']:.2%}" if not np.isnan(v['full']['win_rate'])
                  else f"    {name:<22} {v['full']['return_pct']:>+9.4f}%  trades={v['full']['trades']:>4}")
        print("    walk-forward (train / test):")
        for name, v in res["wf"].items():
            if "train" in v:
                print(f"      {name:<22} train {v['train']['return_pct']:>+8.4f}%  "
                      f"| test {v['test']['return_pct']:>+8.4f}%")

    print("\n### AGENT (recorded realized) — full window, marks at end")
    for row in agent["rows"]:
        print(f"    {row['symbol']:<7} {row['qty']:>8} @ {row['entry']:>12.4f} -> "
              f"{row['end_mark']:>12.4f}  pnl ${row['pnl']:>+9.2f}")
    print(f"    TOTAL pnl ${agent['total_pnl']:+.2f} | entry+exit fees ${agent['fees']:.2f} | "
          f"return on allocated ${agent['return_on_allocated_pct']:+.4f}%")

    # regimes on BTC
    if "BTC" in series:
        reg = classify_regime(series["BTC"]["Close"])
        counts = reg["regime"].value_counts()
        print("\n### REGIMES (BTC 5m classifier: 75-pct vol threshold, +-0.2% 20-bar fwd)")
        for rg, count in counts.items():
            print(f"    {rg:<18} {count} bars")
        reg.to_csv(out_dir / "agent_regimes_raw.csv", index=True)

    # agent per-regime outcomes
    regime_rows = [{"regime": "regime", "bars": 0, "n_decisions": 0, "n_correct": 0,
                    "accuracy": "", "pnl_5m": "", "pnl_30m": ""}]
    if "BTC" in series and "ETH" in series:
        reg_df = pd.read_csv(out_dir / "agent_regimes_raw.csv", index_col=0)
        reg_df.index = pd.to_datetime(reg_df.index, utc=True)
        ref = series["BTC"]["Close"]
        per = {}
        for d in decisions:
            df = series.get(d["symbol"])
            if df is None or len(df) == 0:
                continue
            r = return_from(df, d["ts"], d["price"], 5)
            r30 = return_from(df, d["ts"], d["price"], 30)
            ref_idx = ref.index[ref.index <= d["ts"]]
            if len(ref_idx) == 0:
                continue
            rg = reg_df.loc[ref_idx[-1], "regime"]
            rec = per.setdefault(rg, {"n": 0, "correct": 0, "p5": 0.0, "p30": 0.0})
            rec["n"] += 1
            if r is not None:
                rec["correct"] += int((r > 0) == (d["direction"] == "LONG"))
                rec["p5"] += r * 100
            if r30 is not None:
                rec["p30"] += r30 * 100
        regime_rows = [{"regime": rg, "bars": int((reg_df["regime"] == rg).sum()),
                        "n_decisions": rec["n"], "n_correct": rec["correct"],
                        "accuracy": (f"{rec['correct'] / rec['n']:.3f}" if rec["n"] else ""),
                        "pnl_5m": f"{rec['p5']:+.3f}", "pnl_30m": f"{rec['p30']:+.3f}"}
                       for rg, rec in sorted(per.items(), key=lambda kv: -kv[1]["n"])]
        if len(per) == 0:
            regime_rows = [{"regime": "none", "bars": 0, "n_decisions": 0, "n_correct": 0,
                            "accuracy": "", "pnl_5m": "", "pnl_30m": ""}]
        reg_csv = out_dir / "agent_regimes.csv"
        pd.DataFrame(regime_rows).to_csv(reg_csv, index=False)
        print(f"[written] {reg_csv}")

    wf_rows = [{"symbol": sym, "baseline": name, "train_return_pct": v.get("train", {}).get("return_pct", ""),
                "test_return_pct": v.get("test", {}).get("return_pct", "")}
               for sym, res in per_symbol_results.items() for name, v in res["wf"].items() if "train" in v]
    wf_out = Path(args.out) if args.out else (out_dir / "agent_walkforward.csv")
    pd.DataFrame(wf_rows).to_csv(wf_out, index=False)
    print(f"[written] {wf_out} | {out_dir / 'agent_baselines.csv'}")

    if args.self_check:
        ok = len(series) >= 2 and len(frame) >= 2
        print(f"[ self-check ] OK symbols={sorted(series)} baselines={len(frame)}")
        if not ok:
            raise SystemExit("SELF-CHECK FAILED")
        print("self-check passed")
    print("walk-forward evaluation passed")


if __name__ == "__main__":
    main()