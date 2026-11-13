"""Test whether the AI model adds anything to Neko's trade decisions.

At each decision point on past 5-minute prices the script shows the model the
same kind of scored long/short setups the live agent shows it, records which
one it picks (or that it holds), and resolves that pick against its stop and
target with real costs. The engine's own pick and a coin-flip direction are
resolved at the same moments for comparison.

The model sees only prices up to the decision bar. It needs network access to
the model provider; the prices come from the local cache.

    export OPENROUTER_API_KEY=...          # never put the key on the command line
    python research/scripts/test_llm_decisions.py --max-calls 40
    python research/scripts/test_llm_decisions.py --model deepseek/deepseek-chat

Options: --base-url for another OpenAI-compatible endpoint, --csv SYMBOL=path
for other price files (same format as replay_live_engine.py).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_live_engine as rle  # noqa: E402
from quant_strategy import (  # noqa: E402
    RSI_ENTRY_THRESHOLD, momentum_confirmed, pick_best_scenario, rsi, scenario_matrix,
)

OUT = rle.REPO / "research" / "exports" / "tables" / "llm_decision_test.json"
COST = rle.COST_LEVELS["with_platform_fee"]

SYSTEM = (
    "You are the decision step of a crypto perpetuals trading agent. You are given "
    "scored trade setups computed from recent 5-minute prices. Each row has a "
    "direction, an entry, a target, a stop, P(win) from a random-walk model, the "
    "reward/risk ratio R and the expected value EV. Rows marked CONFIRMED pass the "
    "momentum checks. Costs are about 1.1% of position size per round trip and are "
    "NOT included in EV. Pick the single best trade, or hold if none is worth the "
    "cost. Reply with one JSON object only: "
    '{"action":"trade"|"hold","symbol":"...","direction":"long"|"short","reasoning":"one sentence"}'
)


def candidates(closes_by: dict, prices: dict, blocked: set) -> list[dict]:
    """Best long and best short per symbol, flagged by whether the live gates pass."""
    rows = []
    for s in scenario_matrix(closes_by, prices, trader_type="scalp",
                             bars_per_year=rle.BARS_PER_YEAR):
        if s.symbol in blocked or s.horizon != "scalp":
            continue
        closes = closes_by[s.symbol]
        r = rsi(closes)
        rsi_ok = r >= RSI_ENTRY_THRESHOLD if s.direction == "long" else r <= 100 - RSI_ENTRY_THRESHOLD
        rows.append({"s": s, "confirmed": bool(rsi_ok and momentum_confirmed(closes, s.direction)),
                     "rsi": r})
    return rows


def describe(rows: list[dict], closes_by: dict) -> str:
    lines = []
    for sym, closes in closes_by.items():
        last = closes[-1]
        chg1h = (last / closes[-13] - 1) * 100
        chg6h = (last / closes[0] - 1) * 100
        lines.append(f"{sym}: price {last:.2f}, last hour {chg1h:+.2f}%, last 6 hours {chg6h:+.2f}%")
    lines.append("")
    for row in rows:
        tag = "CONFIRMED" if row["confirmed"] else "unconfirmed"
        lines.append(f"{row['s'].to_prompt()} | RSI {row['rsi']:.0f} | {tag}")
    return "\n".join(lines)


def ask(base_url: str, api_key: str, model: str, user: str, retries: int = 3) -> str:
    for attempt in range(retries):
        resp = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "temperature": 0.2,
                  "messages": [{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": user}]},
            timeout=90)
        if resp.status_code in (429, 500, 502, 503) and attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""
    return ""


def parse(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def score(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["outcome"] != "open"]
    n = len(closed)
    wins = sum(t["outcome"] == "win" for t in closed)
    lo, hi = rle.wilson(wins, n)
    nets = [t["gross_pct"] - COST for t in closed]
    return {"trades": n, "wins": wins, "win_rate": wins / n if n else None,
            "win_rate_ci95": [lo, hi],
            "avg_net_pct_after_costs": sum(nets) / n if n else None,
            "total_net_pct_after_costs": sum(nets)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default="openai/gpt-4o-mini")
    ap.add_argument("--base-url", default=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
    ap.add_argument("--max-calls", type=int, default=40, help="cap on paid model calls")
    ap.add_argument("--every-bars", type=int, default=36, help="bars between decisions (36 = 3h)")
    ap.add_argument("--csv", action="append", default=[])
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        print("Set OPENROUTER_API_KEY in the environment first.", file=sys.stderr)
        return 2

    sources = (dict(a.split("=", 1) for a in args.csv) if args.csv else
               {p.stem.split("_")[1]: str(p) for p in sorted(rle.CACHE.glob("CRYPTO_*_5m.csv"))})
    series = {sym: rle.load(Path(p)) for sym, p in sources.items()}
    idx = {sym: {b["t"]: k for k, b in enumerate(bars)} for sym, bars in series.items()}
    times = [b["t"] for b in series[max(series, key=lambda s: len(series[s]))]]

    rng = random.Random(11)
    llm, engine, coin, log = [], [], [], []
    holds = unparsed = calls = 0
    for n in range(rle.WINDOW, len(times) - 12, args.every_bars):
        if calls >= args.max_calls:
            break
        t = times[n]
        closes_by, prices = {}, {}
        for sym, bars in series.items():
            k = idx[sym].get(t)
            if k is not None and k >= rle.WINDOW:
                closes_by[sym] = [b["close"] for b in bars[k - rle.WINDOW + 1:k + 1]]
                prices[sym] = bars[k]["close"]
        rows = candidates(closes_by, prices, set())
        if not rows:
            continue

        def settle(scenario, bucket):
            k = idx[scenario.symbol][t]
            stop = abs(scenario.stop / scenario.entry - 1) * 100
            take = abs(scenario.target / scenario.entry - 1) * 100
            outcome, move, _ = rle.resolve(series[scenario.symbol], k, scenario.direction, stop, take)
            bucket.append({"t": t, "symbol": scenario.symbol, "direction": scenario.direction,
                           "outcome": outcome, "gross_pct": round(move, 4)})
            return outcome

        calls += 1
        try:
            reply = ask(args.base_url, api_key, args.model, describe(rows, closes_by))
        except requests.RequestException as exc:
            print(f"model call failed at {t}: {exc}", file=sys.stderr)
            return 1
        choice = parse(reply)
        entry = {"t": t, "reply": reply[:400]}
        if choice is None:
            unparsed += 1
            entry["result"] = "unparsed"
        elif str(choice.get("action", "")).lower() != "trade":
            holds += 1
            entry["result"] = "hold"
        else:
            want = (str(choice.get("symbol", "")).upper(), str(choice.get("direction", "")).lower())
            match = next((r for r in rows if (r["s"].symbol, r["s"].direction) == want), None)
            if match is None:
                unparsed += 1
                entry["result"] = "picked a row that was not offered"
            else:
                entry["result"] = f"{want[1]} {want[0]} -> {settle(match['s'], llm)}"
                entry["confirmed_row"] = match["confirmed"]
        log.append(entry)

        best = pick_best_scenario([r["s"] for r in rows if r["confirmed"]], {}, {},
                                  conviction_floor=rle.CONVICTION_FLOOR)
        if best is not None:
            settle(best, engine)
        settle(rng.choice(rows)["s"], coin)
        print(f"{t[:16]}  model: {entry['result']}")

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": args.model,
        "data": {"symbols": sorted(series), "from": times[0], "to": times[-1]},
        "decision_points": calls,
        "round_trip_cost_pct": COST,
        "note": ("Decisions overlap in time, so trades are not independent; treat the "
                 "intervals as optimistic. Funding, liquidation and stop slippage are not modelled."),
        "model_result": {**score(llm), "holds": holds, "unusable_replies": unparsed},
        "engine_only_result": score(engine),
        "coin_flip_result": score(coin),
        "decisions": log,
    }
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")

    def line(name, s):
        if not s["trades"]:
            return f"{name:<12} no closed trades"
        return (f"{name:<12} {s['trades']:>3} trades  win rate {s['win_rate'] * 100:5.1f}% "
                f"(95% range {s['win_rate_ci95'][0] * 100:.0f}-{s['win_rate_ci95'][1] * 100:.0f}%)  "
                f"avg after costs {s['avg_net_pct_after_costs']:+.2f}%")
    print()
    print(f"{calls} decision points, model {args.model}, cost {COST:.2f}% per round trip")
    print(line("model", result["model_result"]) + f"  ({holds} holds, {unparsed} unusable replies)")
    print(line("engine only", result["engine_only_result"]))
    print(line("coin flip", result["coin_flip_result"]))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
