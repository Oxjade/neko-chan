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


def one_line() -> str:
    """The week's answer as one sentence, for the daily email."""
    week = this_week()
    mood = week["mood"]
    if week["answer"] == "hold":
        return (f"This week: hold {' and '.join(week['hold'])} (no leverage). "
                f"Mood is {mood['label'].lower()} ({mood['value']}).")
    return (f"This week: stay in cash, hold no coins. "
            f"Mood is {mood['label'].lower()} ({mood['value']}).")
