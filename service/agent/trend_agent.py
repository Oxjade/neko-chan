"""Slow trend-following paper agent.

Rule (the one that survived research/scripts/strategy_search.py in both the
choose and the check period): on 4-hour bars, go long when the 10-day
exponential average crosses above the 40-day one, go short when it crosses
below, and hold until the opposite cross. A stop sits 4 ATR from entry.

It trades rarely: about one trade per coin every three to four weeks. It
enters only on a fresh cross, never in the middle of a trend, because that is
what was tested. No AI model is involved.

Sizing: equal share of the account per coin, no leverage.

Launched by the agent pool through live_agent.py when a bot's style is
"trend". Reads LIVE_AGENT_BOT_ID, LIVE_AGENT_WATCHLIST, TG_REGISTRY_PATH.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

FAST_BARS = 60          # 10 days of 4h bars
SLOW_BARS = 240         # 40 days
ATR_BARS = 14
STOP_ATR = 4.0
FEE_RESERVE = 0.98
BAR_MS = 4 * 3600 * 1000
HISTORY_BARS = 1500     # enough for the slow average to settle
CYCLE_SECONDS = int(os.getenv("TREND_CYCLE_SECONDS", "300"))
HL_URL = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz/info")

REPO = Path(__file__).resolve().parents[2]
TG_BOT_DIR = REPO / "service" / "tg_bot"
EXPORT_DIR = REPO / "research" / "exports"
LOG_HEADER = ["ts", "symbol", "direction", "action", "price", "quantity", "stop_pct",
              "take_pct", "fill_ok", "reasoning", "error", "leverage"]


def ema(values: list[float], span: int) -> list[float]:
    k = 2.0 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(bars: list[dict], n: int = ATR_BARS) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, bar in zip(bars[-n - 1:-1], bars[-n:]):
        trs.append(max(bar["h"] - bar["l"], abs(bar["h"] - prev["c"]), abs(bar["l"] - prev["c"])))
    return sum(trs) / n


def signal(bars: list[dict]) -> dict:
    """What the rule says after the last CLOSED bar.

    trend: +1 up, -1 down.  crossed: True only if the trend changed on that bar.
    """
    if len(bars) < SLOW_BARS + 2:
        return {"trend": 0, "crossed": False, "atr": None, "bars": len(bars)}
    closes = [b["c"] for b in bars]
    fast, slow = ema(closes, FAST_BARS), ema(closes, SLOW_BARS)
    now = 1 if fast[-1] > slow[-1] else -1
    before = 1 if fast[-2] > slow[-2] else -1
    return {"trend": now, "crossed": now != before, "atr": atr(bars), "bars": len(bars)}


def closed_bars(coin: str, now_ms: int | None = None) -> list[dict]:
    now_ms = now_ms or int(time.time() * 1000)
    resp = requests.post(HL_URL, json={"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "4h", "startTime": now_ms - HISTORY_BARS * BAR_MS,
        "endTime": now_ms}}, timeout=30)
    resp.raise_for_status()
    bars = [{"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
             "l": float(c["l"]), "c": float(c["c"])} for c in resp.json()]
    return [b for b in bars if b["t"] + BAR_MS <= now_ms]      # drop the forming bar


def mid_prices() -> dict[str, float]:
    resp = requests.post(HL_URL, json={"type": "allMids"}, timeout=30)
    resp.raise_for_status()
    return {k: float(v) for k, v in resp.json().items() if not k.startswith(("#", "@"))}


class Journal:
    """Decision log and price cache in the format the console reads."""

    def __init__(self, bot_id: int):
        self.log = EXPORT_DIR / f"live_agent_log_bot{bot_id}.csv"
        self.cache = EXPORT_DIR / f"live_agent_cache_bot{bot_id}.json"
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    def write(self, symbol="", direction="", action="hold", price="", qty="",
              stop_pct="", filled=False, why="", error="") -> None:
        fresh = not self.log.exists()
        with open(self.log, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if fresh:
                w.writerow(LOG_HEADER)
            w.writerow([datetime.now(timezone.utc).isoformat(), symbol, direction, action,
                        price, qty, stop_pct, "", filled, why, error, 1])

    def prices(self, prices: dict[str, float]) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        data = {sym: {"ts": ts, "symbol": sym, "price": px, "action": "hold"}
                for sym, px in prices.items()}
        self.cache.write_text(json.dumps(data, indent=1), encoding="utf-8")


def run_cycle(bot_id: int, symbols: list[str], gateway, journal: Journal,
              fetch_bars=closed_bars, fetch_mids=mid_prices) -> list[str]:
    """One pass over the watchlist. Returns a line per action taken."""
    store = gateway.store
    mids = fetch_mids()
    prices = {s: mids[s] for s in symbols if s in mids}
    journal.prices(prices)
    positions = {p["symbol"]: p for p in store.positions(bot_id)}
    equity = gateway.equity(bot_id, prices)
    # margin locked in open positions is part of the account too
    equity += sum(p["qty"] * p["entry_price"] / max(p["leverage"], 1.0) for p in positions.values())
    done, notes = [], []
    for sym in symbols:
        price = prices.get(sym)
        if not price:
            notes.append(f"{sym}: no price")
            continue
        sig = signal(fetch_bars(sym))
        if not sig["trend"]:
            notes.append(f"{sym}: only {sig['bars']} bars of history, need {SLOW_BARS + 2}")
            continue
        want = "long" if sig["trend"] > 0 else "short"
        pos = positions.get(sym)
        if pos:
            stop = pos.get("stop_loss")
            stopped = stop and (price <= stop if pos["direction"] == "long" else price >= stop)
            if stopped or pos["direction"] != want:
                why = "stop hit" if stopped else "trend reversed"
                bar = fetch_bars(sym)[-1]["t"]
                r = gateway.close(bot_id, sym, price, idempotency_key=f"trend-x-{bot_id}-{sym}-{bar}")
                journal.write(sym, pos["direction"], "sell" if pos["direction"] == "long" else "cover",
                              price, pos["qty"], filled=bool(r.get("ok")), why=f"Closed: {why}.",
                              error=r.get("error", ""))
                done.append(f"closed {pos['direction']} {sym} ({why})")
                pos = None
                if stopped:
                    continue          # after a stop, wait for the next cross
            else:
                notes.append(f"{sym}: holding {pos['direction']}")
                continue
        if pos is None and sig["crossed"] and sig["atr"]:
            # keep 2% back so entry and exit fees never bounce the order
            notional = equity / len(symbols) * FEE_RESERVE
            qty = notional / price
            stop_dist = STOP_ATR * sig["atr"]
            stop = price - stop_dist if want == "long" else price + stop_dist
            bar = fetch_bars(sym)[-1]["t"]
            r = gateway.open(bot_id, sym, want, qty, price, leverage=1.0, stop_loss=stop,
                             idempotency_key=f"trend-o-{bot_id}-{sym}-{bar}")
            journal.write(sym, want, "buy" if want == "long" else "short", price, qty,
                          stop_pct=round(stop_dist / price * 100, 2), filled=bool(r.get("ok")),
                          why=f"The 10-day average crossed {'above' if want == 'long' else 'below'} "
                              f"the 40-day average.", error=r.get("error", ""))
            done.append(f"opened {want} {sym}" if r.get("ok") else f"{sym} open failed: {r.get('error')}")
        elif pos is None:
            notes.append(f"{sym}: trend is {'up' if want == 'long' else 'down'}, waiting for the next cross")
    if not done:
        journal.write(why="No change. " + "; ".join(notes))
    return done or notes


def main() -> int:
    bot_id = int(os.getenv("LIVE_AGENT_BOT_ID", "0") or 0)
    symbols = [s.strip().upper() for s in os.getenv("LIVE_AGENT_WATCHLIST", "").split(",") if s.strip()]
    symbols = symbols or ["BTC", "ETH", "SOL"]
    if str(TG_BOT_DIR) not in sys.path:
        sys.path.insert(0, str(TG_BOT_DIR))
    from paper_gateway import PaperGateway
    from paper_store import PaperStore
    db = os.getenv("TG_REGISTRY_PATH") or str(TG_BOT_DIR / "registry.db")
    gateway = PaperGateway(PaperStore(db))
    journal = Journal(bot_id)
    once = "--once" in sys.argv
    while True:
        try:
            for line in run_cycle(bot_id, symbols, gateway, journal):
                print(f"[trend bot {bot_id}] {line}", flush=True)
        except Exception as exc:  # network blips must not kill the agent
            journal.write(error=f"cycle failed: {exc}")
            print(f"[trend bot {bot_id}] cycle failed: {exc}", file=sys.stderr, flush=True)
        if once:
            return 0
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
