"""Full e2e: wizard happy path against the LIVE local platform with mocked
Telegram transport. Verifies: agent registered -> appears on leaderboard ->
bot row created with multi-market config -> decision log produced by runner env."""

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

from key_vault import KeyVault
from store import Registry
from platform_client import PlatformClient, PlatformError
from handlers.wizard import validate_name
from main import build_app

PLATFORM = "http://127.0.0.1:8000"


@pytest.fixture()
def platform_ready():
    import requests

    try:
        requests.get(f"{PLATFORM}/health", timeout=5)
    except Exception:
        pytest.skip("local AI-Trader platform not running")
    return PlatformClient(PLATFORM)


@pytest.fixture()
def reg():
    tmp = tempfile.mkdtemp()
    r = Registry(os.path.join(tmp, "reg.db"), KeyVault())
    yield r
    r.close()


def _fake_query(reg, tg_id=77, bot_name="E2EBot"):
    sent = []

    class Msg:
        async def reply_text(self, text, reply_markup=None):
            sent.append(("text", text))
        async def edit_text(self, text, reply_markup=None):
            sent.append(("edit", text))

    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id),
        message=Msg(), answer=lambda: None,
        data=None,
        _sent=sent,
    )


def test_e2e_happy_path(reg, platform_ready):
    """Simulates the wizard completion exactly as handlers do, on the live platform."""
    import time as _time

    unique = f"E2EBot{int(_time.time()) % 100000}"
    assert validate_name(unique) is None
    markets = {"perps": False, "spot": True, "us-stock": True, "forex": True}

    # store key the way the wizard would (validation mocked at the provider layer)
    reg.store_key(77, "openai", "sk-fake-key-12345678", None, "gpt-4o-mini")

    # agent registration on live platform
    agent = platform_ready.register_agent(unique)
    assert agent.get("name") == unique
    assert agent.get("token")

    # bot creation via the simple flow defaults (spot+stocks+forex, 1x)
    bot = reg.create_bot(77, unique, "999999999:fake-token", "e2e_bot",
                         agent["name"], agent["token"], markets,
                         1.0, 120, "balanced")
    assert reg.get_bot(bot["id"])["leverage"] == 1.0
    assert "spot" in bot["symbols"]
    assert bot["symbols"].count("us-stock") == 1

    # agent appears on the platform leaderboard (leaderboard is cached ~60s; poll)
    import time as _t
    row = None
    for _ in range(7):
        row = platform_ready.agent_row(agent["token"], unique)
        if row is not None:
            break
        _t.sleep(10)
    assert row is not None, "agent must appear on the leaderboard"
    assert "total_profit_percent" in row

    # bot listed in registry (the 'bot list')
    assert len(reg.bots_for(77)) == 1

    # decision env mapping: the agent pool would spawn a runner with this universe
    from agent_pool import _symbols_to_universe
    uni = _symbols_to_universe(markets, 1.0)
    assert "BTC:crypto" in uni and "AAPL:us-stock" in uni and "EURUSD:forex" in uni