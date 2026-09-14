"""Sniper (§3.2): deployer-watch model. Fires only on watched wallets' launches.

Physics (§3.2/§3.2a): cannot buy before a same-tx dev buy — we win *first public
entry*, ~next checkpoint. Hot path: event → gauntlet (cached funding graph) →
size+clamp → executor. No LLM. `lag_ms` logged for the honest stats panel.
"""

from __future__ import annotations

import logging
import time

from . import constants as K
from .chain import Chain
from .db import DegenLedger
from .streamer import NormEvent
from .validate import dev_screen

log = logging.getLogger(__name__)


class Sniper:
    MIN_PRIOR_LAUNCHES_FOR_AUTO = 1      # earn-the-rope: <1 clean launch ⇒ ask mode
    COOLDOWN_S = 3600                    # 1 launch / deployer / 24h

    def __init__(self, ch: Chain, led: DegenLedger, executor):
        self.ch, self.led, self.executor = ch, led, executor
        self._last_fire: dict[tuple, float] = {}

    def attach(self, bus) -> None:
        bus.on("CurveCreated", self.on_curve_created)
        bus.on("Graduated", self._on_graduated_sample)

    def _on_graduated_sample(self, ev: NormEvent) -> None:
        pass  # metrics sample already recorded by streamer/evaluator

    def on_curve_created(self, ev: NormEvent) -> None:
        watchers = self.led.watched_deployers()
        mine = [w for w in watchers if w["deployer"].lower() == ev.creator.lower()]
        if not mine:
            return
        w = mine[0]
        now = time.time()
        # anti-serial-rug: 1 SUCCESSFUL snipe per deployer / 24h (rejections don't
        # consume the slot — a dev that fails screens is not "launched").
        if w.get("launches", 0) > 0:
            key = (w["bot_id"], ev.creator.lower())
            if now - self._last_fire.get(key, 0) < self.COOLDOWN_S:
                self.led.log_snipe(w["bot_id"], ev.launchpad, ev.curve_id, ev.creator,
                                   "rejected", "deployer cooldown (1/24h)")
                return
        # §5.4 dev-screen at fire time (not watch time)
        st = self._state(ev, w["bot_id"])
        if st is None:
            return
        # §5.3 forged-curve validation in the hot path (cheap, no network round-trip
        # beyond the resolve we already did) — the design's only defense against a
        # crafted Curve-shaped object.
        from .validate import curve_validator
        cerr = curve_validator(self.ch, st)
        if cerr:
            self.led.log_snipe(w["bot_id"], ev.launchpad, ev.curve_id, ev.creator,
                               "rejected", "validator: " + "; ".join(cerr))
            return
        t0 = time.perf_counter()
        ds = dev_screen(self.ch, st)
        if not ds["ok"]:
            self.led.log_snipe(w["bot_id"], ev.launchpad, ev.curve_id, ev.creator,
                               "rejected", "; ".join(ds["blocks"]))
            fails = w.get("fails", 0) + 1
            self.led.set_watch(w["bot_id"], ev.creator, fails=fails)
            if fails >= 2:
                self.led.set_watch(w["bot_id"], ev.creator, enabled=False)
            return
        if w["mode"] == "ask" and (w.get("launches", 0) < self.MIN_PRIOR_LAUNCHES_FOR_AUTO
                                   or w.get("fails", 0) > 0):
            self.led.log_snipe(w["bot_id"], ev.launchpad, ev.curve_id, ev.creator,
                               "ask", "awaiting confirm (suggest mode)")
            return
        size = w["size_sui"]
        if w.get("spread_burst") and len(self.led.bundle_wallets(w["bot_id"])) >= 2:
            legs = self.led_spread(w["bot_id"], size)
            res = self.executor.spread_burst(w["bot_id"], launchpad=ev.launchpad,
                                             curve_id=ev.curve_id, token_type=st.token_type,
                                             curve_isv=st.curve_obj.get("shared_version", 0),
                                             total_sui=size, min_out_each=0, legs=legs,
                                             idem=f"burst{ev.curve_id}")
        else:
            res = self.executor.buy(w["bot_id"], launchpad=ev.launchpad,
                                    curve_id=ev.curve_id, token_type=st.token_type,
                                    curve_isv=st.curve_obj.get("shared_version", 0),
                                    sui_amount=size, min_out=0,
                                    idem=f"snipe{ev.curve_id}:{w['bot_id']}",
                                    source="sniper")
        lag = int((time.perf_counter() - t0) * 1000)
        if res.get("ok"):
            self._last_fire[(w["bot_id"], ev.creator.lower())] = now
        self.led.log_snipe(w["bot_id"], ev.launchpad, ev.curve_id, ev.creator,
                           "fired" if res.get("ok") else "rejected",
                           "" if res.get("ok") else str(res.get("error", ""))[:120],
                           lag_ms=lag, filled=bool(res.get("ok")))
        self.led.set_watch(w["bot_id"], ev.creator,
                           launches=(w.get("launches", 0) + (1 if res.get("ok") else 0)))

    def _state(self, ev, bot_id: int):
        from .launchpad import resolve_input
        st = resolve_input(self.ch, ev.curve_id)
        if st.kind != "curve":
            self.led.log_snipe(bot_id, ev.launchpad, ev.curve_id, ev.creator,
                               "rejected", f"already {st.kind}")
            return None
        return st

    def _auto_unwatch(self, w):
        fails = w.get("fails", 0)
        if fails >= 2:
            self.led.set_watch(w["bot_id"], w["deployer"], enabled=False)

    def led_spread(self, bot_id, total):
        from .bundle import BundleManager
        return BundleManager(self.led).funding_plan(bot_id, total)
