"""Crypto mood for the week: the Fear & Greed index and what it has meant.

research/scripts/crypto_weekly_search.py tested the index as a weekly signal
on ten coins over six years. Going WITH the mood (long when the index is 50
or more, short when it is below) was right more often than not and beat
simply holding in both halves of the history. Going against it ("buy fear")
was the worst rule tested. This module reports this week's reading, what the
tested rule says, and that record.

The rule uses the reading on the week's last day (Sunday, UTC) for the week
that follows. The index is market-wide, so it gives one direction for all
coins, and it is partly built from recent price moves, so it overlaps with a
trend signal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

import market_data as md

FNG_URL = "https://api.alternative.me/fng/?limit=21&format=json"
EVIDENCE = (Path(__file__).resolve().parents[2] / "research" / "exports" / "tables"
            / "crypto_weekly_search.json")
WITH_MOOD = "Long when greedy, short when fearful"
EXTREMES = "Sell extreme fear, buy extreme greed"
HOLDING = "Always long"


def _readings() -> list[dict]:
    """Recent daily readings, newest first."""
    def load():
        try:
            resp = requests.get(FNG_URL, timeout=20)
            resp.raise_for_status()
            data = resp.json()["data"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise md.DataError(f"The mood index is unavailable right now ({type(exc).__name__}).") from exc
        return [{"date": datetime.fromtimestamp(int(d["timestamp"]), timezone.utc).date(),
                 "value": int(d["value"]), "label": d.get("value_classification", "")} for d in data]
    return md.cached("fear_greed", 3600, load)


def rule_direction(value: int) -> dict:
    """What the two tested rules say for a reading."""
    return {
        "with_mood": "long" if value >= 50 else "short",
        "extreme": "long" if value > 75 else ("short" if value < 25 else None),
    }


def _evidence() -> dict | None:
    try:
        rows = {r["rule"]: r for r in json.loads(EVIDENCE.read_text())["rows"]}
    except (OSError, ValueError, KeyError):
        return None

    def pick(name):
        r = rows.get(name)
        if not r:
            return None
        return {half: {"accuracy": r["no_costs"][half]["accuracy"],
                       "avg_pct": r["no_costs"][half]["avg_pct"],
                       "coin_weeks": r["no_costs"][half]["coin_weeks"]}
                for half in ("choose", "check")}
    data = json.loads(EVIDENCE.read_text())
    return {"with_mood": pick(WITH_MOOD), "extreme": pick(EXTREMES), "holding": pick(HOLDING),
            "from": data.get("from"), "to": data.get("to"), "split": data.get("split"),
            "coins": len(data.get("coins", []))}


def this_week() -> dict:
    readings = _readings()
    today = readings[0]
    # A week runs Monday to Monday and is governed by the Sunday reading before
    # it. On a Sunday the current week is therefore still last Sunday's.
    sunday = next((r for r in readings if r["date"].weekday() == 6 and r["date"] < today["date"]),
                  today)
    week_ago = next((r for r in readings if (sunday["date"] - r["date"]).days == 7), None)
    return {
        "today": {"value": today["value"], "label": today["label"], "date": today["date"].isoformat()},
        "week_reading": {"value": sunday["value"], "label": sunday["label"],
                         "date": sunday["date"].isoformat()},
        "change_from_last_week": sunday["value"] - week_ago["value"] if week_ago else None,
        "rules": rule_direction(sunday["value"]),
        "evidence": _evidence(),
    }
