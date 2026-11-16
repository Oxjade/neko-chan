"""Taking profit on the weekly crypto calls: can a nearer target be made to work?

The weekly calls open on Monday and close the next Monday, with a stop one
"normal week" away (research/scripts/crypto_exits.py). The only target that
test allowed was three normal weeks away, which is rarely reached. This script
asks what a nearer, reachable take profit costs or gains, for every side the
platform calls, at the leverage it suggests.

Plans tested, all with the stop one normal week away:

  full exit      close everything at a target 0.5 to 3 normal weeks away
  part exit      close a third, half or two thirds at a first target and hold
                 the rest to Monday, with the rest's stop left where it was or
                 moved to the entry price, and with or without a far target
  lock in        no target, but once the trade is one normal week ahead the
                 stop moves to the entry price or to half a normal week of
                 profit, and steps up again at two normal weeks

Daily candles are used, so the order of events inside a day is unknown. The
worst case is assumed throughout: if a day touches the stop and a target, the
stop counts first; a stop that was moved up only protects from the NEXT day;
and if a day touches a moved stop and a later target, the stop counts first.
Fees are charged each way on every part; longs pay funding for the days held.

How a plan is chosen for all sides together (written before looking at the
results): it must make money in both halves of the history and keep at least
80% of the stop-only average week, over the whole history. Among those, the
plan with the most weeks ending ahead wins; a tie goes to the higher average.

That first pass showed longs and shorts behaving differently, so a second rule
was added afterwards and is the one the platform uses. For each side on its
own, only plans that can be placed as resting orders on Monday are considered
(the stop alone, or the stop with one take profit for the whole position):
the exchange's smallest order is 10 USD, which rules out part exits on small
positions, and a beginner should not have to watch the screen to move a stop.
A take profit is adopted for a side only if its average week is at least as
good as the stop-only rule's in BOTH halves of the history; of those that
qualify, the nearest one is used, because a target has to be reachable to be
of any use. If none qualifies, that side keeps the stop only.

    python research/scripts/crypto_take_profit.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crypto_exits as ce  # noqa: E402
import crypto_long_short as cls  # noqa: E402
import crypto_weekly_search as cws  # noqa: E402

TABLES = cws.fws.REPO / "research" / "exports" / "tables"
OUT = TABLES / "crypto_take_profit.json"
STOP = 1.0
KEEP = 0.8                       # share of the stop-only average a plan must keep
FULL_TARGETS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)
FIRST_TARGETS = (0.5, 0.75, 1.0, 1.5)
PARTS = (1 / 3, 1 / 2, 2 / 3)
FAR_TARGETS = (None, 2.0, 3.0)


def plan(name: str, take=(), locks=(), final=None) -> dict:
    """take: (target, share of the position) pairs, nearest first.
    locks: (favourable move that triggers it, where the stop moves to), in normal weeks; 0 is the entry.
    final: a target that closes whatever is left."""
    return {"name": name, "take": [list(t) for t in take], "locks": [list(x) for x in locks], "final": final}


def play(week: dict, side: str, coin: str, leverage: float, p: dict) -> tuple[float, dict]:
    """One week under a plan: result per 1 USD of stake, and what was reached."""
    sign = 1.0 if side == "long" else -1.0
    days, normal = week["days"], week["normal"]
    entry = days[0][0]
    liquidation = 1.0 / leverage - 1.0 / (2 * cls.MAX_LEVERAGE[coin])
    stop_move = min(STOP * normal, liquidation)
    wiped = STOP * normal >= liquidation
    left, banked, held = 1.0, 0.0, 0.0           # share still open, move banked, share-days held
    takes = [(t * normal, share) for t, share in p["take"]]
    locks = sorted((trigger * normal, level * normal) for trigger, level in p["locks"])
    final = p["final"] * normal if p["final"] else None
    lock = None                                  # where the moved stop sits, as a move in our favour
    reached = {"first_target": False, "final_target": False, "stopped": False, "locked_out": False}
    best = 0.0
    for i in range(7):
        _, high, low, close = days[i]
        worst = -((1 - low / entry) if side == "long" else (high / entry - 1))
        top = (high / entry - 1) if side == "long" else (1 - low / entry)
        held += left
        if lock is None and -worst >= stop_move:              # the stop counts before anything else
            banked += left * (-1.0 / leverage if wiped else -stop_move)
            left, reached["stopped"] = 0.0, True
            break
        if lock is not None and worst <= lock:                # a moved stop counts before a target
            banked += left * lock
            left, reached["locked_out"] = 0.0, True
            break
        while takes and top >= takes[0][0]:
            move, share = takes.pop(0)
            share = min(share, left)
            banked += share * move
            left -= share
            reached["first_target"] = True
        if final is not None and top >= final and left > 0:
            banked += left * final
            left, reached["final_target"] = 0.0, True
            break
        best = max(best, top)
        for trigger, level in locks:                          # protects from the next day on
            if best >= trigger:
                lock = level if lock is None else max(lock, level)
    if left > 0:
        banked += left * (days[6][3] / entry - 1) * sign
    funding = held * cls.FUNDING_PER_DAY if side == "long" else 0.0
    return max(leverage * (banked - 2 * cls.FEE_PER_SIDE - funding), -1.0), reached


def summarise(rows: list[tuple], split) -> dict:
    def part(sub):
        r = np.array([v for _, v, _ in sub])
        if not len(r):
            return {"weeks": 0, "ahead": None, "avg": None, "worst": None, "total": 0.0}
        return {"weeks": int(len(r)), "ahead": float((r > 0).mean()), "avg": float(r.mean()),
                "worst": float(r.min()), "total": float(r.sum())}
    out = {"all": part(rows), "first_half": part([x for x in rows if x[0] < split]),
           "second_half": part([x for x in rows if x[0] >= split])}
    for key in ("first_target", "final_target", "stopped", "locked_out"):
        out["all"][key] = float(np.mean([flags[key] for _, _, flags in rows])) if rows else None
    return out


def rounded(value, digits: int) -> str:
    """JSON with floats cut to a few decimals, one line per plan."""
    def cut(v):
        if isinstance(v, float):
            return round(v, digits)
        if isinstance(v, dict):
            return {k: cut(x) for k, x in v.items()}
        if isinstance(v, list):
            return [cut(x) for x in v]
        return v
    return json.dumps(cut(value), indent=None, separators=(",", ":"))


def plans() -> list[dict]:
    out = [plan("stop only")]
    out += [plan(f"all out at {t:g}", final=t) for t in FULL_TARGETS]
    for first in FIRST_TARGETS:
        for share in PARTS:
            for far in FAR_TARGETS:
                tail = "" if far is None else f", rest out at {far:g}"
                frac = {1 / 3: "a third", 1 / 2: "half", 2 / 3: "two thirds"}[share]
                out.append(plan(f"{frac} out at {first:g}, rest to Monday{tail}",
                                take=[(first, share)], final=far))
                out.append(plan(f"{frac} out at {first:g}, rest stopped at entry{tail}",
                                take=[(first, share)], locks=[(first, 0.0)], final=far))
    out += [
        plan("lock entry once 1 ahead", locks=[(1.0, 0.0)]),
        plan("lock half once 1 ahead", locks=[(1.0, 0.5)]),
        plan("lock entry at 1, 1 at 2", locks=[(1.0, 0.0), (2.0, 1.0)]),
        plan("lock half at 1, 1.5 at 2", locks=[(1.0, 0.5), (2.0, 1.5)]),
    ]
    return out


def main() -> int:
    refresh = "--refresh" in sys.argv
    config = json.loads((TABLES / "crypto_long_short.json").read_text())["calls"]
    called = [(coin, side, c["suggested_leverage"]) for coin, sides in config.items()
              for side, c in sides.items() if c["allowed"]]
    weeks = {(coin, side): ce.traded_weeks(coin, side, refresh) for coin, side, _ in called}
    mondays = sorted(w["monday"] for ws in weeks.values() for w in ws)
    split = mondays[len(mondays) // 2]

    # The new replay must agree with the earlier one wherever they overlap.
    for coin, side, lev in called:
        for w in weeks[(coin, side)]:
            for target in (None, 1.0, 2.0, 3.0):
                mine, _ = play(w, side, coin, lev, plan("check", final=target))
                theirs = ce.play(w, side, coin, STOP, target, leverage=lev)
                assert abs(mine - theirs) < 1e-12, (coin, side, w["monday"], target, mine, theirs)

    def run(p: dict) -> dict:
        pooled, by_call, by_side = [], {}, {"long": [], "short": []}
        for coin, side, lev in called:
            rows = [(w["monday"], *play(w, side, coin, lev, p)) for w in weeks[(coin, side)]]
            by_call[f"{side} {coin}"] = summarise(rows, split)
            by_side[side] += rows
            pooled += rows
        return {**p, "pooled": summarise(pooled, split), "by_call": by_call,
                "by_side": {side: summarise(rows, split) for side, rows in by_side.items()}}

    results = [run(p) for p in plans()]
    base = results[0]["pooled"]

    def qualifies(row) -> bool:
        p = row["pooled"]
        return (p["first_half"]["avg"] > 0 and p["second_half"]["avg"] > 0
                and p["all"]["avg"] >= KEEP * base["all"]["avg"])

    print(f"{len(called)} called sides at their suggested leverage, {base['all']['weeks']} traded weeks, "
          f"halves split at {split.date()}; result per 1 USD of weekly stake")
    print(f"{'plan':<52} {'ahead':>6} {'avg':>7} {'worst':>6} {'1st':>7} {'2nd':>7} {'target':>7} "
          f"{'stopped':>8}  keeps")
    for row in sorted(results, key=lambda r: -r["pooled"]["all"]["ahead"]):
        p = row["pooled"]
        a = p["all"]
        hit = a["final_target"] if not row["take"] else a["first_target"]
        print(f"{row['name']:<52} {a['ahead'] * 100:>5.1f}% {a['avg'] * 100:>+6.2f}% {a['worst'] * 100:>+5.0f}% "
              f"{p['first_half']['avg'] * 100:>+6.2f}% {p['second_half']['avg'] * 100:>+6.2f}% "
              f"{hit * 100:>6.1f}% {(a['stopped'] + a['locked_out']) * 100:>7.1f}%  "
              f"{a['avg'] / base['all']['avg'] * 100:>3.0f}%{'  OK' if qualifies(row) and row is not results[0] else ''}")

    good = [r for r in results[1:] if qualifies(r)]
    chosen = max(good, key=lambda r: (r["pooled"]["all"]["ahead"], r["pooled"]["all"]["avg"])) if good else None
    if chosen and chosen["pooled"]["all"]["ahead"] <= base["all"]["ahead"]:
        chosen = None                               # no plan ends ahead more often than the stop alone
    print("\nchosen:", chosen["name"] if chosen else "none: the stop-only rule stays")
    if chosen:
        c, b = chosen["pooled"], base
        print(f"  ahead {c['all']['ahead'] * 100:.1f}% (stop only {b['all']['ahead'] * 100:.1f}%), "
              f"avg {c['all']['avg'] * 100:+.2f}% ({b['all']['avg'] * 100:+.2f}%), "
              f"worst {c['all']['worst'] * 100:+.0f}% ({b['all']['worst'] * 100:+.0f}%), "
              f"first target reached {c['all']['first_target'] * 100:.0f}% of weeks")
        for call, st in chosen["by_call"].items():
            was = results[0]["by_call"][call]["all"]
            print(f"  {call:<10} weeks {st['all']['weeks']:>4} ahead {st['all']['ahead'] * 100:>3.0f}% "
                  f"(was {was['ahead'] * 100:.0f}%) avg {st['all']['avg'] * 100:+.2f}% (was {was['avg'] * 100:+.2f}%)")
    # Per side: the nearest take profit that is no worse than the stop alone in both halves.
    def as_good(row, side) -> bool:
        mine, theirs = row["by_side"][side], results[0]["by_side"][side]
        return (mine["first_half"]["avg"] >= theirs["first_half"]["avg"]
                and mine["second_half"]["avg"] >= theirs["second_half"]["avg"])

    sides_out = {}
    print("\nper side, resting orders only (the stop, or the stop and one take profit):")
    for side in ("long", "short"):
        alone = results[0]["by_side"][side]
        full = sorted((r for r in results if r["final"] and not r["take"] and not r["locks"]),
                      key=lambda r: r["final"])
        print(f"  {side}s, {alone['all']['weeks']} weeks. stop only: ahead {alone['all']['ahead'] * 100:.1f}% "
              f"avg {alone['all']['avg'] * 100:+.2f}% (halves {alone['first_half']['avg'] * 100:+.2f}%, "
              f"{alone['second_half']['avg'] * 100:+.2f}%)")
        for r in full:
            st = r["by_side"][side]
            print(f"    take profit at {r['final']:<5g} reached {st['all']['final_target'] * 100:>3.0f}% of weeks  "
                  f"ahead {st['all']['ahead'] * 100:.1f}%  avg {st['all']['avg'] * 100:+.2f}% "
                  f"(halves {st['first_half']['avg'] * 100:+.2f}%, {st['second_half']['avg'] * 100:+.2f}%)"
                  f"{'  as good as the stop alone' if as_good(r, side) else ''}")
        pick = next((r for r in full if as_good(r, side)), None)
        any_plan = sum(as_good(r, side) for r in results[1:])
        print(f"    -> {'take profit at ' + format(pick['final'], 'g') + ' normal weeks' if pick else 'no take profit'}"
              f" ({any_plan} of {len(results) - 1} plans of any kind were as good as the stop alone)")
        one = next(r for r in full if r["final"] == 1.0)
        sides_out[side] = {
            "target": pick["final"] if pick else None,
            "at_one_normal_week": one["by_side"][side]["all"],
            "with": pick["by_side"][side] if pick else alone, "stop_only": alone,
            "plans_as_good": any_plan, "plans_tested": len(results) - 1,
            "reached": {format(r["final"], "g"): r["by_side"][side]["all"]["final_target"] for r in full},
        }

    OUT.write_text(rounded({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stop": STOP, "keep": KEEP, "split": str(split.date()), "by_side": sides_out,
        "called": [{"coin": c, "side": s, "leverage": lev} for c, s, lev in called],
        # every plan, without the per-call detail, so the table stays small
        "plans": [{k: v for k, v in r.items() if k != "by_call"} for r in results],
        "chosen_for_all_sides": chosen["name"] if chosen else None}, 5) + "\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
