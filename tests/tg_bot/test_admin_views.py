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