import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest

from ledger import ExecLedger
from risk_guard import RiskGuard
from killswitch import KillSwitch


@pytest.fixture()
def env():
    tmp = tempfile.mkdtemp()
    ledger = ExecLedger(os.path.join(tmp, "exec.db"))
    guard = RiskGuard()
    ks = KillSwitch(guard, ledger)
    yield ledger, guard, ks
    ledger.close()


def test_killswitch_flats_every_chain(env):
    ledger, guard, ks = env
    calls = {}

    def hl_hook(bot_id):
        calls["hl"] = bot_id
        return {"ok": True}

    def sol_hook(bot_id):
        calls["sol"] = bot_id
        return {"ok": True}

    def sui_hook(bot_id):
        calls["sui"] = bot_id
        return {"ok": True}

    ks.register_hook("hyperliquid", hl_hook)
    ks.register_hook("solana", sol_hook)
    ks.register_hook("sui", sui_hook)

    result = ks.engage(1, "test kill")
    assert result["fully_flattened"] is True
    assert calls == {"hl": 1, "sol": 1, "sui": 1}
    # risk guard now blocks all orders
    assert guard.is_killswitched(1)


def test_killswitch_stays_engaged_on_partial_failure(env):
    ledger, guard, ks = env
    ks.register_hook("hyperliquid", lambda b: {"ok": True})
    ks.register_hook("solana", lambda b: (_ for _ in ()).throw(RuntimeError("rpc down")))

    result = ks.engage(1, "test")
    assert result["fully_flattened"] is False
    assert result["results"]["solana"]["ok"] is False
    # still engaged - never silently partial
    assert guard.is_killswitched(1)

    ks.release(1)
    assert not guard.is_killswitched(1)


def test_killswitch_without_hooks_reports_failure(env):
    ledger, guard, ks = env
    result = ks.engage(1, "no adapters registered")
    assert result["fully_flattened"] is False