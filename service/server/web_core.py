"""Read models for the web app: trades, performance statistics and the
go-live evidence gate, computed from the paper-trading tables.

Pure functions plus a thin read-only SQLite layer. No Telegram, no network.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORT_DIR = REPO_ROOT / "research" / "exports"
REPLAY_PATH = EXPORT_DIR / "tables" / "live_engine_replay.json"

# Evidence gate: what a paper record must show before live money is defensible.
GATE_MIN_TRADES = 100
GATE_MAX_DRAWDOWN_PCT = 15.0

_ENTRY_SIDES = {"buy", "short"}
_EPS = 1e-12


def registry_path() -> Path:
    return Path(os.getenv("TG_REGISTRY_PATH",
                          str(REPO_ROOT / "service" / "tg_bot" / "registry.db")))


# ----------------------------------------------------------------- statistics

def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a win rate. Honest at small n."""
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def pair_trades(orders: list[dict]) -> list[dict]:
    """Turn the fill log into closed round-trip trades.

    One open lot per symbol (the paper store allows one position per symbol).
    Partial exits accumulate into the same trade; the trade closes when the
    lot is flat. Entry fees are attributed pro rata to the quantity closed.
    """
    lots: dict[str, dict] = {}
    trades: list[dict] = []
    for o in sorted(orders, key=lambda r: (r["created_at"], r["id"])):
        sym = o["symbol"]
        qty = float(o["qty"])
        price = float(o["price"])
        fee = float(o["fee"] or 0.0)
        if o["side"] in _ENTRY_SIDES:
            lot = lots.get(sym)
            if lot and lot["direction"] == o["direction"]:
                total = lot["qty_open"] + qty
                lot["entry"] = (lot["entry"] * lot["qty_open"] + price * qty) / total
                lot["qty_open"] = total
                lot["qty_total"] += qty
                lot["entry_fee_left"] += fee
            else:
                lots[sym] = {
                    "symbol": sym, "direction": o["direction"], "entry": price,
                    "qty_open": qty, "qty_total": qty, "entry_fee_left": fee,
                    "leverage": float(o.get("leverage") or 1.0),
                    "opened_at": o["created_at"],
                    "gross": 0.0, "fees": 0.0, "exit_value": 0.0, "exit_qty": 0.0,
                }
            continue
        lot = lots.get(sym)
        if not lot or lot["qty_open"] <= _EPS:
            continue  # exit with no recorded entry: ignore, never invent a trade
        q = min(qty, lot["qty_open"])
        sign = 1.0 if lot["direction"] == "long" else -1.0
        entry_fee = lot["entry_fee_left"] * (q / lot["qty_open"])
        lot["gross"] += q * (price - lot["entry"]) * sign
        lot["fees"] += fee + entry_fee
        lot["entry_fee_left"] -= entry_fee
        lot["exit_value"] += q * price
        lot["exit_qty"] += q
        lot["qty_open"] -= q
        if lot["qty_open"] <= max(_EPS, lot["qty_total"] * 1e-9):
            notional = lot["qty_total"] * lot["entry"]
            net = lot["gross"] - lot["fees"]
            trades.append({
                "symbol": sym,
                "direction": lot["direction"],
                "opened_at": lot["opened_at"],
                "closed_at": o["created_at"],
                "qty": lot["qty_total"],
                "entry": lot["entry"],
                "exit": lot["exit_value"] / lot["exit_qty"],
                "leverage": lot["leverage"],
                "notional": notional,
                "gross": lot["gross"],
                "fees": lot["fees"],
                "net": net,
                "net_pct": (net / notional * 100.0) if notional else 0.0,
            })
            del lots[sym]
    return trades


def equity_curve(trades: list[dict], starting: float) -> list[dict]:
    """Realised equity after each closed trade (fees included)."""
    points = []
    equity = starting
    for t in trades:
        equity += t["net"]
        points.append({"t": t["closed_at"], "equity": equity})
    return points


def max_drawdown_pct(curve: list[dict], starting: float) -> float:
    peak = starting
    worst = 0.0
    for p in curve:
        peak = max(peak, p["equity"])
        if peak > 0:
            worst = max(worst, (peak - p["equity"]) / peak * 100.0)
    return worst


def performance(trades: list[dict], starting: float) -> dict:
    """Win rate with its uncertainty, payoff, and the break-even win rate the
    measured payoff implies. Everything is net of fees."""
    n = len(trades)
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    lo, hi = wilson_interval(len(wins), n)
    avg_win = sum(t["net"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = -sum(t["net"] for t in losses) / len(losses) if losses else 0.0
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    breakeven = None
    if avg_win > 0 and avg_loss > 0:
        breakeven = avg_loss / (avg_win + avg_loss)
    curve = equity_curve(trades, starting)
    net = sum(t["net"] for t in trades)
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate": (len(wins) / n) if n else None,
        "win_rate_low": lo if n else None,
        "win_rate_high": hi if n else None,
        "breakeven_win_rate": breakeven,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": (avg_win / avg_loss) if avg_loss > 0 else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy": (net / n) if n else None,
        "net": net,
        "gross": sum(t["gross"] for t in trades),
        "fees": sum(t["fees"] for t in trades),
        "return_pct": (net / starting * 100.0) if starting else 0.0,
        "max_drawdown_pct": max_drawdown_pct(curve, starting),
        "starting": starting,
        "curve": curve,
    }


def evidence_gate(perf: dict) -> dict:
    """The checks a paper record must pass before live trading is defensible.

    The win-rate check is on the LOWER bound of the confidence interval: a
    lucky streak does not pass it, only a large enough sample does.
    """
    n = perf["trades"]
    be = perf["breakeven_win_rate"]
    low = perf["win_rate_low"]
    checks = [
        {
            "key": "sample",
            "label": f"At least {GATE_MIN_TRADES} closed trades",
            "passed": n >= GATE_MIN_TRADES,
            "detail": f"{n} of {GATE_MIN_TRADES}",
        },
        {
            "key": "edge",
            "label": "Win rate beats break-even even at the low end of its range",
            "passed": bool(n and be is not None and low is not None and low > be),
            "detail": (f"low end {low * 100:.1f}% vs break-even {be * 100:.1f}%"
                       if n and be is not None and low is not None
                       else "needs both winning and losing trades"),
        },
        {
            "key": "expectancy",
            "label": "Average trade is profitable after fees",
            "passed": bool(n and (perf["expectancy"] or 0) > 0),
            "detail": (f"{perf['expectancy']:+.2f} USD per trade" if n else "no trades yet"),
        },
        {
            "key": "drawdown",
            "label": f"Worst drawdown under {GATE_MAX_DRAWDOWN_PCT:.0f}%",
            "passed": bool(n and perf["max_drawdown_pct"] < GATE_MAX_DRAWDOWN_PCT),
            "detail": (f"{perf['max_drawdown_pct']:.1f}%" if n else "no trades yet"),
        },
    ]
    return {"passed": all(c["passed"] for c in checks), "checks": checks}


# ------------------------------------------------------------------- reading

def _connect() -> sqlite3.Connection | None:
    path = registry_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    conn = _connect()
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []  # table not created yet
    finally:
        conn.close()


_BOT_COLUMNS = ("id, tg_id, bot_name, agent_name, leverage, interval_sec, risk_profile, "
                "is_running, paused, pid, last_error, trader_type, chain, network, "
                "watchlist, trading_mode, created_at")


def list_bots() -> list[dict]:
    return _rows(f"SELECT {_BOT_COLUMNS} FROM bots ORDER BY id")


def get_bot(bot_id: int) -> dict | None:
    rows = _rows(f"SELECT {_BOT_COLUMNS} FROM bots WHERE id = ?", (bot_id,))
    return rows[0] if rows else None


def paper_portfolio(bot_id: int) -> dict | None:
    rows = _rows("SELECT * FROM paper_portfolios WHERE bot_id = ?", (bot_id,))
    return rows[0] if rows else None


def paper_orders(bot_id: int) -> list[dict]:
    return _rows("SELECT * FROM paper_orders WHERE bot_id = ? ORDER BY created_at, id", (bot_id,))


def paper_positions(bot_id: int) -> list[dict]:
    return _rows("SELECT * FROM paper_positions WHERE bot_id = ? ORDER BY id", (bot_id,))


def latest_prices(bot_id: int) -> dict[str, dict]:
    """Last price the agent saw per symbol, from its decision cache."""
    path = EXPORT_DIR / f"live_agent_cache_bot{bot_id}.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for key, row in cache.items():
        try:
            price = float(row.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if key != "__hold__" and price > 0:
            out[key.upper()] = {"price": price, "ts": row.get("ts")}
    return out


def recent_decisions(bot_id: int, limit: int = 50) -> list[dict]:
    """Tail of the agent's append-only decision log."""
    path = EXPORT_DIR / f"live_agent_log_bot{bot_id}.csv"
    if not path.exists():
        return []
    tail: deque = deque(maxlen=limit)
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                tail.append(row)
    except (OSError, csv.Error):
        return []
    out = []
    for row in reversed(tail):
        out.append({
            "ts": row.get("ts"),
            "symbol": row.get("symbol") or "",
            "direction": row.get("direction") or "",
            "action": row.get("action") or "hold",
            "price": _num(row.get("price")),
            "stop_pct": _num(row.get("stop_pct")),
            "take_pct": _num(row.get("take_pct")),
            "leverage": _num(row.get("leverage")),
            "filled": str(row.get("fill_ok", "")).strip().lower() == "true",
            "reasoning": (row.get("reasoning") or "")[:600],
            "error": (row.get("error") or "")[:300],
        })
    return out


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def open_positions(bot_id: int) -> list[dict]:
    prices = latest_prices(bot_id)
    out = []
    for p in paper_positions(bot_id):
        mark = prices.get(p["symbol"].upper())
        price = mark["price"] if mark else p["entry_price"]
        sign = 1.0 if p["direction"] == "long" else -1.0
        out.append({
            "symbol": p["symbol"],
            "direction": p["direction"],
            "qty": p["qty"],
            "entry": p["entry_price"],
            "mark": price,
            "marked_at": mark["ts"] if mark else None,
            "leverage": p["leverage"],
            "stop": p["stop_loss"],
            "target": p["take_profit"],
            "unrealized": p["qty"] * (price - p["entry_price"]) * sign,
            "opened_at": p["opened_at"],
        })
    return out


def bot_report(bot_id: int) -> dict:
    """Everything the console shows for one paper bot."""
    portfolio = paper_portfolio(bot_id)
    starting = float(portfolio["starting"]) if portfolio else 1000.0
    trades = pair_trades(paper_orders(bot_id))
    perf = performance(trades, starting)
    positions = open_positions(bot_id)
    cash = float(portfolio["cash"]) if portfolio else starting
    margin = sum(p["qty"] * p["entry"] / max(p["leverage"] or 1.0, 1.0) for p in positions)
    return {
        "performance": perf,
        "gate": evidence_gate(perf),
        "positions": positions,
        "account": {
            "cash": cash,
            "margin_in_use": margin,
            "unrealized": sum(p["unrealized"] for p in positions),
            "equity": cash + margin + sum(p["unrealized"] for p in positions),
            "starting": starting,
        },
        "trades": list(reversed(trades))[:200],
    }


def combined_report(bot_ids: list[int]) -> dict:
    """Pooled record across bots, for the public page."""
    trades: list[dict] = []
    starting = 0.0
    for bot_id in bot_ids:
        portfolio = paper_portfolio(bot_id)
        starting += float(portfolio["starting"]) if portfolio else 1000.0
        trades.extend(pair_trades(paper_orders(bot_id)))
    trades.sort(key=lambda t: t["closed_at"])
    perf = performance(trades, starting or 1000.0)
    return {"performance": perf, "gate": evidence_gate(perf),
            "trades": list(reversed(trades))[:25]}


def replay_evidence() -> dict | None:
    """Summary of research/scripts/replay_live_engine.py, if it has been run."""
    try:
        data = json.loads(REPLAY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    summary = data.get("summary") or {}
    return {
        "generated_at": data.get("generated_at"),
        "data": data.get("data"),
        "trades": summary.get("trades_closed"),
        "wins": summary.get("wins"),
        "win_rate": summary.get("win_rate"),
        "win_rate_low": (summary.get("win_rate_ci95") or [None, None])[0],
        "win_rate_high": (summary.get("win_rate_ci95") or [None, None])[1],
        "model_p_win": summary.get("avg_model_p_win"),
        "by_cost": summary.get("by_cost"),
        "random_baseline": data.get("random_direction_baseline"),
    }


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
