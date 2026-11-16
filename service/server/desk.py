"""The desk: signals, each market's record, and what the record has taught.

One rule produces every signal: on 4-hour bars, long when the 10-day average
crosses above the 40-day average, short on the opposite cross, with a stop
4 ATR from entry, held until the opposite cross. It is the rule that survived
research/scripts/strategy_search.py on crypto.

For each market the same rule is replayed over that market's own history, so
a signal is always shown beside what it has actually done there. Where a
direction has lost money on a market it is muted: no signal is raised and the
paper account does not trade it. Because the replay is redone as new bars
arrive, a muted direction comes back if it starts working again. That is all
"learning" means here: keeping score and acting on it.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np

import market_data as md
import web_core

FAST_BARS, SLOW_BARS = 60, 240
ATR_BARS = 14
STOP_ATR = 4.0
COST_PCT = 0.13                 # round trip, exchange fees and slippage
NEW_SIGNAL_BARS = 6             # a cross within the last 24 hours is "new"
MIN_TRADES_TO_JUDGE = 5
MAX_WATCHLIST = 30


def ema(values: np.ndarray, span: int) -> np.ndarray:
    k = 2.0 / (span + 1)
    out = np.empty(len(values))
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def atr_series(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = ATR_BARS) -> np.ndarray:
    prev = np.concatenate(([np.nan], close[:-1]))
    tr = np.fmax(high - low, np.fmax(np.abs(high - prev), np.abs(low - prev)))
    out = np.full(len(close), np.nan)
    for i in range(n - 1, len(close)):
        window = tr[i - n + 1:i + 1]
        if not np.isnan(window).any():
            out[i] = window.mean()
    return out


def replay(bars: dict, warmup: int = SLOW_BARS) -> dict:
    """Run the rule over the bars. Returns closed trades and the current state.

    Signal on a bar's close, entry at the next bar's open. A gap through the
    stop fills at the open. `warmup` bars are skipped so the averages have
    settled before the first trade.
    """
    o, h, l, c, t = bars["open"], bars["high"], bars["low"], bars["close"], bars["t"]
    up = ema(c, FAST_BARS) > ema(c, SLOW_BARS)
    atr = atr_series(h, l, c)
    n = len(c)
    trades, pos = [], None
    for i in range(max(1, warmup), n):
        if pos is not None:
            d = pos["dir"]
            exit_px = reason = None
            if (l[i] <= pos["stop"]) if d > 0 else (h[i] >= pos["stop"]):
                exit_px = min(o[i], pos["stop"]) if d > 0 else max(o[i], pos["stop"])
                reason = "stop"
            elif up[i - 1] != (d > 0):
                exit_px, reason = o[i], "reversal"
            if exit_px is not None:
                trades.append({
                    "direction": "long" if d > 0 else "short",
                    "opened_at": _iso(t[pos["i"]]), "closed_at": _iso(t[i]),
                    "entry": pos["px"], "exit": float(exit_px), "reason": reason,
                    "gross_pct": float((exit_px / pos["px"] - 1) * 100 * d),
                })
                pos = None
        crossed = i >= 2 and up[i - 1] != up[i - 2]
        if pos is None and crossed and not math.isnan(atr[i - 1]):
            d = 1 if up[i - 1] else -1
            pos = {"i": i, "dir": d, "px": float(o[i]), "stop": float(o[i] - d * STOP_ATR * atr[i - 1])}
    pending = None
    if pos is None and n >= 2 and up[-1] != up[-2] and not math.isnan(atr[-1]):
        pending = {"direction": "long" if up[-1] else "short",
                   "stop_distance": float(STOP_ATR * atr[-1])}
    held = 1
    while held < n and up[-held - 1] == up[-1]:
        held += 1
    return {
        "trades": trades, "open": pos, "pending": pending, "bars": n,
        "trend": "up" if up[-1] else "down", "trend_days": held * 4 / 24,
        "atr_pct": float(atr[-1] / c[-1] * 100) if not math.isnan(atr[-1]) else None,
        "last_close": float(c[-1]), "last_bar": _iso(t[-1]), "times": t,
    }


def _iso(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()


def _as_trades(rows: list[dict]) -> list[dict]:
    """Shape replay trades for web_core.performance (percent of position, net of cost)."""
    return [{"net": r["gross_pct"] - COST_PCT, "gross": r["gross_pct"], "fees": COST_PCT,
             "closed_at": r["closed_at"]} for r in rows]


def trust(rows: list[dict], direction: str) -> dict:
    """Has this direction earned its place on this market?"""
    mine = [r for r in rows if r["direction"] == direction]
    n = len(mine)
    total = sum(r["gross_pct"] - COST_PCT for r in mine)
    wins = sum(1 for r in mine if r["gross_pct"] - COST_PCT > 0)
    if n < MIN_TRADES_TO_JUDGE:
        state = "unproven"
    elif total <= 0:
        state = "muted"
    else:
        state = "trusted"
    return {"state": state, "trades": n, "wins": wins, "total_net_pct": total}


def market_signal(symbol: str, live: dict | None = None) -> dict:
    """Everything the desk shows for one market."""
    live = live or md.catalogue().get(symbol)
    if not live:
        raise md.DataError(f"{md.label(symbol)} is not available.")
    run = replay(md.candles(symbol))
    price = live["price"]
    trust_by = {d: trust(run["trades"], d) for d in ("long", "short")}
    perf = web_core.performance(_as_trades(run["trades"]), 100.0)
    perf.pop("curve", None)

    signal = {"state": "waiting", "direction": None}
    if run["pending"]:
        d = run["pending"]["direction"]
        sign = 1 if d == "long" else -1
        signal = {"state": "new", "direction": d, "entry": price,
                  "stop": price - sign * run["pending"]["stop_distance"],
                  "since": run["last_bar"], "change_pct": 0.0}
    elif run["open"]:
        pos = run["open"]
        d = "long" if pos["dir"] > 0 else "short"
        bars_ago = run["bars"] - pos["i"]
        signal = {"state": "new" if bars_ago <= NEW_SIGNAL_BARS else "active", "direction": d,
                  "entry": pos["px"], "stop": pos["stop"], "since": _iso(run["times"][pos["i"]]),
                  "change_pct": (price / pos["px"] - 1) * 100 * pos["dir"]}
    elif run["trades"] and run["trades"][-1]["reason"] == "stop":
        signal = {"state": "stopped", "direction": None, "since": run["trades"][-1]["closed_at"]}
    if signal["direction"] and trust_by[signal["direction"]]["state"] == "muted":
        signal["muted"] = True
    if signal.get("stop"):
        signal["stop_pct"] = abs(signal["entry"] - signal["stop"]) / signal["entry"] * 100

    return {
        **live,
        "trend": run["trend"], "trend_days": run["trend_days"], "atr_pct": run["atr_pct"],
        "history_days": run["bars"] * 4 / 24,
        "signal": signal, "trust": trust_by, "record": perf,
        "recent_trades": list(reversed(run["trades"]))[:12],
        "funding_daily_pct": live["funding_hourly"] * 24 * 100,
    }


def watchlist_signals(symbols: list[str]) -> list[dict]:
    live = md.catalogue()

    def one(symbol):
        try:
            return market_signal(symbol, live.get(symbol))
        except md.DataError as exc:
            base = live.get(symbol) or {"symbol": symbol, "label": md.label(symbol),
                                        "class": md.asset_class(symbol), "price": None}
            return {**base, "error": str(exc)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, symbols))


def lessons(rows: list[dict]) -> list[dict]:
    """What the record has taught, in plain sentences."""
    out = []
    for row in rows:
        if row.get("error"):
            continue
        for direction in ("long", "short"):
            t = row["trust"][direction]
            if t["state"] == "muted":
                out.append({
                    "symbol": row["symbol"], "label": row["label"], "direction": direction,
                    "text": (f"{direction.capitalize()}s on {row['label']} are muted: "
                             f"{t['wins']} of {t['trades']} won, {t['total_net_pct']:+.1f}% in total."),
                })
    return out


def allowed(rows: list[dict]):
    """Filter for the paper trader: skip directions the record has muted."""
    muted = {(l["symbol"], l["direction"]) for l in lessons(rows)}
    return lambda symbol, direction: (symbol, direction) not in muted


# ---- adapters so the paper trader (service/agent/trend_agent.py) can use this data

def bars_for_agent(symbol: str) -> list[dict]:
    b = md.candles(symbol)
    return [{"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c)}
            for t, o, h, l, c in zip(b["t"], b["open"], b["high"], b["low"], b["close"])]


def mids_for_agent() -> dict[str, float]:
    return {symbol: row["price"] for symbol, row in md.catalogue().items()}
