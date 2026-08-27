import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("TG_VAULT_MASTER_KEY", Fernet.generate_key().decode())
os.environ.setdefault("TG_MASTER_TOKEN", "111:fake-master")

from key_vault import KeyVault
from store import Registry
from handlers.common import get_bot_username, poll_for_verify_code
from handlers.wizard import validate_name, simple_flow_handlers


@pytest.fixture()
def registry():
    tmp = tempfile.mkdtemp()
    r = Registry(os.path.join(tmp, "reg.db"), KeyVault())
    yield r
    r.close()


def test_validate_name():
    assert validate_name("BitcoinWhale") is None
    assert validate_name("ab") is not None
    assert validate_name("a" * 30) is not None
    assert validate_name("Bad Name!") is not None
    assert validate_name("My Bot 2") is None  # spaces/numbers allowed


def test_first_user_promoted_to_owner(registry):
    registry.upsert_user(111, "alice")
    assert registry.promote_first_user_to_admin(111) is True
    assert registry.is_admin(111) is True
    # second user is NOT promoted
    registry.upsert_user(222, "bob")
    assert registry.promote_first_user_to_admin(222) is False
    assert registry.is_admin(222) is False


def test_ownership_flow_blocks_without_code(monkeypatch, registry):
    """No messages at all -> 'no_updates' (user never pressed Start), never 'verified'."""
    from handlers import common

    monkeypatch.setattr(common, "get_bot_username", lambda token: "user_bot")
    assert common.get_bot_username("x") == "user_bot"
    # network returns no updates -> must detect the missing-Start case
    monkeypatch.setattr(common.requests, "get", lambda *a, **k: SimpleNamespace(
        json=lambda: {"ok": True, "result": []}))
    assert common.poll_for_verify_code("fake-token", "CODE", timeout_s=0.2, interval=0.05) == "no_updates"


def test_ownership_flow_wrong_code_but_chat_exists(monkeypatch):
    """Chat exists (updates arrive) but code never matches -> 'timeout'."""
    from handlers import common

    seen = {"n": 0}

    def fake_updates(url, **kwargs):
        seen["n"] += 1
        return SimpleNamespace(json=lambda: {
            "ok": True,
            "result": [{"update_id": seen["n"], "message": {"text": "hi there"}}],
        })

    monkeypatch.setattr(common.requests, "get", fake_updates)
    assert common.poll_for_verify_code("fake-token", "VERIFY-9999", timeout_s=0.2, interval=0.05) == "timeout"


def test_ownership_flow_passes_when_code_arrives(monkeypatch):
    from handlers import common

    seen = {"called": 0}

    def fake_updates(url, **kwargs):
        seen["called"] += 1
        if seen["called"] >= 2:
            return SimpleNamespace(json=lambda: {
                "ok": True,
                "result": [{"update_id": 1, "message": {"text": "VERIFY-1234"}}],
            })
        return SimpleNamespace(json=lambda: {"ok": True, "result": []})

    monkeypatch.setattr(common.requests, "get", fake_updates)
    assert poll_for_verify_code("fake-token", "VERIFY-1234", timeout_s=5, interval=0.05) == "verified"


def test_ownership_rejects_invalid_token(monkeypatch):
    from handlers import common

    monkeypatch.setattr(common.requests, "get", lambda *a, **k: SimpleNamespace(
        json=lambda: {"ok": False, "error_code": 401}))
    assert poll_for_verify_code("bad-token", "CODE", timeout_s=5, interval=0.05) == "token_invalid"


def test_simple_flow_registers_bot(monkeypatch, registry):
    """token -> verify -> name -> agent + bot registered (platform mocked)."""
    from handlers import common

    # seed the registry user + admin
    registry.upsert_user(7, "carrier")
    registry.promote_first_user_to_admin(7)
    # store an AI key so the runner can start later
    registry.store_key(7, "openai", "sk-abcdef123456", None, "gpt-4o-mini")

    calls = {"registered": []}

    class FakePlatform:
        def register_agent(self, name):
            calls["registered"].append(name)
            return {"name": name, "token": "ptok-123"}

    class FakeUserbot:
        def __init__(self):
            self.started = []

        def start_bot(self, bot_id):
            self.started.append(bot_id)

    class FakePool:
        def __init__(self):
            self.started = []

        def start(self, bot_id):
            self.started.append(bot_id)

    from telegram import Update, Message, User, Chat

    sent = []

    async def reply(text, reply_markup=None):
        sent.append(text)

    async def reply_kb(text, reply_markup=None):
        sent.append(text)

    msg = SimpleNamespace(text="MyBot", reply_text=reply_kb,
                          effective_user=SimpleNamespace(id=7, username="carrier"))
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=7, username="carrier"), message=msg)

    flow = simple_flow_handlers(registry, KeyVault(), FakePlatform(), FakeUserbot(), FakePool())
    # drive only the name handler directly: simulate pending token set
    ctx = SimpleNamespace(bot_data={"pending": {"token": "111:tok1", "username": "user_bot",
                                               "verify_code": "VERIFY-9999"}})

    import asyncio

    state = asyncio.get_event_loop().run_until_complete(flow.states[1][0].callback(upd, ctx))
    assert state == ConversationHandler.END
    assert calls["registered"] == ["MyBot"]
    bots = registry.bots_for(7)
    assert len(bots) == 1
    assert bots[0]["bot_name"] == "MyBot"
    assert bots[0]["agent_name"] == "MyBot"


from telegram.ext import ConversationHandler  # noqa: E402