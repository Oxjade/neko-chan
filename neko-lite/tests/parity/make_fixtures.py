"""Record one frozen moment of market data and what the Python reference makes of it.

The TypeScript core must give the same answers as service/server from the
same inputs. This script:

  1. freezes the clock the Python modules read;
  2. runs the Python functions once against the live sources, saving every raw
     upstream response byte for byte (Hyperliquid, Fear & Greed, the ECB zip);
  3. forgets everything, runs the same functions again from the saved
     responses only, and saves their results.

tests/parity/parity.test.ts then feeds the same saved responses to the
TypeScript core and compares every field.

Run it with the repository's Python environment (about four minutes: the
long price histories are fetched slowly to stay inside Hyperliquid's limit):

    .venv/bin/python neko-lite/tests/parity/make_fixtures.py

Output goes to neko-lite/tests/parity/fixtures/ (git-ignored, about 15 MB).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time as real_time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = HERE / "fixtures"
sys.path.insert(0, str(REPO / "service" / "server"))

import crypto_mood  # noqa: E402
import forex_weekly  # noqa: E402
import market_data  # noqa: E402
import terminal  # noqa: E402
import terminal_adv  # noqa: E402
import weekly_calls  # noqa: E402

STAKE = 18.0
CHARTS = (("BTC", "4h"), ("ETH", "1h"), ("xyz:NVDA", "1d"))
BOOKS = ("ETH", "xyz:NVDA")
WEIGHT_PER_MINUTE = 900          # Hyperliquid allows 1200 per IP; leave room for anything else running


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class Upstream:
    """Fetches and saves raw responses in record mode; serves only saved ones in replay mode."""

    def __init__(self):
        self.mode = "record"
        self.lock = threading.Lock()
        self.hl: dict[str, bytes] = {}
        self.http: dict[str, bytes] = {}
        self.spent: list[tuple[float, float]] = []
        self.misses: list[str] = []

    # -- Hyperliquid ---------------------------------------------------------
    def weight(self, payload: dict) -> float:
        if payload["type"] == "candleSnapshot":
            req = payload["req"]
            ms = {"1h": 3600e3, "4h": 4 * 3600e3, "1d": 24 * 3600e3}[req["interval"]]
            return 20 + min(5001, (req["endTime"] - req["startTime"]) / ms + 1) / 60
        return 2 if payload["type"] == "l2Book" else 20

    def wait_for_room(self, weight: float):
        while True:
            now = real_time.monotonic()
            self.spent = [(at, w) for at, w in self.spent if now - at < 60]
            if sum(w for _, w in self.spent) + weight <= WEIGHT_PER_MINUTE:
                self.spent.append((now, weight))
                return
            real_time.sleep(1.0)

    def post(self, payload: dict):
        key = canonical(payload)
        with self.lock:
            if key not in self.hl:
                if self.mode == "replay":
                    self.misses.append(key)
                    raise market_data.DataError("not recorded: " + key)
                self.hl[key] = self.fetch_hl(payload)
            return json.loads(self.hl[key])

    def fetch_hl(self, payload: dict) -> bytes:
        for attempt in range(8):
            self.wait_for_room(self.weight(payload))
            try:
                resp = requests.post(market_data.HL_URL, json=payload, timeout=60)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(str(resp.status_code))
                resp.raise_for_status()
                json.loads(resp.content)
                print(f"  recorded {payload.get('type')} {payload.get('req', payload).get('coin', payload.get('dex', ''))}"
                      f" {payload.get('req', {}).get('interval', '')} ({len(resp.content):,} bytes)", flush=True)
                return resp.content
            except (requests.RequestException, ValueError) as exc:
                wait = min(60, 5 * 2 ** attempt)
                print(f"  {type(exc).__name__} {exc}; trying again in {wait}s", flush=True)
                real_time.sleep(wait)
        raise SystemExit(f"could not record {canonical(payload)}")

    # -- plain GETs (Fear & Greed, ECB) ----------------------------------------
    def get(self, url: str, timeout: float = 30):
        with self.lock:
            if url not in self.http:
                if self.mode == "replay":
                    self.misses.append(url)
                    raise requests.ConnectionError("not recorded: " + url)
                resp = requests.get(url, timeout=timeout)
                resp.raise_for_status()
                self.http[url] = resp.content
                print(f"  recorded {url} ({len(resp.content):,} bytes)", flush=True)
            body = self.http[url]
        return SimpleNamespace(content=body, raise_for_status=lambda: None, json=lambda: json.loads(body))


def run_reference(now: datetime) -> dict:
    """Everything the TypeScript app serves, from the Python functions."""
    out = {
        "week": weekly_calls.this_week(),
        "scoreboard_0": weekly_calls.scoreboard(STAKE, 0.0),
        "scoreboard_5": weekly_calls.scoreboard(STAKE, 5.0),
        "terminal": terminal.snapshot(STAKE, now),
        "advanced": terminal_adv.snapshot(STAKE, now),
    }
    for symbol, interval in CHARTS:
        out[f"chart_{symbol.replace(':', '_')}_{interval}"] = terminal.chart(symbol, interval)
    for symbol in BOOKS:
        out[f"book_{symbol.replace(':', '_')}"] = market_data.order_book(symbol)
    return out


def forget():
    market_data._cache.clear()
    forex_weekly._state.update(loaded_at=0.0, daily=None)


def main():
    # Whole seconds, so the Python modules' int(time.time() * 1000) is exact.
    now_ms = int(real_time.time()) * 1000
    now = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=now_ms)
    clock = SimpleNamespace(time=lambda: now_ms / 1000)
    assert int(clock.time() * 1000) == now_ms

    upstream = Upstream()
    shim = SimpleNamespace(get=upstream.get, post=requests.post, RequestException=requests.RequestException)
    market_data._post = upstream.post
    market_data.time = clock
    weekly_calls.time = clock
    forex_weekly.time = clock
    crypto_mood.requests = shim
    forex_weekly.requests = shim
    scratch = Path(tempfile.mkdtemp(prefix="neko-parity-"))
    forex_weekly.DISK_CACHE = scratch / "ecb.csv"            # never write into the repository
    forex_weekly.REPO_COPY = scratch / "missing.csv"         # and never fall back to an old copy

    print(f"Frozen at {now.isoformat()} ({now_ms}). Recording upstream responses...", flush=True)
    # The worksheet loads its markets on eight threads and hides failures; fetch the
    # long histories one at a time first so none is lost to the rate limit.
    for symbol in terminal_adv.WORKSHEET:
        try:
            market_data.candles(symbol)
        except market_data.DataError as exc:
            print(f"  {symbol}: {exc}")
    run_reference(now)

    print("Replaying the Python functions from the recorded responses only...", flush=True)
    forget()
    upstream.mode = "replay"
    expected = run_reference(now)
    if upstream.misses:
        raise SystemExit(f"the replay asked for {len(upstream.misses)} responses that were not recorded: "
                         f"{upstream.misses[:3]}")
    live = market_data.catalogue()
    missing = [s for s in terminal_adv.WORKSHEET if s in live
               and s not in {r["symbol"] for r in expected["advanced"]["worksheet"]}]

    # The TypeScript core reads the week's mood from the 200-day request; check it says the same.
    strip = lambda rows: [{k: v for k, v in r.items() if k != "time_until_update"} for r in rows]
    recent = json.loads(upstream.http[crypto_mood.FNG_URL])["data"]
    longer = json.loads(upstream.http["https://api.alternative.me/fng/?limit=200&format=json"])["data"]
    if strip(recent) != strip(longer[:len(recent)]):
        raise SystemExit("the 21-day and 200-day mood responses disagree; run again")

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "raw" / "hl").mkdir(parents=True)
    (OUT / "expected").mkdir()
    index = []
    for n, (key, body) in enumerate(upstream.hl.items()):
        name = f"{n:04d}.json"
        (OUT / "raw" / "hl" / name).write_bytes(body)
        index.append({"payload": json.loads(key), "file": name})
    (OUT / "raw" / "hl" / "index.json").write_text(json.dumps(index, indent=1))
    http_index = {}
    for n, (url, body) in enumerate(upstream.http.items()):
        name = f"http-{n}" + (".zip" if url.endswith(".zip") else ".json")
        (OUT / "raw" / name).write_bytes(body)
        http_index[url] = name
    (OUT / "raw" / "http.json").write_text(json.dumps(http_index, indent=1))
    for name, value in expected.items():
        (OUT / "expected" / f"{name}.json").write_text(json.dumps(value, allow_nan=False))
    (OUT / "meta.json").write_text(json.dumps({
        "now_ms": now_ms, "now": now.isoformat(), "stake": STAKE,
        "charts": [list(c) for c in CHARTS], "books": list(BOOKS),
        "hyperliquid_responses": len(index), "worksheet_rows": len(expected["advanced"]["worksheet"]),
        "worksheet_missing": missing,
    }, indent=1))
    shutil.rmtree(scratch, ignore_errors=True)
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"Saved {len(index)} Hyperliquid responses and {len(expected)} reference results "
          f"({size / 1e6:.1f} MB) to {OUT}")
    print(f"Worksheet rows: {len(expected['advanced']['worksheet'])} of {len(terminal_adv.WORKSHEET)}"
          + (f"; left out by Python: {missing}" if missing else ""))


if __name__ == "__main__":
    main()
