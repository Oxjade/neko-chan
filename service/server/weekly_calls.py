"""This week's calls on perps: long, short or stay out, with a suggested leverage.

For someone who does not trade. Once a week, per coin (Bitcoin, Ether):

  long   when the market's mood is greedy (Fear & Greed 50 or more) and the
         coin rose over the last four weeks
  short  when the mood is fearful (below 50) and the coin fell over the last
         four weeks
  out    otherwise

A call is only made where research/scripts/crypto_long_short.py found that
side right more often than not AND profitable in both halves of the history
for that coin. Where it did not (shorting Bitcoin), the platform says stay
out instead of guessing.

The suggested leverage is the highest tested level at which the weekly stake
was liquidated in at most one week in fifty. Positions are opened on Monday
(00:00 UTC) and closed the following Monday.

Stop loss and take profit (research/scripts/crypto_exits.py): tight stops and
take-profit targets made the calls worse, and limit entries at a better price
missed too many winning weeks. A stop one "normal week" from entry (the
coin's typical weekly move over the last half year) is suggested as
protection: it caps a bad week for a small cost in average gain. No take
profit is suggested; a far one, three normal weeks away, is offered as
optional because it did not hurt.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import crypto_mood
import market_data as md

NAMES = {"BTC": "Bitcoin", "ETH": "Ether", "SOL": "Solana", "SUI": "Sui"}
TABLES = Path(__file__).resolve().parents[2] / "research" / "exports" / "tables"
CALLS_EVIDENCE = TABLES / "crypto_long_short.json"
SPOT_EVIDENCE = TABLES / "crypto_in_or_out.json"
EXITS_EVIDENCE = TABLES / "crypto_exits.json"
TAKE_PROFIT_EVIDENCE = TABLES / "crypto_take_profit.json"
MIN_ORDER_USD = 10.0                        # the exchange refuses to open anything smaller
VOL_WEEKS = 26                              # the "normal week" looks back half a year
SUGGESTED = 0                               # leverage value meaning "use the suggested level"
LEVERAGES = (SUGGESTED, 1, 2, 3, 5, 10, 20, 30)
FEE_PER_SIDE = 0.00045                      # exchange fee, share of the position, each way
FUNDING_PER_DAY = 0.0003                    # typical cost of holding a leveraged long, per day
DAY_MS = 24 * 3600 * 1000


def calls_config() -> dict:
    """Per coin and side: whether the call is made, the suggested leverage, and its record."""
    try:
        return json.loads(CALLS_EVIDENCE.read_text())["calls"]
    except (OSError, ValueError, KeyError) as exc:
        raise md.DataError("The tested record for the weekly calls is missing.") from exc


def take_profit_rule() -> dict:
    """Per side, the take profit the test allows, in normal weeks (None: hold to Monday).

    Shorts get one because falls tend to bounce; longs do not, because every
    nearer target made longs worse (research/scripts/crypto_take_profit.py)."""
    try:
        sides = json.loads(TAKE_PROFIT_EVIDENCE.read_text())["by_side"]
        return {side: {"target": s["target"], "with": s["with"]["all"], "stop_only": s["stop_only"]["all"],
                       "at_one_normal_week": s.get("at_one_normal_week"),
                       "reached": s["with"]["all"].get("final_target") if s["target"] else None,
                       "plans_as_good": s["plans_as_good"], "plans_tested": s["plans_tested"]}
                for side, s in sides.items()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def exit_rule() -> dict:
    """Stop and take profit, in normal weeks, with the tested effect."""
    try:
        rec = json.loads(EXITS_EVIDENCE.read_text())["recommendation"]
        take = take_profit_rule()
        return {"stop": rec["stop"],
                "targets": {side: (take.get(side) or {}).get("target") for side in ("long", "short")},
                "take_profit": take,
                "with": rec["with"]["all"], "holding": rec["holding"]["all"], "by_call": rec["by_call"]}
    except (OSError, ValueError, KeyError):
        return {"stop": None, "targets": {"long": None, "short": None}, "take_profit": {},
                "with": None, "holding": None, "by_call": {}}


def normal_week(candles: dict, sunday) -> float | None:
    """A coin's typical weekly move (as a fraction) over the half year ending that Sunday."""
    closes = [candles.get(sunday - timedelta(days=7 * k)) for k in range(VOL_WEEKS + 1)]
    if any(c is None for c in closes):
        return None
    prices = [c["close"] for c in reversed(closes)]
    returns = [b / a - 1 for a, b in zip(prices, prices[1:])]
    mean = sum(returns) / len(returns)
    return (sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) ** 0.5


def signal(mood_value: int, close_now: float, close_four_weeks_ago: float) -> str | None:
    """What the rule says before asking whether that side has earned a call."""
    if mood_value >= 50 and close_now > close_four_weeks_ago:
        return "long"
    if mood_value < 50 and close_now < close_four_weeks_ago:
        return "short"
    return None


def liquidation_distance(leverage: float, max_leverage: int) -> float:
    """Adverse move, as a fraction of the entry price, at which the exchange closes the position."""
    if leverage <= 1:
        return 1.0
    return 1.0 / leverage - 1.0 / (2 * max_leverage)


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


def week_open(coin: str, monday) -> float | None:
    """The price the week opened at: the open of Monday's daily candle.

    That candle exists from 00:00 UTC while its day is still forming, so the
    entry, stop and take profit are fixed from the first minute of the week
    instead of following the live price until Monday has closed."""
    def load():
        now = int(time.time() * 1000)
        rows = md._post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1d", "startTime": now - 9 * DAY_MS, "endTime": now}})
        return {datetime.fromtimestamp(int(r["t"]) / 1000, timezone.utc).date(): float(r["o"]) for r in rows}
    try:
        return md.cached(f"week_open:{coin}", 300, load).get(monday)
    except md.DataError:
        return None                           # the live price stands in until the candle can be read


def spot_record() -> dict | None:
    """The long side without leverage, against simply holding."""
    try:
        data = json.loads(SPOT_EVIDENCE.read_text())
        rows = {r["rule"]: r for r in data["baskets"]["BTC and ETH"]}
        return {"rule": rows["In when mood and 4 weeks agree"], "hold": rows["Always hold"],
                "from": data["from"], "to": data["to"], "split": data["split"]}
    except (OSError, ValueError, KeyError):
        return None


def _record(side_config: dict, leverage: int | None) -> dict:
    at = (side_config.get("by_leverage") or {}).get(f"{leverage}x") if leverage else None
    return {
        "first_half": {k: side_config["first_half"][k] for k in ("right", "avg_pct", "coin_weeks")},
        "second_half": {k: side_config["second_half"][k] for k in ("right", "avg_pct", "coin_weeks")},
        "at_leverage": ({k: at[k] for k in ("ahead", "liquidated", "avg_per_week", "weeks")} if at else None),
    }


def this_week() -> dict:
    config = calls_config()
    exits = exit_rule()
    mood = crypto_mood.this_week()
    value = mood["week_reading"]["value"]
    live = md.catalogue()
    coins = []
    for coin in config:
        closes = sunday_closes(coin)
        if len(closes) < 5:
            raise md.DataError("Not enough weekly prices to decide.")
        if closes[0]["date"].isoformat() != mood["week_reading"]["date"]:
            # Mood and price must describe the same week's end.
            raise md.DataError("This week's mood and prices are out of step; try again shortly.")
        now, before = closes[0]["close"], closes[4]["close"]
        sig = signal(value, now, before)
        side_config = config[coin][sig] if sig else None
        called = bool(sig and side_config["allowed"])
        leverage = side_config["suggested_leverage"] if called else None
        market = live.get(coin) or {}
        price, max_lev = market.get("price"), market.get("max_leverage", 20)
        row = {
            "coin": coin, "name": NAMES.get(coin, coin), "mood_greedy": value >= 50,
            "four_week_change_pct": (now / before - 1) * 100,
            "signal": sig, "side": sig if called else None,
            "skipped": bool(sig and not called), "leverage": leverage, "price": price,
            "record": _record(side_config, side_config["suggested_leverage"]) if sig else None,
        }
        if called:
            # the weekly amount at which this call's share becomes an order the exchange accepts
            row["min_weekly_amount"] = MIN_ORDER_USD / leverage * len(config)
        if called and price:
            sign = 1 if sig == "long" else -1
            candles = daily_candles(coin)
            sunday = closes[0]["date"]
            # The trade opens at Monday's open. Before Monday starts there is no such candle yet,
            # so the live price stands in.
            monday = sunday + timedelta(days=1)
            entry = (candles.get(monday) or {}).get("open") or week_open(coin, monday) or price
            distance = liquidation_distance(leverage, max_lev)
            row["entry"] = entry
            row["liquidation_move_pct"] = distance * 100
            row["liquidation_price"] = entry * (1 - sign * distance)
            normal = normal_week(candles, sunday)
            if normal and exits["stop"]:
                stop_move = min(exits["stop"] * normal, distance * 0.95)
                row["normal_week_pct"] = normal * 100
                row["stop_pct"] = stop_move * 100
                row["stop_price"] = entry * (1 - sign * stop_move)
                row["stake_lost_at_stop"] = min(1.0, leverage * (stop_move + 2 * FEE_PER_SIDE))
                target_rule = exits["targets"].get(sig)
                if target_rule:
                    target_move = target_rule * normal
                    row["target_pct"] = target_move * 100
                    row["target_price"] = entry * (1 + sign * target_move)
                    row["stake_made_at_target"] = leverage * (target_move - 2 * FEE_PER_SIDE)
        coins.append(row)
    week_start = datetime.fromisoformat(mood["week_reading"]["date"]).date() + timedelta(days=1)
    return {
        "calls": [{"coin": c["coin"], "side": c["side"], "leverage": c["leverage"]}
                  for c in coins if c["side"]],
        "mood": mood["week_reading"], "coins": coins, "spot_record": spot_record(),
        "week_of": week_start.isoformat(), "next_call": (week_start + timedelta(days=7)).isoformat(),
        "preview": preview(config, mood, live),
        "exits": {k: exits[k] for k in ("stop", "targets", "take_profit", "with", "holding")},
        "min_order_usd": MIN_ORDER_USD,
    }


def preview(config: dict, mood: dict, live: dict) -> dict | None:
    """What next Monday's calls would be if the week ended now. Final only on Monday."""
    today = mood.get("today")
    if not today:
        return None
    rows = []
    for coin in config:
        closes = sunday_closes(coin)
        price = (live.get(coin) or {}).get("price")
        if len(closes) < 4 or not price:
            continue
        before = closes[3]["close"]              # four weeks before the coming week's end
        sig = signal(today["value"], price, before)
        called = bool(sig and config[coin][sig]["allowed"])
        rows.append({"coin": coin, "name": NAMES.get(coin, coin), "signal": sig,
                     "side": sig if called else None, "skipped": bool(sig and not called),
                     "leverage": config[coin][sig]["suggested_leverage"] if called else None,
                     "four_week_change_pct": (price / before - 1) * 100})
    return {"mood": today, "coins": rows}


# ---- week by week ----------------------------------------------------------

def daily_candles(coin: str) -> dict:
    """Completed daily candles by date."""
    def load():
        now = int(time.time() * 1000)
        rows = md._post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1d", "startTime": now - 330 * DAY_MS, "endTime": now}})
        return {datetime.fromtimestamp(int(r["t"]) / 1000, timezone.utc).date():
                {"open": float(r["o"]), "high": float(r["h"]), "low": float(r["l"]),
                 "close": float(r["c"])}
                for r in rows if int(r["t"]) + DAY_MS <= now}
    return md.cached(f"daily_candles:{coin}", 3600, load)


def week_result(candles: dict, monday, mood_value: int, share: float, leverage: float,
                max_leverage: int, coin_config: dict) -> dict:
    """One coin, one finished week: the call, and what its share of the stake made."""
    sunday_before = monday - timedelta(days=1)
    days = [candles.get(monday + timedelta(days=i)) for i in range(7)]
    now, before = candles.get(sunday_before), candles.get(sunday_before - timedelta(days=28))
    if not now or not before or any(d is None for d in days):
        return {"known": False}
    sig = signal(mood_value, now["close"], before["close"])
    if not sig or not coin_config[sig]["allowed"]:
        return {"known": True, "side": None, "skipped": bool(sig), "result": 0.0, "wiped": False,
                "stopped": False}
    lev = float(coin_config[sig]["suggested_leverage"] if leverage == SUGGESTED
                else min(leverage, max_leverage))
    if share * lev < MIN_ORDER_USD:
        # the exchange would not have opened a position this small
        return {"known": True, "side": None, "skipped": False, "too_small": True, "result": 0.0,
                "wiped": False, "stopped": False}
    entry = days[0]["open"]
    if sig == "long":
        against = 1 - min(d["low"] for d in days) / entry
        move = days[-1]["close"] / entry - 1
    else:
        against = max(d["high"] for d in days) / entry - 1
        move = 1 - days[-1]["close"] / entry
    liquidation = liquidation_distance(lev, max_leverage)
    rule = exit_rule()
    stop_rule = rule["stop"]
    normal = normal_week(candles, sunday_before) if stop_rule else None
    stop_move = stop_rule * normal if (stop_rule and normal) else None
    target_rule = rule["targets"].get(sig) if stop_move is not None else None
    target_move = target_rule * normal if target_rule else None
    # Walk the week a day at a time: liquidation, the stop or the take profit ends it early.
    # Inside one day the order of events is unknown, so the stop is counted before the target.
    held, wiped, stopped, took = 0, False, False, False
    for day in days:
        held += 1
        worst = (1 - day["low"] / entry) if sig == "long" else (day["high"] / entry - 1)
        if stop_move is not None and stop_move < liquidation and worst >= stop_move:
            move, stopped = -stop_move, True
            break
        if worst >= liquidation:
            wiped = True
            break
        best = (day["high"] / entry - 1) if sig == "long" else (1 - day["low"] / entry)
        if target_move is not None and best >= target_move:
            move, took = target_move, True
            break
    funding = held * FUNDING_PER_DAY if (sig == "long" and lev > 1) else 0.0
    result = -share if wiped else share * lev * (move - 2 * FEE_PER_SIDE - funding)
    return {"known": True, "side": sig, "skipped": False, "result": max(result, -share),
            "wiped": bool(wiped), "stopped": bool(stopped), "took_profit": bool(took),
            "leverage_used": lev}


def scoreboard(stake: float, leverage: float, weeks: int = 12) -> dict:
    """The last finished weeks: the calls made and what a fresh stake made each week."""
    config = calls_config()
    moods = crypto_mood.history()
    live = md.catalogue()
    coins = list(config)
    candles = {coin: daily_candles(coin) for coin in coins}
    max_lev = {coin: (live.get(coin) or {}).get("max_leverage", 20) for coin in coins}
    last_day = min(max(c) for c in candles.values())
    # the last finished week ends on the latest completed Sunday
    last_sunday = last_day - timedelta(days=(last_day.weekday() + 1) % 7)
    rows = []
    for i in range(weeks):
        monday = last_sunday - timedelta(days=6 + 7 * i)
        mood_value = moods.get(monday - timedelta(days=1))
        if mood_value is None:
            continue
        parts = {coin: week_result(candles[coin], monday, mood_value, stake / len(coins), leverage,
                                   max_lev[coin], config[coin]) for coin in coins}
        if not all(p["known"] for p in parts.values()):
            continue
        rows.append({
            "week_of": monday.isoformat(), "mood": mood_value,
            "calls": [{"coin": c, "side": p["side"], "leverage": p["leverage_used"]}
                      for c, p in parts.items() if p["side"]],
            "skipped": [c for c, p in parts.items() if p["skipped"]],
            "wiped": [c for c, p in parts.items() if p["wiped"]],
            "stopped": [c for c, p in parts.items() if p.get("stopped")],
            "took_profit": [c for c, p in parts.items() if p.get("took_profit")],
            "too_small": [c for c, p in parts.items() if p.get("too_small")],
            "result": sum(p["result"] for p in parts.values()),
        })
    total = 0.0
    for row in reversed(rows):                      # oldest first for the running total
        total += row["result"]
        row["running_total"] = total
    expect = []
    for coin in coins:
        for side in ("long", "short"):
            c = config[coin][side]
            if not c["allowed"]:
                continue
            lev = c["suggested_leverage"] if leverage == SUGGESTED else min(leverage, max_lev[coin])
            # use the nearest tested level at or below the leverage actually used
            tested = sorted(int(k[:-1]) for k in c["by_leverage"])
            level = max((t for t in tested if t <= lev), default=tested[0])
            at = c["by_leverage"].get(f"{level}x")
            if at:
                expect.append({"coin": coin, "side": side, "leverage": lev, "weeks": at["weeks"],
                               "ahead": at["ahead"], "liquidated": at["liquidated"],
                               "avg_per_week": at["avg_per_week"]})
    return {
        "stake": stake, "leverage": leverage, "rows": rows, "total": total,
        "ahead": sum(1 for r in rows if r["result"] > 0),
        "behind": sum(1 for r in rows if r["result"] < 0),
        "no_trade": sum(1 for r in rows if not r["calls"]),
        "expect": expect,
        "capped": {coin: max_lev[coin] for coin in coins if max_lev[coin] < leverage},
        "min_order_usd": MIN_ORDER_USD,
    }


def one_line() -> str:
    """The week's calls as one sentence, for the daily email."""
    week = this_week()
    mood = week["mood"]
    tail = f"Mood is {mood['label'].lower()} ({mood['value']})."
    if not week["calls"]:
        return f"This week: stay out, no trade. {tail}"
    parts = [f"{c['side']} {c['coin']} at {c['leverage']}x" for c in week["calls"]]
    return f"This week: {' and '.join(parts)}. {tail}"
