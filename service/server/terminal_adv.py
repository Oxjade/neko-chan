"""Advanced mode of the terminal: the dense, many-panel view.

Every panel is computed from data the platform already has: live prices,
funding and open interest for 130-odd markets (Hyperliquid), 4-hour candles,
the Fear & Greed index, the central-bank forex rates, and the saved research
tables. Nothing is filled in for effect: there is no news or economic
calendar because there is no source for one.

Each panel fails on its own: a missing source empties that panel, not the page.
"""

from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np

import crypto_mood
import desk
import forex_weekly
import market_data as md
import terminal
import weekly_calls as wc

WORKSHEET = md.CRYPTO + tuple(f"xyz:{s}" for s in (
    "NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "AMD", "COIN", "MSTR",
    "SP500", "XYZ100", "GOLD", "SILVER", "CL", "EUR", "GBP", "JPY"))
PERFORMANCE = ("BTC", "ETH", "SOL", "SUI", "xyz:NVDA", "xyz:SP500", "xyz:GOLD")
CORRELATION = ("BTC", "ETH", "SOL", "SUI", "xyz:NVDA", "xyz:SP500", "xyz:GOLD", "xyz:EUR")
BARS_PER_DAY = 6
MIN_VOLUME_USD = 250_000          # ignore markets too thin for their day change to mean much
ANNUAL = math.sqrt(BARS_PER_DAY * 365)


def _soft(fn, default):
    try:
        return fn()
    except Exception:              # one panel must never take the page down
        return default


def movers(live: dict) -> dict:
    """Most up and most down today, per asset class."""
    out = {}
    for cls in md.CLASSES:
        rows = [r for r in live.values() if r["class"] == cls and r["day_change_pct"] is not None
                and r["day_volume_usd"] >= MIN_VOLUME_USD]
        rows.sort(key=lambda r: r["day_change_pct"])
        pick = lambda r: {"symbol": r["symbol"], "label": r["label"], "price": r["price"],
                          "day_change_pct": r["day_change_pct"]}
        # with only a couple of markets in a class, the same one must not appear on both sides
        out[cls] = {"up": [pick(r) for r in reversed(rows[-3:]) if r["day_change_pct"] > 0],
                    "down": [pick(r) for r in rows[:3] if r["day_change_pct"] < 0],
                    "count": len(rows)}
    return out


def breadth(live: dict) -> dict:
    """How many markets are up, down or flat today, per asset class."""
    out = {}
    for cls in md.CLASSES:
        changes = [r["day_change_pct"] for r in live.values()
                   if r["class"] == cls and r["day_change_pct"] is not None]
        out[cls] = {"up": sum(c > 0.1 for c in changes), "down": sum(c < -0.1 for c in changes),
                    "flat": sum(abs(c) <= 0.1 for c in changes)}
    return out


def _stats(symbol: str) -> dict:
    """Slow-moving figures for one market, from its 4-hour candles."""
    row = desk.market_signal(symbol)
    close = md.candles(symbol)["close"]
    returns = np.diff(np.log(close))

    def change(bars):
        return float((close[-1] / close[-bars - 1] - 1) * 100) if len(close) > bars else None

    def vol(bars):
        return float(returns[-bars:].std(ddof=1) * ANNUAL * 100) if len(returns) >= bars else None
    return {
        "symbol": symbol, "label": row["label"], "class": row["class"],
        "week_pct": change(7 * BARS_PER_DAY), "month_pct": change(30 * BARS_PER_DAY),
        "four_week_pct": change(28 * BARS_PER_DAY),
        "trend": row["trend"], "trend_days": row["trend_days"], "atr_pct": row["atr_pct"],
        "vol_7d": vol(7 * BARS_PER_DAY), "vol_30d": vol(30 * BARS_PER_DAY),
        "signal": row["signal"]["direction"] if not row["signal"].get("muted") else None,
        "signal_state": row["signal"]["state"],
        "record_wins": row["record"]["wins"], "record_trades": row["record"]["trades"],
        "record_avg_pct": row["record"]["expectancy"],
    }


def worksheet(live: dict) -> list[dict]:
    def slow():
        def one(symbol):
            return _soft(lambda: _stats(symbol), None)
        with ThreadPoolExecutor(max_workers=8) as pool:
            return [r for r in pool.map(one, WORKSHEET) if r]
    rows = md.cached("terminal:worksheet", 120, slow)
    out = []
    for r in rows:
        now = live.get(r["symbol"]) or {}
        out.append({**r, "price": now.get("price"), "day_change_pct": now.get("day_change_pct"),
                    "funding_apr": (now.get("funding_hourly") or 0.0) * 24 * 365 * 100,
                    "open_interest_usd": now.get("open_interest_usd"),
                    "day_volume_usd": now.get("day_volume_usd")})
    return out


def performance() -> dict:
    """Thirty days of each market, rebased to 100, one point every twelve hours."""
    series = {}
    for symbol in PERFORMANCE:
        close = _soft(lambda: md.candles(symbol)["close"], None)
        if close is None or len(close) < 30 * BARS_PER_DAY + 1:
            continue
        window = close[-30 * BARS_PER_DAY - 1::3]
        series[md.label(symbol)] = [float(v / window[0] * 100) for v in window]
    return series


def correlation() -> dict:
    """How closely the markets moved together over the last thirty days (4-hour moves)."""
    frames = {}
    for symbol in CORRELATION:
        bars = _soft(lambda: md.candles(symbol), None)
        if bars is not None:
            frames[md.label(symbol)] = dict(zip(bars["t"].tolist()[-30 * BARS_PER_DAY - 1:],
                                                bars["close"].tolist()[-30 * BARS_PER_DAY - 1:]))
    labels = list(frames)
    if len(labels) < 2:
        return {"labels": [], "matrix": []}
    shared = sorted(set.intersection(*(set(f) for f in frames.values())))
    if len(shared) < 30:
        return {"labels": [], "matrix": []}
    prices = np.array([[frames[label][t] for t in shared] for label in labels])
    matrix = np.corrcoef(np.diff(np.log(prices), axis=1))
    return {"labels": labels, "matrix": [[float(v) for v in row] for row in matrix], "bars": len(shared)}


def signal_matrix(week: dict) -> list[dict]:
    """Per coin and side: is it called, at what leverage, and how it has done."""
    config = wc.calls_config()
    rows = []
    for coin in week["coins"]:
        for side in ("long", "short"):
            c = config[coin["coin"]][side]
            rows.append({
                "coin": coin["coin"], "side": side, "allowed": c["allowed"],
                "leverage": c["suggested_leverage"], "active": coin["side"] == side,
                "pointed": coin["signal"] == side,
                "right_first": c["first_half"]["right"], "right_second": c["second_half"]["right"],
                "avg_first": c["first_half"]["avg_pct"], "avg_second": c["second_half"]["avg_pct"],
                "weeks": (c["first_half"]["coin_weeks"] or 0) + (c["second_half"]["coin_weeks"] or 0),
            })
    return rows


def leverage_table() -> dict:
    """Share of traded weeks in which the weekly stake was liquidated, by leverage."""
    config = wc.calls_config()
    levels = sorted({int(k[:-1]) for c in config.values() for s in c.values() for k in s["by_leverage"]})
    rows = []
    for coin, sides in config.items():
        for side, c in sides.items():
            if not c["allowed"]:
                continue
            rows.append({"coin": coin, "side": side, "suggested": c["suggested_leverage"],
                         "liquidated": {str(l): c["by_leverage"].get(f"{l}x", {}).get("liquidated") for l in levels},
                         "avg": {str(l): c["by_leverage"].get(f"{l}x", {}).get("avg_per_week") for l in levels}})
    return {"levels": levels, "rows": rows}


def risk(positions: list[dict], stake: float) -> dict:
    rows = []
    for p in positions:
        open_ = p["status"] == "open"
        at_stop = (p["share"] * p["leverage"] * (abs(p["stop"] - p["entry"]) / p["entry"] + 2 * wc.FEE_PER_SIDE)
                   if p.get("stop") else p["share"])
        rows.append({
            "coin": p["coin"], "side": p["side"], "status": p["status"], "leverage": p["leverage"],
            "exposure_usd": p["position_usd"] if open_ else 0.0, "pnl_usd": p["pnl_usd"],
            "stop_distance_pct": p.get("stop_distance_pct"),
            "liquidation_distance_pct": (abs(p["liquidation"] - p["last"]) / p["last"] * 100
                                         if open_ and p.get("liquidation") else None),
            "loss_at_stop_usd": min(at_stop, p["share"]) if open_ else 0.0,
            "normal_week_pct": p.get("normal_week_pct"),
        })
    exposure = sum(r["exposure_usd"] for r in rows)
    return {"rows": rows, "exposure_usd": exposure, "exposure_of_stake": exposure / stake if stake else 0.0,
            "worst_case_usd": -sum(r["loss_at_stop_usd"] for r in rows),
            "in_cash_usd": stake - sum(p["share"] for p in positions if p["status"] == "open")}


def funding(live: dict) -> list[dict]:
    rows = [{"symbol": r["symbol"], "label": r["label"], "funding_apr": r["funding_hourly"] * 24 * 365 * 100,
             "open_interest_usd": r["open_interest_usd"], "day_volume_usd": r["day_volume_usd"]}
            for r in live.values() if r["class"] == "crypto"]
    return sorted(rows, key=lambda r: -abs(r["funding_apr"]))


def mood_history() -> list[dict]:
    values = crypto_mood.history()
    return [{"date": d.isoformat(), "value": v} for d, v in sorted(values.items())][-120:]


def exits() -> dict:
    rule = wc.exit_rule()
    return {"stop": rule["stop"], "targets": rule["targets"], "take_profit": rule["take_profit"],
            "with": rule["with"], "holding": rule["holding"],
            "by_call": {k: v["all"] for k, v in (rule.get("by_call") or {}).items()}}


def snapshot(stake: float, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    live = md.catalogue(max_age=4)
    base = terminal.snapshot(stake, now)
    week = wc.this_week()
    slow = lambda key, ttl, fn, default: _soft(lambda: md.cached(key, ttl, fn), default)
    return {
        "now": now.isoformat(),
        "movers": _soft(lambda: movers(live), {}),
        "breadth": _soft(lambda: breadth(live), {}),
        "worksheet": _soft(lambda: worksheet(live), []),
        "performance": slow("terminal:performance", 300, performance, {}),
        "correlation": slow("terminal:correlation", 300, correlation, {"labels": [], "matrix": []}),
        "signals": _soft(lambda: signal_matrix(week), []),
        "leverage": _soft(leverage_table, {"levels": [], "rows": []}),
        "risk": _soft(lambda: risk(base["positions"], stake), {"rows": []}),
        "scoreboard": slow(f"terminal:scoreboard:{stake}", 300, lambda: wc.scoreboard(stake, wc.SUGGESTED), None),
        "funding": _soft(lambda: funding(live), []),
        "forex": slow("terminal:forex", 3600, lambda: forex_weekly.overview()["pairs"], []),
        "mood_history": slow("terminal:mood", 3600, mood_history, []),
        "exits": _soft(exits, None),
        "markets": len(live),
    }
