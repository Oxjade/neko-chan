"""Tests for the user bot's real-trading screens (wallet / kill-switch / exec risk)."""

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("TG_VAULT_MASTER_KEY", Fernet.generate_key().decode())

from key_vault import KeyVault
from store import Registry
from userbot import UserBotController
from messages import USERBOT


class FakeGateway:
    def __init__(self, ready=True):
        self.ready = ready
        self.adapters = {"hyperliquid": object(), "solana": object(), "sui": object()}
        self.engaged = []
        self.released = []
        self.provisioned = []

        def wallet_by_bot_chain(bot_id, chain):
            return {"id": 1, "bot_id": bot_id, "chain": chain,
                    "address": "0x" + "ab" * 20 if chain == "hyperliquid" else "A" * 44,
                    "status": "active"}

        def load_chain_state(wallet_id):
            return {"balances": {"USDC": 123.45, "native": 0.5},
                    "positions": [{"symbol": "BTC", "side": "long", "qty": 0.1,
                                   "entry_px": 70000.0, "notional_usd": 7000.0}]}

        self.ledger = types.SimpleNamespace(
            wallet_by_bot_chain=wallet_by_bot_chain,
            load_chain_state=load_chain_state,
        )

    def provision_all_wallets(self, bot_id):
        self.provisioned.append(bot_id)
        return {c: 1 for c in self.adapters}

    def provision_wallet(self, bot_id, chain):
        return 1

    def sync(self, bot_id, chain):
        return {"ok": True}

    def engage_killswitch(self, bot_id, reason):
        self.engaged.append((bot_id, reason))
        return {"ok": True, "fully_flattened": True,
                "results": {c: {"ok": True} for c in self.adapters}}

    def release_killswitch(self, bot_id):
        self.released.append(bot_id)


@pytest.fixture()
def controller():
    tmp = tempfile.mkdtemp()
    reg = Registry(os.path.join(tmp, "reg.db"), KeyVault())
    yield UserBotController(reg, platform=object(), vault=KeyVault(),
                            agent_pool=None, gateway=FakeGateway(ready=True))
    reg.close()


def test_exec_ready_reflects_gateway():
    _vault = KeyVault()
    c = UserBotController(Registry(":memory:", _vault), platform=object(), gateway=FakeGateway(ready=True))
    assert c._exec_ready() is True
    c2 = UserBotController(Registry(":memory:", _vault), platform=object(), gateway=FakeGateway(ready=False))
    assert c2._exec_ready() is False
    c3 = UserBotController(Registry(":memory:", _vault), platform=object(), gateway=None)
    assert c3._exec_ready() is False


def test_render_wallet_when_exec_disabled():
    c = UserBotController(Registry(":memory:", KeyVault()), platform=object(),
                          gateway=FakeGateway(ready=False))
    text = c._render_wallet(1, "Whale", 0)
    assert "Real trading is not enabled" in text


def test_render_wallet_shows_chain_state(controller):
    text = controller._render_wallet(1, "Whale", 0)
    assert "Hyperliquid" in text and "Solana" in text and "Sui" in text
    assert "$123.45" in text
    assert "1 open" in text


def test_exec_risk_lines_are_human_readable(controller):
    lines = controller._exec_risk_lines()
    joined = "\n".join(lines)
    assert "Max notional" in joined
    assert "Stop-loss" in joined and "required" in joined
    assert "Daily loss halt" in joined
    assert all(l.startswith("•") for l in lines)


def test_killswitch_engages_and_releases(controller):
    gw = controller.gateway
    res = gw.engage_killswitch(7, "test")
    assert res["fully_flattened"] is True
    assert (7, "test") in gw.engaged
    gw.release_killswitch(7)
    assert 7 in gw.released


# ---------------- disclaimer gate ----------------

def test_disclaimer_copy_is_clean_and_direct():
    from messages import WIZARD
    d = WIZARD["disclaimer"]
    # clean, proper language - no experiment/backtest/strategy internals
    assert "NOT financial advice" in d
    assert "does NOT guarantee profit" in d
    assert "trusting your funds" in d
    assert "afford to lose" in d
    for banned in ("experiment", "backtest", "momentum", "+664", "+1316", "quantitative finance"):
        assert banned not in d, f"disclaimer must not mention {banned}"


def test_disclaimer_accept_persists_in_registry():
    tmp = tempfile.mkdtemp()
    reg = Registry(os.path.join(tmp, "r.db"), KeyVault())
    reg.upsert_user(55, "tester")
    assert reg.get_user(55)["accepted_disclaimer"] == 0
    reg.accept_disclaimer(55)
    assert reg.get_user(55)["accepted_disclaimer"] == 1
    reg.close()


# ---------------- scheduled deletion (unconfigured-bot cleanup) ----------------

def _make_bot(reg, tg_id=77, name="BotX", token="999:tok"):
    return reg.create_bot(tg_id, name, token, "botx", "A1", "plat-tok",
                          {"spot": 1}, 1.0, 120, "balanced")


def test_schedule_and_cancel_deletion():
    from datetime import datetime, timedelta, timezone
    tmp = tempfile.mkdtemp()
    reg = Registry(os.path.join(tmp, "r.db"), KeyVault())
    bot = _make_bot(reg)
    future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    reg.schedule_bot_deletion(bot["id"], future)
    assert reg.get_bot(bot["id"])["scheduled_deletion_at"] == future
    assert reg.due_bot_deletions() == []  # not due yet
    reg.cancel_bot_deletion(bot["id"])
    assert reg.get_bot(bot["id"])["scheduled_deletion_at"] is None
    reg.close()


def test_due_deletions_returns_expired_bots():
    from datetime import datetime, timedelta, timezone
    tmp = tempfile.mkdtemp()
    reg = Registry(os.path.join(tmp, "r.db"), KeyVault())
    bot = _make_bot(reg)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    reg.schedule_bot_deletion(bot["id"], past)
    due = reg.due_bot_deletions()
    assert len(due) == 1 and due[0]["id"] == bot["id"]
    reg.close()


def test_cleanup_janitor_deletes_and_notifies():
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch
    from main import start_bot_cleanup
    tmp = tempfile.mkdtemp()
    reg = Registry(os.path.join(tmp, "r.db"), KeyVault())
    bot = _make_bot(reg)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    reg.schedule_bot_deletion(bot["id"], past)

    stopped, deleted, notified = [], [], []

    class _FakeUB:
        def stop_bot(self, bid):
            stopped.append(bid)

    class _FakePool:
        def stop(self, bid):
            stopped.append(bid)

    with patch("main.time.sleep", lambda s: None):  # no real wait
        t = start_bot_cleanup(reg, _FakeUB(), _FakePool(), deadline_hours=3, poll_seconds=1)
        t.join(timeout=2)

    assert bot["id"] in stopped
    assert reg.get_bot(bot["id"]) is None  # deleted
    reg.close()