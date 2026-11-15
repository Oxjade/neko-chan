"""The live terminal: model positions for this week's calls, what to do about
them right now, a recommendation per market, and chart data.

Everything here is derived from the tested rules:

  calls      weekly_calls (long, short or stay out; suggested leverage)
  stop       one normal week from entry (crypto_exits.py)
  in profit  hold to Monday. Moving the stop to break-even made no real
             difference in testing and trailing it closely made results
             worse, so the terminal never tells you to tighten a stop.
  target     none by default; a far one (three normal weeks) is optional
  exit       close at Monday 00:00 UTC and take the new call

The "positions" are model positions: what the user's weekly amount would be
doing had the calls been followed from Monday's open. The terminal does not
know what the user actually holds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import desk
import market_data as md
import weekly_calls as wc

DASHBOARD = ("BTC", "ETH", "SOL", "SUI", "xyz:NVDA", "xyz:SP500", "xyz:GOLD", "xyz:EUR")
NEAR_STOP = 0.25            # warn when less than a quarter of the stop distance is left
EMA_DAYS = (10, 40)
CHART_BARS = {"1h": 240, "4h": 240, "1d": 240}
BARS_PER_DAY = {"1h": 24, "4h": 6, "1d": 1}


def _utc(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _money(v: float) -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}"


def _px(v: float) -> str:
    return f"{v:,.0f}" if v >= 1000 else (f"{v:,.2f}" if v >= 10 else f"{v:,.4f}")


def position_state(coin_row: dict, days: list[dict], price: float, share: float) -> dict:
    """Where a model position stands. `days` are this week's daily candles so far
    (Monday first, the forming day included)."""
    side, lev = coin_row["side"], coin_row["leverage"]
    sign = 1.0 if side == "long" else -1.0
    entry, stop = coin_row["entry"], coin_row.get("stop_price")
    target, liq = coin_row.get("target_price"), coin_row.get("liquidation_price")
    status, closed_at = "open", None
    # Inside one day the order of events is unknown, so the stop is counted before the take profit.
    for day in days:
        worst = day["low"] if side == "long" else day["high"]
        best = day["high"] if side == "long" else day["low"]
        if liq and (worst - liq) * sign <= 0:
            status, closed_at = "liquidated", liq
            break
        if stop and (worst - stop) * sign <= 0:
            status, closed_at = "stopped", stop
            break
        if target and (best - target) * sign >= 0:
            status, closed_at = "target", target
            break
    mark = closed_at if closed_at else price
    move = (mark / entry - 1) * sign
    pnl = -share if status == "liquidated" else share * lev * (move - 2 * wc.FEE_PER_SIDE)
    out = {
        "coin": coin_row["coin"], "name": coin_row["name"], "side": side, "leverage": lev,
        "share": share, "position_usd": share * lev, "entry": entry, "last": price,
        "stop": stop, "target": target, "liquidation": liq, "status": status,
        "move_pct": move * 100, "pnl_usd": max(pnl, -share), "pnl_pct": max(pnl, -share) / share * 100,
        "normal_week_pct": coin_row.get("normal_week_pct"),
    }
    if target and status == "open":
        out["target_distance_pct"] = (target - price) * sign / price * 100
    if stop and status == "open":
        total = abs(stop - entry)
        left = (stop - price) * -sign             # positive while the stop has not been reached
        out["stop_distance_pct"] = left / price * 100
        out["stop_room_left"] = max(0.0, min(1.5, left / total)) if total else None
    return out


def advice(positions: list[dict], week: dict, now: datetime, too_small: list[dict] | None = None) -> list[dict]:
    """What to do right now, most urgent first."""
    items = []

    def add(level, key, title, text, symbol=None):
        items.append({"level": level, "key": key, "title": title, "text": text, "symbol": symbol})

    close_at = datetime.fromisoformat(week["next_call"]).replace(tzinfo=timezone.utc)
    opened_at = datetime.fromisoformat(week["week_of"]).replace(tzinfo=timezone.utc)
    hours_left = (close_at - now).total_seconds() / 3600
    live_positions = [p for p in positions if p["status"] == "open"]

    for s in too_small or []:
        add("warn", f"small:{s['coin']}", f"{s['name']}: too small to place",
            f"The call is {s['side']} {s['name']} at {s['leverage']}x, but your share makes a "
            f"${s['position_usd']:.2f} position and the exchange's smallest order is ${wc.MIN_ORDER_USD:.0f}. "
            f"Set the weekly amount to at least ${s['min_weekly_amount']:.0f}, or skip {s['name']}.", s["coin"])
    for p in positions:
        tag = f"{p['side']} {p['name']}"
        if p["status"] == "liquidated":
            add("critical", f"liq:{p['coin']}", f"{p['name']}: liquidated",
                f"The {tag} was liquidated near {_px(p['liquidation'])}. That share is gone. Do not re-enter this week.",
                p["coin"])
        elif p["status"] == "stopped":
            add("critical", f"stop:{p['coin']}", f"{p['name']}: stop hit, trade closed",
                f"The {tag} hit its stop at {_px(p['stop'])} ({_money(p['pnl_usd'])}). It is closed for the week. "
                f"Do not re-enter or move the stop after the fact.", p["coin"])
        elif p["status"] == "target":
            add("good", f"target:{p['coin']}", f"{p['name']}: take profit hit, trade closed",
                f"The {tag} reached its take profit at {_px(p['target'])} ({_money(p['pnl_usd'])}). "
                f"It is closed for the week. Do not re-enter; the next call is Monday.", p["coin"])
        else:
            room = p.get("stop_room_left")
            normal = p.get("normal_week_pct") or 0
            if room is not None and room <= NEAR_STOP:
                add("warn", f"near:{p['coin']}", f"{p['name']}: close to the stop",
                    f"Price {_px(p['last'])} is {p['stop_distance_pct']:.1f}% from the stop at {_px(p['stop'])}. "
                    f"Leave the stop where it is and let it work.", p["coin"])
            elif normal and p["move_pct"] >= normal and not p.get("target"):
                why = ("There is no take profit on longs, because in testing every nearer target made longs "
                       "worse: rises tend to keep going." if p["side"] == "long"
                       else "No take profit is set for this side.")
                add("good", f"profit:{p['coin']}", f"{p['name']}: in profit, hold",
                    f"The {tag} is up {p['move_pct']:.1f}% ({_money(p['pnl_usd'])}), more than a normal week. "
                    f"Hold to Monday. {why}", p["coin"])
            elif p["move_pct"] >= 0:
                plan = (f"Take profit at {_px(p['target'])}, {p['target_distance_pct']:.1f}% away. "
                        if p.get("target") else "")
                add("info", f"hold:{p['coin']}", f"{p['name']}: on track",
                    f"The {tag} is up {p['move_pct']:.1f}% ({_money(p['pnl_usd'])}). No action. {plan}"
                    f"Stop at {_px(p['stop'])}." if p["stop"] else
                    f"The {tag} is up {p['move_pct']:.1f}% ({_money(p['pnl_usd'])}). No action.", p["coin"])
            else:
                add("info", f"down:{p['coin']}", f"{p['name']}: down, inside normal swings",
                    f"The {tag} is down {abs(p['move_pct']):.1f}% ({_money(p['pnl_usd'])}), "
                    f"{p['stop_distance_pct']:.1f}% from the stop. No action: the stop is the exit." if p["stop"] else
                    f"The {tag} is down {abs(p['move_pct']):.1f}% ({_money(p['pnl_usd'])}). No action.", p["coin"])

    if live_positions and 0 < hours_left <= 24:
        names = " and ".join(p["name"] for p in live_positions)
        add("warn" if hours_left <= 3 else "info", "close-soon", "Weekly close is coming",
            f"Close {names} at 00:00 UTC Monday, in {int(hours_left)}h {int(hours_left * 60 % 60):02d}m. "
            f"Then take the new call.")
    if live_positions and (now - opened_at) > timedelta(hours=24):
        add("info", "late", "Not in yet? Wait for Monday",
            "These trades were opened at Monday's open. Entering part-way through the week was not tested, "
            "so if you are not already in, skip to the next call.")
    for c in week["coins"]:
        if c["side"]:
            continue
        if c["skipped"]:
            add("info", f"out:{c['coin']}", f"{c['name']}: stay out",
                f"The rule points {c['signal']}, but going {c['signal']} on {c['name']} did not hold up in testing.",
                c["coin"])
        else:
            add("info", f"out:{c['coin']}", f"{c['name']}: stay out",
                "Mood and the last four weeks do not point the same way, so there is no call.", c["coin"])
    preview = week.get("preview")
    if preview and 0 < hours_left <= 48:
        calls = [f"{c['side']} {c['name']} at {c['leverage']}x" for c in preview["coins"] if c["side"]]
        add("info", "preview", "Preview of Monday's call",
            ("If nothing changes: " + " and ".join(calls) + ".") if calls
            else "If nothing changes: stay out of everything.")
    order = {"critical": 0, "warn": 1, "good": 2, "info": 3}
    return sorted(items, key=lambda i: order[i["level"]])


def recommendation(symbol: str, row: dict, week: dict) -> dict:
    """A reading for one market: which way the tested signals lean, and whether there is a call."""
    coin = next((c for c in week["coins"] if c["coin"] == symbol), None)
    votes = []
    if row.get("trend"):
        votes.append(("10-day against 40-day average", 1 if row["trend"] == "up" else -1))
    if coin:
        votes.append(("Market mood", 1 if coin["mood_greedy"] else -1))
        votes.append(("Last four weeks", 1 if coin["four_week_change_pct"] > 0 else -1))
    score = sum(v for _, v in votes)
    lean = score / len(votes) if votes else 0.0
    if coin and coin["side"]:
        verdict = coin["side"].upper()
        detail = f"Called this week at {coin['leverage']}x."
    elif coin and coin["skipped"]:
        verdict, detail = "STAY OUT", f"Signals lean {coin['signal']}, but that side has not held up here."
    elif coin:
        verdict, detail = "STAY OUT", "Mood and the last four weeks disagree."
    else:
        verdict = "NO CALL"
        detail = "No rule tested has an edge on this market. Shown for reference only."
    return {"symbol": symbol, "lean": lean, "verdict": verdict, "detail": detail,
            "strength": abs(score), "of": len(votes),
            "votes": [{"name": n, "direction": "up" if v > 0 else "down"} for n, v in votes]}


def _note(row: dict, rec: dict) -> str:
    if row.get("error"):
        return row["error"]
    trend = f"Trend {row['trend']} {row['trend_days']:.0f}d."
    return f"{trend} {rec['detail']}"


def snapshot(stake: float, now: datetime | None = None) -> dict:
    now = _utc(now)
    week = wc.this_week()
    live = md.catalogue(max_age=4)
    coins = list(wc.calls_config())
    share = stake / len(coins)
    monday = datetime.fromisoformat(week["week_of"]).date()
    positions, too_small = [], []
    for c in week["coins"]:
        if not c["side"]:
            continue
        if share * c["leverage"] < wc.MIN_ORDER_USD:
            # the exchange would refuse an order this small, so there is no position to follow
            too_small.append({"coin": c["coin"], "name": c["name"], "side": c["side"],
                              "leverage": c["leverage"], "position_usd": share * c["leverage"],
                              "min_weekly_amount": c.get("min_weekly_amount")
                              or wc.MIN_ORDER_USD / c["leverage"] * len(coins)})
            continue
        price = (live.get(c["coin"]) or {}).get("price") or c["price"]
        daily = md.chart_candles(c["coin"], "1d", 12)
        days = [{"high": d["h"], "low": d["l"]} for d in daily
                if datetime.fromtimestamp(d["t"] / 1000, timezone.utc).date() >= monday]
        positions.append(position_state(c, days, price, share))

    # Trend states and sparklines move slowly; refresh them once a minute, prices every few seconds.
    rows = md.cached("terminal:rows", 60, lambda: desk.watchlist_signals(list(DASHBOARD)))
    markets = []
    for row in rows:
        symbol = row["symbol"]
        fresh = live.get(symbol) or {}
        rec = recommendation(symbol, row, week)
        try:
            closes = md.candles(symbol)["close"][-42:].tolist()       # the last week of 4-hour closes
        except md.DataError:
            closes = []
        markets.append({
            "symbol": symbol, "label": row.get("label", md.label(symbol)),
            "class": row.get("class", md.asset_class(symbol)),
            "price": fresh.get("price", row.get("price")), "day_change_pct": fresh.get("day_change_pct"),
            "spark": closes, "note": _note(row, rec), "recommendation": rec,
        })
    total = sum(p["pnl_usd"] for p in positions)
    return {
        "now": now.isoformat(), "week_of": week["week_of"], "next_call": week["next_call"],
        "mood": {"week": week["mood"], "today": (week.get("preview") or {}).get("mood")},
        "stake": stake, "positions": positions, "week_pnl_usd": total,
        "advice": advice(positions, week, now, too_small), "markets": markets, "too_small": too_small,
        "stay_out": [{"coin": c["coin"], "name": c["name"], "signal": c["signal"], "skipped": c["skipped"]}
                     for c in week["coins"] if not c["side"]],
    }


def chart(symbol: str, interval: str) -> dict:
    """Candles with the two averages the trend rule uses, and this week's levels for the market."""
    per_day = BARS_PER_DAY[interval]
    need = CHART_BARS[interval] + EMA_DAYS[1] * per_day * 3          # history for the slow average to settle
    candles = md.chart_candles(symbol, interval, min(need, 4900))
    if not candles:
        raise md.DataError(f"No chart data for {md.label(symbol)}.")
    closes = [c["c"] for c in candles]
    emas = {}
    for days in EMA_DAYS:
        span = days * per_day
        if len(closes) >= span:
            k, value, series = 2.0 / (span + 1), closes[0], []
            for price in closes:
                value = price * k + value * (1 - k)
                series.append(value)
            emas[f"{days}d"] = series[-CHART_BARS[interval]:]
    shown = candles[-CHART_BARS[interval]:]
    for name in emas:                                   # pad when history is shorter than the window
        emas[name] = [None] * (len(shown) - len(emas[name])) + emas[name][-len(shown):]
    return {"symbol": symbol, "label": md.label(symbol), "interval": interval, "candles": shown, "emas": emas}
