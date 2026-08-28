import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("TG_VAULT_MASTER_KEY", Fernet.generate_key().decode())
os.environ.setdefault("TG_MASTER_TOKEN", "111:fake-master")
os.environ.setdefault("TG_ADMIN_IDS", "999")

from key_vault import KeyVault
from store import Registry
from platform_client import PlatformClient
from handlers.master import register_master_handlers
from main import build_app, menu_buttons


@pytest.fixture()
def app():
    tmp = tempfile.mkdtemp()
    reg = Registry(os.path.join(tmp, "reg.db"), KeyVault())
    platform = PlatformClient()
    from userbot import UserBotController
    from agent_pool import AgentPool

    ub = UserBotController(reg, platform)
    pool = AgentPool(reg)
    app = build_app(reg, platform, KeyVault(), ub, pool)
    reg.close()
    return app


def test_menu_buttons_present():
    flat = [btn for row in menu_buttons() for btn in row]
    assert any("Add My Bot" in b for b in flat)
    assert any("Leaderboard" in b for b in flat)


def test_build_app_registers_handlers(app):
    names = []
    for group in app.handlers.values():
        for h in group:
            cb = getattr(h, "callback", None)
            names.append(getattr(cb, "__name__", "") or str(cb)[:40])
    joined = " ".join(names)
    assert "on_start" in joined or "start" in joined


def test_admin_view_unauthorized(monkeypatch):
    from handlers.master import register_master_handlers
    tmp = tempfile.mkdtemp()
    reg = Registry(os.path.join(tmp, "reg.db"), KeyVault())
    calls = []

    async def reply(text):
        calls.append(text)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),  # not in TG_ADMIN_IDS
        message=SimpleNamespace(reply_text=reply),
    )
    # drive the handler directly through the app
    app = build_app(reg, PlatformClient(), KeyVault(), None, None)
    for group in app.handlers.values():
        for h in group:
            cb = getattr(h, "callback", None)
            if cb is not None and getattr(cb, "__name__", "") == "admin_list":
                import asyncio
                asyncio.get_event_loop().run_until_complete(h.callback(update, SimpleNamespace()))
    assert any("Unauthorized" in c for c in calls)
    reg.close()


class _FakeGateway:
    def __init__(self, ready=True):
        self.ready = ready
        self.adapters = {"hyperliquid": object()}
        self.engaged = []

    def engage_killswitch(self, bot_id, reason):
        self.engaged.append(bot_id)
        return {"ok": True, "fully_flattened": True,
                "results": {"hyperliquid": {"ok": True}}}


class _FakeUserbot:
    def __init__(self, gateway):
        self.gateway = gateway


def _build_with_gateway(gateway):
    tmp = tempfile.mkdtemp()
    reg = Registry(os.path.join(tmp, "reg.db"), KeyVault())
    app = build_app(reg, PlatformClient(), KeyVault(),
                    _FakeUserbot(gateway), None)
    return reg, app


def _find_handler(app, name):
    import asyncio
    for group in app.handlers.values():
        for h in group:
            cb = getattr(h, "callback", None)
            if cb is not None and getattr(cb, "__name__", "") == name:
                return h
    return None


def test_admin_list_shows_kill_all_button_when_gateway_ready():
    reg, app = _build_with_gateway(_FakeGateway(ready=True))
    sent = []
    async def reply(text, reply_markup=None):
        sent.append((text, reply_markup))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),  # TG_ADMIN_IDS
        message=SimpleNamespace(reply_text=reply),
    )
    import asyncio
    asyncio.get_event_loop().run_until_complete(_find_handler(app, "admin_list").callback(update, SimpleNamespace()))
    assert sent and "Kill ALL" in str(sent[0][1])
    reg.close()


def test_admin_killall_yes_engages_gateway_for_all_bots():
    gw = _FakeGateway(ready=True)
    reg, app = _build_with_gateway(gw)
    # register a couple of bots (minimal required fields)
    for i in range(2):
        reg.create_bot(999, f"Bot{i}", f"tok{i}", f"b{i}", f"A{i}", f"t{i}",
                       {"perps": 1}, 1.0, 120, "balanced")
    sent = []
    async def edit(text, reply_markup=None):
        sent.append(text)
    async def answer():
        pass
    update = SimpleNamespace(
        callback_query=SimpleNamespace(from_user=SimpleNamespace(id=999),
                                       data="admin:killall_yes", answer=answer,
                                       message=SimpleNamespace(edit_text=edit)),
    )
    import asyncio
    asyncio.get_event_loop().run_until_complete(_find_handler(app, "admin_killall_yes").callback(update, SimpleNamespace()))
    assert len(gw.engaged) == 2  # both bots flattened
    assert sent and "ENGAGED" in sent[0]
    assert "2/2" in sent[0]
    reg.close()


def test_admin_killall_unauthorized_ignored():
    gw = _FakeGateway(ready=True)
    reg, app = _build_with_gateway(gw)
    reg.create_bot(999, "Bot0", "tok0", "b0", "A0", "t0",
                   {"perps": 1}, 1.0, 120, "balanced")
    sent = []
    async def edit(text, reply_markup=None):
        sent.append(text)
    async def answer():
        pass
    update = SimpleNamespace(
        callback_query=SimpleNamespace(from_user=SimpleNamespace(id=1),  # not admin
                                       data="admin:killall_yes", answer=answer,
                                       message=SimpleNamespace(edit_text=edit)),
    )
    import asyncio
    asyncio.get_event_loop().run_until_complete(_find_handler(app, "admin_killall_yes").callback(update, SimpleNamespace()))
    assert gw.engaged == []  # nothing engaged for non-admin
    assert sent and "Unauthorized" in sent[0]
    reg.close()