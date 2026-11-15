"""This week, for a beginner: hold coins or hold cash. No shorting, no leverage.

The rule (research/scripts/crypto_in_or_out.py): for the coming week, hold a
coin only if BOTH are true at the start of the week

  1. the market's mood is greedy: the Fear & Greed index is 50 or more, and
  2. the coin rose over the last four weeks.

Otherwise hold cash. Over Aug 2020 to now this grew money faster than holding
all the time and fell far less along the way, in each half of the history.
It is decided once a week, from the last completed week (Monday 00:00 UTC).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import crypto_mood
import market_data as md

COINS = ("BTC", "ETH")
EVIDENCE = (Path(__file__).resolve().parents[2] / "research" / "exports" / "tables"
            / "crypto_in_or_out.json")
RULE_NAME = "In when mood and 4 weeks agree"
HOLD_NAME = "Always hold"
BASKET = "BTC and ETH"
DAY_MS = 24 * 3600 * 1000


def sunday_closes(coin: str) -> list[dict]:
    """Closing price of each completed week (the Sunday daily candle), newest first."""
    def load():
        now = int(time.time() * 1000)
        rows = md._post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1d", "startTime": now - 70 * DAY_MS, "endTime": now}})
        out = []
        for r in rows:
            opened = datetime.fromtimestamp(int(r["t"]) / 1000, timezone.utc)
            if int(r["t"]) + DAY_MS <= now and opened.weekday() == 6:
                out.append({"date": opened.date(), "close": float(r["c"])})
        return sorted(out, key=lambda x: x["date"], reverse=True)
    return md.cached(f"sunday_closes:{coin}", 3600, load)


def decide(mood_value: int, closes: list[dict]) -> dict:
    """The rule for one coin. `closes` is newest first, one per completed week."""
    if len(closes) < 5:
        raise md.DataError("Not enough weekly prices to decide.")
    now, before = closes[0]["close"], closes[4]["close"]
    change = (now / before - 1) * 100
    mood_ok, rose = mood_value >= 50, change > 0
    return {"hold": bool(mood_ok and rose), "mood_ok": bool(mood_ok), "rose": bool(rose),
            "four_week_change_pct": change, "week_close": now, "as_of": closes[0]["date"].isoformat()}


def record() -> dict | None:
    try:
        data = json.loads(EVIDENCE.read_text())
        rows = {r["rule"]: r for r in data["baskets"][BASKET]}
        return {"rule": rows[RULE_NAME], "hold": rows[HOLD_NAME], "from": data["from"],
                "to": data["to"], "split": data["split"]}
    except (OSError, ValueError, KeyError):
        return None


def this_week() -> dict:
    mood = crypto_mood.this_week()
    value = mood["week_reading"]["value"]
    coins = []
    for coin in COINS:
        closes = sunday_closes(coin)
        if closes and closes[0]["date"].isoformat() != mood["week_reading"]["date"]:
            # Mood and price must describe the same week's end.
            raise md.DataError("This week's mood and prices are out of step; try again shortly.")
        coins.append({"coin": coin, **decide(value, closes)})
    holding = [c["coin"] for c in coins if c["hold"]]
    return {
        "answer": "hold" if holding else "cash",
        "hold": holding,
        "mood": mood["week_reading"],
        "coins": coins,
        "record": record(),
    }


# ---- week by week ----------------------------------------------------------

LEVERAGES = (1, 3, 5, 10, 20, 30)
FEE_PER_SIDE = 0.00045          # exchange fee, share of the position, each way
FUNDING_PER_DAY = 0.0003        # typical cost of holding a leveraged long, per day
LEVERAGE_EVIDENCE = EVIDENCE.with_name("leverage_on_rule.json")


def daily_candles(coin: str) -> dict:
    """Completed daily candles by date."""
    def load():
        now = int(time.time() * 1000)
        rows = md._post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1d", "startTime": now - 220 * DAY_MS, "endTime": now}})
        return {datetime.fromtimestamp(int(r["t"]) / 1000, timezone.utc).date():
                {"open": float(r["o"]), "low": float(r["l"]), "close": float(r["c"])}
                for r in rows if int(r["t"]) + DAY_MS <= now}
    return md.cached(f"daily_candles:{coin}", 3600, load)


def week_result(candles: dict, monday, mood_value: int, share: float, leverage: float,
                max_leverage: int) -> dict:
    """One coin, one finished week: was it held, and what did its share of the stake make?"""
    from datetime import timedelta
    sunday_before = monday - timedelta(days=1)
    days = [candles.get(monday + timedelta(days=i)) for i in range(7)]
    now, before = candles.get(sunday_before), candles.get(sunday_before - timedelta(days=28))
    if not now or not before or any(d is None for d in days):
        return {"known": False}
    hold = mood_value >= 50 and now["close"] > before["close"]
    if not hold:
        return {"known": True, "hold": False, "result": 0.0, "wiped": False}
    lev = float(min(leverage, max_leverage))
    entry = days[0]["open"]
    low_move = min(d["low"] for d in days) / entry - 1
    close_move = days[-1]["close"] / entry - 1
    # The exchange closes the position before the whole stake is gone.
    wipe = -(1.0 / lev - 1.0 / (2 * max_leverage)) if lev > 1 else -1.0
    costs = lev * (2 * FEE_PER_SIDE + (7 * FUNDING_PER_DAY if lev > 1 else 0.0))
    result = share * (lev * close_move - costs)
    wiped = low_move <= wipe
    if wiped:
        result = -share
    return {"known": True, "hold": True, "result": max(result, -share), "wiped": bool(wiped),
            "leverage_used": lev, "week_move_pct": close_move * 100}


def scoreboard(stake: float, leverage: float, weeks: int = 12) -> dict:
    """The last finished weeks: what the rule said and what a fresh stake made each week."""
    from datetime import timedelta
    moods = crypto_mood.history()
    live = md.catalogue()
    candles = {coin: daily_candles(coin) for coin in COINS}
    last_day = min(max(c) for c in candles.values())
    # the last finished week ends on the latest completed Sunday
    last_sunday = last_day - timedelta(days=(last_day.weekday() + 1) % 7)
    rows = []
    for i in range(weeks):
        monday = last_sunday - timedelta(days=6 + 7 * i)
        mood_value = moods.get(monday - timedelta(days=1))
        if mood_value is None:
            continue
        parts = {coin: week_result(candles[coin], monday, mood_value, stake / len(COINS), leverage,
                                   (live.get(coin) or {}).get("max_leverage", 20))
                 for coin in COINS}
        if not all(p["known"] for p in parts.values()):
            continue
        rows.append({
            "week_of": monday.isoformat(), "mood": mood_value,
            "held": [c for c, p in parts.items() if p["hold"]],
            "wiped": [c for c, p in parts.items() if p["wiped"]],
            "result": sum(p["result"] for p in parts.values()),
        })
    total = 0.0
    for row in reversed(rows):                      # oldest first for the running total
        total += row["result"]
        row["running_total"] = total
    expect = None
    try:
        by = json.loads(LEVERAGE_EVIDENCE.read_text())["by_leverage"].get(f"{leverage:g}x")
        if by:
            expect = {**by["picture"], "lost_whole_stake": by["lost_whole_stake"]}
    except (OSError, ValueError, KeyError):
        pass
    return {
        "stake": stake, "leverage": leverage, "rows": rows, "total": total,
        "ahead": sum(1 for r in rows if r["result"] > 0),
        "behind": sum(1 for r in rows if r["result"] < 0),
        "no_trade": sum(1 for r in rows if not r["held"]),
        "expect": expect,
        "capped": {coin: live[coin]["max_leverage"] for coin in COINS
                   if coin in live and live[coin]["max_leverage"] < leverage},
    }


def one_line() -> str:
    """The week's answer as one sentence, for the daily email."""
    week = this_week()
    mood = week["mood"]
    if week["answer"] == "hold":
        return (f"This week: hold {' and '.join(week['hold'])} (no leverage). "
                f"Mood is {mood['label'].lower()} ({mood['value']}).")
    return (f"This week: stay in cash, hold no coins. "
            f"Mood is {mood['label'].lower()} ({mood['value']}).")
