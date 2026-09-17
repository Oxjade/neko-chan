"""Fast order loop + plan compiler (§3.4 / §3.4a).

Deterministic: reads prices, fires armed rungs on trigger, re-sizes remaining
lanes for the new average, and flips venue curve→pool on Graduated. The LLM
(§3.4a slow layer) only *plans*; nothing here calls a model. The model's output
is compiled by `compile_plan_to_orders` and passes the same caps/validators.
"""

from __future__ import annotations

import logging
import time

from . import constants as K
from .chain import Chain
from .db import DegenLedger, utcnow
from .launchpad import resolve_input
from .metrics import compute
from .validate import run_gauntlet

log = logging.getLogger(__name__)


def lane_shape_for(style: str, budget_sui: float) -> list:
    """Preset ladders (§3.4 editor). Light/Standard/Heavy + momentum."""
    if style == "light":
        return [{"dip_pct": -15, "size_share": 0.5}, {"dip_pct": -35, "size_share": 0.5}]
    if style == "heavy":
        return [{"dip_pct": -10, "size_share": 0.15}, {"dip_pct": -25, "size_share": 0.25},
                {"dip_pct": -40, "size_share": 0.30}, {"dip_pct": -55, "size_share": 0.30}]
    if style == "momentum":
        return [{"rise_pct": 15, "size_share": 0.5}, {"rise_pct": 35, "size_share": 0.5}]
    # standard
    return [{"dip_pct": -10, "size_share": 0.34}, {"dip_pct": -25, "size_share": 0.33},
            {"dip_pct": -40, "size_share": 0.33}]


def compile_plan_to_orders(led: DegenLedger, bot_id: int, plan_id: int, *,
                           wallet: str = "") -> list[int]:
    """Turn a degen_plan's lane_shape_json into armed conditional orders (§3.4a)."""
    with led._lock:
        row = led._conn.execute("SELECT * FROM degen_plan WHERE id=?", (plan_id,)).fetchone()
    if not row:
        return []
    p = dict(row)
    import json as _json
    lanes = _json.loads(p.get("lane_shape_json") or "[]")
    pos = led.position_by_curve(bot_id, p["curve_id"], wallet)
    if not pos:
        return []
    ids = []
    for i, ln in enumerate(lanes):
        amt = round(p_total(pos) * ln.get("size_share", 0) , 6)
        trig = ln.get("dip_pct")
        mode = "price" if trig is not None else "price"
        target = pos["avg_entry_sui"] * (1 + (trig or ln.get("rise_pct", 0)) / 100.0)
        oid = led.add_order(bot_id, wallet=wallet, intent="buy", otype="dca",
                            launchpad=p["launchpad"], curve_id=p["curve_id"],
                            token_type=pos.get("token_type", ""),
                            target_price=target, trigger_mode=mode, qty_sui=amt,
                            lane_index=i, plan_id=plan_id,
                            idempotency_key=f"plan{plan_id}:{i}:{wallet}")
        if oid > 0:
            ids.append(oid)
    return ids


def p_total(pos) -> float:
    return float(pos.get("entry_sui", 0) or 0)


# ---------------------------------------------------------------- evaluator
class OrderEvaluator:
    """Polls prices for armed orders; fires crossed ones through the executor.
    Re-runs the §5.3/§5.4 gauntlet at TRIGGER time (§3.4 rule), not setup time."""

    def __init__(self, ch: Chain, led: DegenLedger, executor, poll_s: float = 1.0):
        self.ch, self.led, self.executor, self.poll_s = ch, led, executor, poll_s
        self._stop = False
        self._samples: dict[str, list] = {}  # curve_id -> [(ts,x,y)] for the fit

    # -- one tick --
    def tick(self, bot_ids: list[int] | None = None) -> int:
        fired = 0
        orders = self.led.armed_orders(bot_ids[0] if bot_ids and len(bot_ids) == 1 else None)
        by_bot: dict[int, list] = {}
        for o in orders:
            by_bot.setdefault(o["bot_id"], []).append(o)
        for bot_id, os_ in by_bot.items():
            cfg = self.led.get_config(bot_id)
            if not cfg.get("enabled"):
                continue
            for o in os_:
                if o["state"] == "paused":
                    continue
                price = self._price_for(o)
                if price is None:
                    continue
                if self._crossed(o, price):
                    if self._fire(o, bot_id, cfg):
                        fired += 1
        return fired

    def _price_for(self, o):
        st = resolve_input(self.ch, o["curve_id"] or o["pool_id"] or o["token_type"])
        if st.kind == "unknown" or not (st.curve_obj or st.token_type):
            return None
        # collect reserve samples for the grad fit
        if st.kind == "curve" and st.sui_reserve_mist and st.token_reserve:
            s = self._samples.setdefault(o["curve_id"], [])
            s.append((int(time.time() * 1000), st.sui_reserve_mist, st.token_reserve))
            if len(s) > 40:
                del s[: len(s) - 40]
            self.led.save_fit(o["curve_id"], 0.0, 0.0, len(s))
        m = compute(self.ch, st, reserves_samples=[(x, y) for _, x, y in self._samples.get(o["curve_id"], [])])
        return m.price_sui if m.price_sui else None

    @staticmethod
    def _crossed(o, price) -> bool:
        if not o.get("target_price"):
            return True  # immediate/market
        return price <= o["target_price"] if o["intent"] == "buy" else price >= o["target_price"]

    def _fire(self, o, bot_id, cfg) -> bool:
        if o["intent"] == "sell":
            res = self._sell(o, bot_id, cfg)
        else:
            res = self._buy(o, bot_id, cfg)
        if res.get("ok"):
            self.led.set_order(o["id"], "fired", tx_digest=res.get("digest", ""))
            self._reprice_remaining_lanes(o)
            return True
        if "caps:" in str(res.get("error", "")) or "daily loss" in str(res.get("error", "")):
            self.led.set_order(o["id"], "cancelled", error=res.get("error", ""))
        return False

    def _buy(self, o, bot_id, cfg):
        st = resolve_input(self.ch, o["curve_id"])
        if st.kind == "graduating":
            return {"ok": False, "error": "graduating — untradeable, holding"}
        if st.kind == "pool":
            return self.executor.buy_post_grad(bot_id, o, st)
        g = run_gauntlet(self.ch, st)
        if not g.allowed:
            return {"ok": False, "error": "gauntlet: " + "; ".join(g.blocks)}
        return self.executor.buy(bot_id, launchpad=o["launchpad"], curve_id=o["curve_id"],
                                 token_type=o["token_type"] or st.token_type,
                                 curve_isv=st.curve_obj.get("shared_version", 0),
                                 sui_amount=o["qty_sui"], min_out=0, wallet=o["wallet"],
                                 idem=f"ord{o['id']}", otype=o["otype"],
                                 target_price=o["target_price"], lane_index=o["lane_index"])

    def _sell(self, o, bot_id, cfg):
        st = resolve_input(self.ch, o["curve_id"] or o["pool_id"])
        if st.kind == "graduating":
            return {"ok": False, "error": "graduating — exit paused until pool live"}
        pos = self.led.position_by_curve(bot_id, o["curve_id"], o["wallet"])
        if not pos:
            return {"ok": False, "error": "no position to sell"}
        # flat 0.5% company fee on exit (the only fee model). Without this the
        # executor defaults fee_bps=0 and the sell leg collects nothing.
        return self.executor.sell(bot_id, o, st, pos, fee_bps=K.PLATFORM_FEE_BPS)

    def _reprice_remaining_lanes(self, fired):
        pos = self.led.position_by_curve(fired["bot_id"], fired["curve_id"], fired["wallet"])
        if not pos:
            return
        with self.led._lock:
            rows = self.led._conn.execute(
                """SELECT id, lane_index FROM degen_order WHERE plan_id=? AND state='armed'
                   AND lane_index>?""", (fired["plan_id"], fired["lane_index"])).fetchall()
        for r in rows:
            # remaining DCA rungs stay at their original trigger prices (dip ladder
            # is anchored to the FIRST avg at arm time); only the qty of the basket
            # is implicitly re-covered by the new avg in position (already handled
            # in upsert_position). No silent re-arming.
            pass

    # -- venue flip --
    def on_graduated(self, ev) -> None:
        """Curver→pool: re-anchor open positions + armed rungs (§3.1a)."""
        cid = ev.curve_id
        with self.led._lock:
            positions = [dict(r) for r in self.led._conn.execute(
                "SELECT * FROM degen_position WHERE curve_id=? AND venue='curve'", (cid,))]
        for pos in positions:
            st = resolve_input(self.ch, cid)
            if st.kind == "pool":
                self.led.set_position(pos["id"], venue="pool", pool_id=st.pool_id)
        # armed curve buys for this token now route through pool (§3.5)

    def loop(self):
        import threading
        while not self._stop:
            t0 = time.time()
            try:
                self.tick()
            except Exception:
                log.exception("evaluator tick failed")
            time.sleep(max(0.05, self.poll_s - (time.time() - t0)))

    def start(self):
        import threading
        self._th = threading.Thread(target=self.loop, name="degen-eval", daemon=True)
        self._th.start()

    def stop(self):
        self._stop = True
