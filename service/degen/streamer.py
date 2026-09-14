"""Streamer (§3.1): cursor-resumed Suipump event ingestion + internal bus.

GraphQL event polling (JSON-RPC is deprecated on public fullnodes). One poller per
launchpad package so a Blast outage never stalls Suipump. Events normalized to a
small internal shape and dispatched to subscribers (sniper, copy, order re-arm).
Dedup by (sender, seq, type) so a resumed cursor that re-delivers a boundary event
is idempotent. gRPC (SubscribeTransactionEvents) is the P-latency upgrade; this
class keeps the SAME interface so the swap is drop-in.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from . import constants as K
from .chain import Chain, GqlError
from .db import DegenLedger
from .launchpad import SuipumpLaunchpad

log = logging.getLogger(__name__)


@dataclass
class NormEvent:
    kind: str          # CurveCreated | Graduated | PoolRecorded | Trade | Unknown
    launchpad: str
    curve_id: str
    creator: str
    token_type: str
    payload: dict
    ts_ms: int
    digest: str
    seq: int


class EventBus:
    def __init__(self):
        self._subs: dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()

    def on(self, kind: str, cb) -> None:
        with self._lock:
            self._subs[kind].append(cb)

    def emit(self, ev: NormEvent) -> None:
        with self._lock:
            handlers = list(self._subs.get(ev.kind, [])) + list(self._subs.get("*", []))
        for cb in handlers:
            try:
                cb(ev)
            except Exception:
                log.exception("event handler failed kind=%s", ev.kind)


# Suipump module event names (verified struct list, docs §0.1)
_EVENT_KIND = {
    "CurveCreated": "CurveCreated",
    "Graduated": "Graduated",
    "PoolRecorded": "PoolRecorded",
}


class SuipumpStreamer:
    def __init__(self, ch: Chain, ledger: DegenLedger, bus: EventBus,
                 packages: tuple = K.SUIPUMP_PACKAGES, poll_s: float = 1.5,
                 page: int = 50):
        self.ch = ch
        self.ledger = ledger
        self.bus = bus
        self.packages = packages
        self.poll_s = poll_s
        self.page = page
        self._seen: "deque[tuple]" = deque(maxlen=5000)
        self._seen_set = set()
        self._stop = threading.Event()
        self.recent_curvecreated: "deque[dict]" = deque(maxlen=200)  # card browse

    # ---------------- cursor ----------------
    def _cursor_key(self, pkg: str) -> str:
        return f"events:{pkg}"

    def _mark_seen(self, key: tuple) -> bool:
        if key in self._seen_set:
            return False
        self._seen_set.add(key)
        self._seen.append(key)
        if len(self._seen_set) > self._seen.maxlen + 1000:
            old = self._seen.popleft()
            self._seen_set.discard(old)
        return True

    # ---------------- parse ----------------
    def _normalize(self, pkg: str, node: dict) -> NormEvent | None:
        t = node.get("type", "")
        # ...::<Module>::<Name>
        parts = t.split("::")
        name = parts[-1] if parts else ""
        kind = _EVENT_KIND.get(name, "Unknown")
        j = node.get("json") or {}
        curve_id = j.get("curve_id") or j.get("id") or ""
        tok = j.get("token_type") or j.get("type") or ""
        creator = node.get("sender", "") or j.get("creator", "")
        ts = j.get("created_at_ms") or j.get("ts_ms") or 0
        try:
            ts_ms = int(ts)
        except (TypeError, ValueError):
            ts_ms = 0
        return NormEvent(kind=kind, launchpad="suipump", curve_id=curve_id,
                         creator=creator, token_type=tok, payload=j, ts_ms=ts_ms,
                         digest=node.get("digest", ""), seq=node.get("seq", 0))

    # ---------------- one poll ----------------
    def poll_once(self) -> int:
        """Fetch + dispatch new events for every package. Returns count emitted."""
        emitted = 0
        for pkg in self.packages:
            base = f"{pkg}::{K.SUIPUMP_MODULE}"
            for ev_name in _EVENT_KIND.values():
                try:
                    nodes, cursor, has_next = self.ch.events(f"{base}::{ev_name}",
                                                             first=self.page,
                                                             after=self.ledger.get_cursor(self._cursor_key(ev_name + ":" + pkg)))
                except GqlError as exc:
                    log.warning("streamer poll %s: %s", ev_name, exc)
                    continue
                for node in nodes:
                    ne = self._normalize(pkg, node)
                    if not ne:
                        continue
                    if not self._mark_seen((pkg, ne.kind, ne.seq, ne.digest)):
                        continue
                    if ev_name == "CurveCreated":
                        self.recent_curvecreated.appendleft(
                            {"curve_id": ne.curve_id, "creator": ne.creator,
                             "symbol": ne.payload.get("symbol", ""), "name": ne.payload.get("name", ""),
                             "ts_ms": ne.ts_ms})
                    self.bus.emit(ne)
                    emitted += 1
                if cursor:
                    self.ledger.set_cursor(self._cursor_key(ev_name + ":" + pkg), cursor)
        return emitted

    # ---------------- loop ----------------
    def run_forever(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.poll_once()
            except Exception:
                log.exception("streamer poll crashed; backoff")
                time.sleep(3)
            self._stop.wait(max(0.0, self.poll_s - (time.time() - t0)))

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run_forever, name="degen-streamer", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
