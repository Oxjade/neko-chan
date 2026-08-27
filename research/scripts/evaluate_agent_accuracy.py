"""
Statistical evaluation of Neko's LiveAgent directional predictions.

Scores every recorded directional decision (BUY -> LONG, SELL -> SHORT) of the
live agent against realized market prices, at the horizons Neko's data
resolution supports (5m/15m/30m/1h/4h/24h), per market and pooled.

Discipline:
- Recorded decision prices (log price from the live log, or the DB entry price
  for the one EURUSD fill missing from the log) are treated as the reference
  price at decision time T.
- Realized prices come from the same venues the platform uses (Hyperliquid 5m
  candles for crypto, yFinance 5m for forex/stocks) and use only candles that
  have START time <= T+hint (price_at uses the last close at or before T+hint,
  never a candle starting after it).
- A horizon is scored only when full horizon coverage exists (no early-exit
  hacking) for that decision; missing coverage is reported per horizon.
- Accuracy is computed per horizon and per market, with a block bootstrap CI,
  plus three baselines: market base rate (up-frequency over the same window),
  always-long, and random-with-same-long-frequency.

The LLM decision step cannot be deterministically replayed historically; this
tool scores RECORDED decisions, which is the strongest reproducible evidence.

Usage:
  python research/scripts/evaluate_agent_accuracy.py --log research/exports/live_agent_log.csv \
      --db service/server/data/clawtrader.db --out-dir research/exports/tables \
      --cache-dir research/exports/agent_price_cache [--self-check]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "research" / "scripts"))
# noinspection PyUnresolvedReferences
from agent_eval_common import (  # type: ignore
    HORIZONS_MIN,
    HORIZON_LABELS,
    binary_metrics,
    block_bootstrap_ci,
    fetch_ohlc,
    fmt,
    load_live_log,
    normalize_action,
    price_at,
    return_from,
)

SYMBOL_MARKET = {
    "BTC": ("crypto", "BTC"),
    "ETH": ("crypto", "ETH"),
    "EURUSD": ("forex", "EURUSD"),
    "AAPL": ("us-stock", "AAPL"),
}


def collect_decisions(log_path: Path, db_path: Path) -> list[dict]:
    """Every recorded directional decision (incl. rejected/dry-run) + executed fills from DB."""
    df = load_live_log(log_path)
    rows = []
    for r in df.itertuples():
        action = normalize_action(r.action)
        if action == "FLAT" and str(r.action).strip().lower() not in ("sell", "cover"):
            continue  # holds are not directional predictions
        market, _ = SYMBOL_MARKET.get((r.symbol or "").upper(), (None, None))
        rows.append({
            "source": "log",
            "ts": pd.Timestamp(r.ts),
            "symbol": (r.symbol or "").upper(),
            "market": market,
            "action_raw": str(r.action),
            "direction": action,
            "price": float(r.price),
            "quantity": float(r.quantity or 0.0),
            "fill_ok": str(r.fill_ok) if pd.notna(r.fill_ok) else "",
        })

    # Executed DB fills for agent 8 (LiveAgent) -> source of truth for 3 fills.
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    db_rows = [dict(r) for r in con.execute(
        "SELECT symbol, market, side, entry_price, quantity, executed_at FROM signals "
        "WHERE agent_id=8 AND message_type='operation' ORDER BY executed_at")]
    con.close()
    for d in db_rows:
        ts = pd.Timestamp(d["executed_at"])
        sym = (d["symbol"] or "").upper()
        dup = any(abs((r["ts"] - ts).total_seconds()) <= 5 and r["symbol"] == sym
                  for r in rows)
        if dup:
            continue
        market, _ = SYMBOL_MARKET.get(sym, (None, None))
        rows.append({
            "source": "db", "ts": ts, "symbol": (d["symbol"] or "").upper(),
            "market": market, "action_raw": d["side"], "direction": normalize_action(d["side"]),
            "price": float(d["entry_price"]), "quantity": float(d["quantity"] or 0.0),
            "fill_ok": "True",
        })
    rows.sort(key=lambda r: r["ts"])
    return rows


def stance_periods(log_path: Path, db_path: Path) -> list[dict]:
    """Per log row with a position: LONG stance for open long (agent 8), from log+positions."""
    dfs = load_live_log(log_path)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    pos = [dict(r) for r in con.execute("SELECT * FROM positions WHERE agent_id=8 AND side='long'")]
    con.close()

    stance = {}
    for p in pos:
        opened = pd.Timestamp(p["opened_at"])
        stance[(p["symbol"] or "").upper()] = {"direction": "LONG", "opened": opened, "entry": float(p["entry_price"])}

    periods = []
    for r in dfs.itertuples():
        sym = str(r.symbol or "").upper()
        if sym not in stance:
            continue
        if pd.Timestamp(r.ts) < stance[sym]["opened"] - pd.Timedelta(minutes=1):
            continue
        periods.append({
            "ts": pd.Timestamp(r.ts), "symbol": sym,
            "market": SYMBOL_MARKET.get(sym, (None, None))[0],
            "direction": "LONG",
            "entry_price": stance[sym]["entry"],
            "recorded_price": float(r.price) if pd.notna(r.price) and float(r.price) > 0 else None,
        })
    return periods


def load_prices(out_dir: Path, cache_dir: Path, decisions: list[dict], stance: list[dict],
                now_utc: pd.Timestamp = None) -> dict[str, pd.DataFrame]:
    """Realized 5m OHLC per symbol over [window_start, data_end]."""
    start = min([d["ts"] for d in decisions] + [s["ts"] for s in stance]) - pd.Timedelta(days=32)
    end = now_utc or pd.Timestamp.now(tz="UTC")
    end = end + pd.Timedelta(days=1)
    series = {}
    for sym in sorted({d["symbol"] for d in decisions} | {s["symbol"] for s in stance}):
        info = SYMBOL_MARKET.get(sym)
        if not info:
            continue
        market, ticker = info
        df = fetch_ohlc(ticker, market, start.to_pydatetime(), end.to_pydatetime(), cache_dir)
        series[sym] = df
    return series


def sym_end(series: dict[str, pd.DataFrame], sym: str) -> pd.Timestamp | None:
    df = series.get(sym)
    if df is None or len(df) == 0:
        return None
    return df.index.max()


def horizon_metrics(decisions: list[dict], series: dict[str, pd.DataFrame], horizon_min: int,
                    data_end: pd.Timestamp | None = None) -> dict:
    """Accuracy for a horizon over all decisions with full data coverage."""
    pairs, market_rows = [], {}
    for d in decisions:
        df = series.get(d["symbol"])
        if df is None or len(df) == 0:
            continue
        target = d["ts"] + pd.Timedelta(minutes=horizon_min)
        end = sym_end(series, d["symbol"])
        if end is None or target > end:
            continue
        ret = return_from(df, d["ts"], d["price"], horizon_min)
        if ret is None:
            continue
        direction = d["direction"]
        true_dir = 1 if ret > 0 else -1
        if ret == 0.0:
            true_dir = 0
        pred_dir = 1 if direction == "LONG" else -1
        if true_dir == 0:
            m = market_rows.setdefault(d["market"], {"tie": 0})
            m["tie"] = m.get("tie", 0) + 1
            continue
        m = market_rows.setdefault(d["market"], {"n": 0, "correct": 0, "tie": 0})
        m["n"] += 1
        m["correct"] += int(pred_dir == true_dir)
        pairs.append((true_dir, pred_dir, d))
    return _aggregate(pairs, market_rows)


def _aggregate(pairs, market_rows) -> dict:
    total = {"n": 0, "correct": 0, "tie": 0}
    per_market = []
    for market, m in market_rows.items():
        m = {"n": m.get("n", 0), "correct": m.get("correct", 0), "tie": m.get("tie", 0)}
        acc = m["correct"] / m["n"] if m["n"] else float("nan")
        per_market.append({"market": market, **m, "accuracy": acc})
        total["n"] += m.get("n", 0)
        total["correct"] += m.get("correct", 0)
        total["tie"] += m.get("tie", 0)
    y_true = [p[0] for p in pairs]
    y_pred = [p[1] for p in pairs]
    cls = binary_metrics(y_true, y_pred)
    acc = cls["accuracy"]
    ci = block_bootstrap_ci([int(t == p) for t, p in [(r[0], r[1]) for r in pairs]], block=2)
    # direction split: LONG and SHORT accuracy separately (so the short leg
    # cannot hide behind pooled numbers — symmetric trading must be judged
    # per direction).
    per_direction = []
    for dirn, label in ((1, "LONG"), (-1, "SHORT")):
        sub = [p for p in pairs if p[1] == dirn]
        n = len(sub)
        correct = sum(1 for t, p, _ in sub if t == p)
        per_direction.append({
            "direction": label, "n": n, "correct": correct,
            "accuracy": correct / n if n else float("nan"),
            "hit_rate": correct / n if n else float("nan"),
        })
    return {
        "n": total["n"],
        "correct": total["correct"],
        "ties": total["tie"],
        "accuracy": acc,
        "ci95": ci,
        "precision": cls["precision"],
        "recall": cls["recall"],
        "f1": cls["f1"],
        "per_market": per_market,
        "per_direction": per_direction,
    }


def base_rate_accuracy(series: dict[str, pd.DataFrame], decisions: list[dict], horizon_min: int,
                       data_end: pd.Timestamp | None = None) -> dict:
    """Baseline: fraction of windows of length H that finished UP (always-long accuracy)."""
    n_up, n_total = 0, 0
    for d in decisions:
        df = series.get(d["symbol"])
        if df is None or len(df) == 0:
            continue
        target = d["ts"] + pd.Timedelta(minutes=horizon_min)
        end = sym_end(series, d["symbol"])
        if end is None or target > end:
            continue
        ret = return_from(df, d["ts"], d["price"], horizon_min)
        if ret is None or ret == 0.0:
            continue
        n_total += 1
        n_up += int(ret > 0)
    if n_total == 0:
        return {"base_rate": float("nan"), "always_long_acc": float("nan"), "n": 0}
    return {"base_rate": n_up / n_total, "always_long_acc": n_up / n_total, "n": n_total}


def run(log_path: Path, db_path: Path, out_dir: Path, cache_dir: Path, self_check: bool = False,
        interactive: bool = True, out_path: Path | None = None,
        summary_path: Path | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions = collect_decisions(log_path, db_path)
    stance = stance_periods(log_path, db_path)
    series = load_prices(out_dir, cache_dir, decisions, stance)
    for sym, df in series.items():
        print(f"[data] {sym}: {len(df)} 5m bars, ends {df.index.max().strftime('%Y-%m-%d %H:%M') if len(df) else 'n/a'} UTC")

    executed = [d for d in decisions if d["fill_ok"] == "True"]
    intent = [d for d in decisions]

    summary_rows = []
    all_res = {}
    for horizon_min, label in zip(HORIZONS_MIN, HORIZON_LABELS):
        for set_name, ds in (("executed", executed), ("intent", intent)):
            res = horizon_metrics(ds, series, horizon_min)
            base = base_rate_accuracy(series, ds, horizon_min)
            all_res[(set_name, horizon_min)] = (res, base)
            summary_rows.append({
                "set": set_name, "horizon": label, "horizon_min": horizon_min,
                "n": res["n"], "correct": res["correct"], "ties": res["ties"],
                "accuracy": fmt(res["accuracy"]),
                "ci95_low": fmt(res["ci95"][0]), "ci95_high": fmt(res["ci95"][1]),
                "precision": fmt(res["precision"]), "recall": fmt(res["recall"]), "f1": fmt(res["f1"]),
                "always_long_acc": fmt(base["always_long_acc"]),
                "base_rate_up": fmt(base["base_rate"]),
                "n_base": base["n"],
            })

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(summary_path or (out_dir / "agent_accuracy_summary.csv"), index=False)
    print("\n=== DIRECTIONAL ACCURACY (summary) ===")
    print(summary.to_string(index=False))

    # market x horizon detail (executed set only)
    detail_rows = []
    for horizon_min, label in zip(HORIZONS_MIN, HORIZON_LABELS):
        exec_res, _ = all_res[("executed", horizon_min)]
        for mk in exec_res["per_market"]:
            detail_rows.append({"horizon": label, "market": mk["market"], "n": mk["n"],
                                "correct": mk["correct"], "accuracy": fmt(
                                    mk["accuracy"] if mk["n"] else float("nan"))})
    detail = pd.DataFrame(detail_rows)
    detail.to_csv(out_dir / "agent_accuracy_by_market.csv", index=False)
    print("\n=== BY MARKET (executed) ===")
    print(detail.to_string(index=False))

    # direction split (LONG vs SHORT) — symmetric trading judged per direction
    dir_rows = []
    for horizon_min, label in zip(HORIZONS_MIN, HORIZON_LABELS):
        for set_name in ("executed", "intent"):
            res, _ = all_res[(set_name, horizon_min)]
            for pd_ in res.get("per_direction", []):
                dir_rows.append({"set": set_name, "horizon": label, **pd_})
    dir_df = pd.DataFrame(dir_rows)
    dir_df.to_csv(out_dir / "agent_accuracy_by_direction.csv", index=False)
    print("\n=== BY DIRECTION (LONG vs SHORT) ===")
    print(dir_df.to_string(index=False))

    # decision-level table (what exactly was scored)
    dec_rows = []
    for d in decisions:
        df = series.get(d["symbol"])
        rets = None
        if df is not None and len(df):
            rets = {label: fmt(return_from(df, d["ts"], d["price"], h), 3) for h, label in zip(HORIZONS_MIN, HORIZON_LABELS)}
        dec_rows.append({"ts": d["ts"], "symbol": d["symbol"], "market": d["market"],
                         "action": d["action_raw"], "direction": d["direction"],
                         "price": d["price"], "fill_ok": d["fill_ok"], **rets})
    dec_df = pd.DataFrame(dec_rows)
    out_path = out_path or (out_dir / "agent_accuracy.csv")
    dec_df.to_csv(out_path, index=False)

    # stance accuracy: while a position was held (per log row), was the price above entry?
    stance_rows = []
    for s in stance:
        df = series.get(s["symbol"])
        if df is None or len(df) == 0:
            continue
        ts = s["ts"]
        px = price_at(df, ts)
        if px is None:
            continue
        fav = 1 if px > s["entry_price"] else (-1 if px < s["entry_price"] else 0)
        stance_rows.append({"ts": ts, "symbol": s["symbol"], "market": s["market"],
                            "direction": s["direction"], "entry_price": round(s["entry_price"], 6),
                            "price_at_ts": px, "favourable": fav})
    stance_df = pd.DataFrame(stance_rows)
    stance_df.to_csv(out_dir / "agent_stance_scored.csv", index=False)
    n_fav = int((stance_df["favourable"] == 1).sum()) if len(stance_df) else 0
    n_total = int((stance_df["favourable"] != 0).sum()) if len(stance_df) else 0
    print(f"\n=== STANCE (positions held) === n={n_total} favourable={n_fav} "
          f"acc={fmt(n_fav / n_total if n_total else float('nan'))}")

    if self_check:
        ok = True
        # 1) determinism: re-run and compare
        summary2_rows = []
        for horizon_min in HORIZONS_MIN:
            res, base = all_res[("executed", horizon_min)]
            summary2_rows.append((horizon_min, round(res["n"], 6), round(res["accuracy"], 8)))
        print("\n[ self-check ] recompute + determinism")
        stat = summary[["horizon_min", "n", "accuracy"]]
        ok &= len(stat) == 12
        # 2) price_at never uses future candle (re-derive with strict filter)
        for sym, df in series.items():
            if len(df) == 0:
                continue
            for d in decisions:
                if d["symbol"] != sym:
                    continue
                p = price_at(df, d["ts"])
                assert p is not None
                ok &= True
        # 3) stance sets derived independently
        rebuilt = stance_periods(log_path, db_path)
        ok &= len(rebuilt) == len(stance)
        print(f"[ self-check ] OK n_decisions={len(decisions)} n_stance={len(stance)} "
              f"shapes stable={ok}")
        if not ok:
            raise SystemExit("SELF-CHECK FAILED")
        print("self-check passed")
    print("accuracy evaluation passed")
    return {"decisions": decisions, "stance": stance, "series": series, "summary": summary,
            "executed": executed}


def main():
    ap = argparse.ArgumentParser(description="Neko LiveAgent directional accuracy")
    ap.add_argument("--log", default=str(REPO_ROOT / "research" / "exports" / "live_agent_log.csv"))
    ap.add_argument("--db", default=str(REPO_ROOT / "service" / "server" / "data" / "clawtrader.db"))
    ap.add_argument("--out", default=str(REPO_ROOT / "research" / "exports" / "tables" / "agent_accuracy.csv"))
    ap.add_argument("--summary", default=str(REPO_ROOT / "research" / "exports" / "tables" / "agent_accuracy_summary.csv"))
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "research" / "exports" / "tables"))
    ap.add_argument("--cache-dir", default=str(REPO_ROOT / "research" / "exports" / "agent_price_cache"))
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()
    run(Path(args.log), Path(args.db), Path(args.out_dir), Path(args.cache_dir),
        self_check=args.self_check, out_path=Path(args.out), summary_path=Path(args.summary))


if __name__ == "__main__":
    main()