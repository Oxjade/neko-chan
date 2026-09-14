import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service"))

from cryptography.fernet import Fernet

from degen import constants as K
from degen.db import DegenLedger
from degen.sniper import Sniper
from degen.streamer import EventBus, NormEvent
from test_degen_core import MockChain, curve_json  # noqa: F401 (helpers)


CID = "0x" + "cc" * 32
DEV = "0x" + "ab" * 32
TOK = "0x" + "dd" * 32 + "::suipump::SUIPUMP"


def _chain(thr=9000e9, sui=10 ** 9, tok=10 ** 14):
    return MockChain({CID: curve_json(CID, DEV, int(sui), int(tok), int(thr))},
                     coins={DEV: [{"objectId": "0x" + "11" * 32, "version": 1,
                                   "digest": "22" * 32, "balance_mist": 10 ** 10}]})


class SpyExec:
    def __init__(self):
        self.buys, self.bursts = [], []

    def buy(self, bot_id, **kw):
        self.buys.append(kw)
        return {"ok": True, "digest": "0x9", "order_id": len(self.buys)}

    def spread_burst(self, bot_id, **kw):
        self.bursts.append(kw)
        return {"ok": True, "legs_ok": 3, "legs_total": 3}


def _ev():
    return NormEvent(kind="CurveCreated", launchpad="suipump", curve_id=CID,
                     creator=DEV, token_type=TOK,
                     payload={"curve_id": CID, "symbol": "X"}, ts_ms=1, digest="d", seq=1)


def _led(watched=True, mode="auto"):
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1, budget_sui=20)
    if watched:
        led.watch_deployer(1, DEV, size_sui=0.5, mode=mode)
    return led


def test_sniper_fires_only_for_watched():
    led, ex = _led(), SpyExec()
    s = Sniper(_chain(), led, ex)
    bus = EventBus()
    s.attach(bus)
    # not watched → silent
    other = NormEvent("CurveCreated", "suipump", CID, "0x" + "99" * 32, TOK, {}, 1, "d2", 2)
    bus.emit(other)
    assert ex.buys == []
    # watched → fires once
    bus.emit(_ev())
    assert len(ex.buys) == 1 and ex.buys[0]["curve_id"] == CID
    st = led._conn.execute("SELECT decision FROM snipe_log").fetchone()
    assert st["decision"] == "fired"


def test_sniper_cooldown_1_per_24h():
    led, ex = _led(), SpyExec()
    s = Sniper(_chain(), led, ex)
    bus = EventBus()
    s.attach(bus)
    bus.emit(_ev())
    bus.emit(_ev())           # immediate second launch from same dev → cooldown
    assert len(ex.buys) == 1


def test_sniper_suggest_mode_asks_first():
    led, ex = _led(mode="ask"), SpyExec()
    s = Sniper(_chain(), led, ex)
    bus = EventBus()
    s.attach(bus)
    bus.emit(_ev())
    assert ex.buys == []      # 0 prior launches ⇒ ask
    log = led._conn.execute("SELECT decision FROM snipe_log").fetchone()
    assert log["decision"] == "ask"


def test_sniper_spread_burst_when_enabled():
    led, ex = _led(), SpyExec()
    led.add_bundle_wallet(1, "w1", "0x" + "b1" * 32, b"k", "h1")
    led.add_bundle_wallet(1, "w2", "0x" + "b2" * 32, b"k", "h2")
    led.watch_deployer(1, DEV, size_sui=0.6, mode="auto", spread_burst=True)
    s = Sniper(_chain(), led, ex)
    bus = EventBus(); s.attach(bus)
    bus.emit(_ev())
    assert len(ex.bursts) == 1 and len(ex.bursts[0]["legs"]) == 3  # main + 2 legs


def test_sniper_rejects_rugged_dev_and_autounwatches():
    led, ex = _led(), SpyExec()
    s = Sniper(_chain(), led, ex)
    # dev holds >20% ⇒ dev_screen blocks
    ch = MockChain({CID: curve_json(CID, DEV, 10 ** 9, 10 ** 13, int(9000e9))},
                   coins={DEV: [{"objectId": "0x11", "version": 1, "digest": "22" * 32,
                                 "balance_mist": 10 ** 10}]})
    s.ch = ch
    bus = EventBus(); s.attach(bus)
    # dev token balance via chain.balances: our MockChain.balance sums coins (SUI) —
    # force token ownership by monkeypatching dev_screen inputs:
    import degen.sniper as sn
    orig = sn.dev_screen
    sn.dev_screen = lambda c, st: {"ok": False, "flags": [], "blocks": ["dev holds 55% of supply"],
                                   "dev_hold_pct": 55.0}
    try:
        bus.emit(_ev())
        assert ex.buys == []
        row = led._conn.execute("SELECT fails FROM snipe_watch").fetchone()
        assert row["fails"] == 1
        bus.emit(_ev())  # 2nd fail → unwatch
    finally:
        sn.dev_screen = orig
    row = led._conn.execute("SELECT enabled FROM snipe_watch").fetchone()
    assert row["enabled"] == 0


# ---------------------------------------------------------------- bundle
def test_bundle_generation_and_isolation(tmp_path):
    from exec_vault import ExecVault
    from degen.bundle import BundleManager
    led = DegenLedger(":memory:")
    vault = ExecVault(Fernet.generate_key())
    bm = BundleManager(led, vault)
    made = bm.generate(1, 3)
    assert len(made) == 3 and made[0]["slot"] == 1
    # key isolation: leg1 ≠ leg2, decryptable only by this bot
    k1 = bm.sign_for(1, made[0]["address"])
    k2 = bm.sign_for(1, made[1]["address"])
    assert k1 and k2 and k1 != k2
    assert bm.sign_for(2, made[0]["address"]) is None    # bot 2 doesn't own it
    # funding plan sums to total across main+legs, legs use their own wallets
    plan = bm.funding_plan(1, 3.0)
    assert abs(sum(l["amount_sui"] for l in plan) - 0.75 * 4) < 1e-9
    assert {l["wallet"] for l in plan[1:]} == {m["address"] for m in made}
