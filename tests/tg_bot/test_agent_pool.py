import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("TG_VAULT_MASTER_KEY", Fernet.generate_key().decode())

from key_vault import KeyVault
from store import Registry
from agent_pool import AgentPool, _symbols_to_universe


@pytest.fixture()
def env():
    tmp = tempfile.mkdtemp()
    reg = Registry(os.path.join(tmp, "reg.db"), KeyVault())
    reg.upsert_user(1)
    reg.store_key(1, "openai", "sk-user-key-1234", None, "gpt-4o-mini")
    bot = reg.create_bot(1, "Whale", "111:tok1", "whale_bot", "WhaleAgent", "ptok",
                         {"perps": 1, "spot": 0, "us-stock": 1, "forex": 1},
                         5.0, 120, "balanced")
    yield reg, bot
    reg.close()


def test_universe_mapping():
    u = _symbols_to_universe({"perps": 1, "spot": 0, "us-stock": 1, "forex": 1}, 5.0)
    assert "BTC:crypto" in u and "AAPL:us-stock" in u and "EURUSD:forex" in u
    assert "NVDA:us-stock" in u and "USDJPY:forex" in u


def test_start_spawns_with_user_credentials(env):
    reg, bot = env
    pool = AgentPool(reg)
    captured = {}

    class FakeProc:
        def __init__(self, cmd, env, **kw):
            captured["env"] = env
            captured["cmd"] = cmd
            self.pid = 4242

        def poll(self):
            return None  # running

    with patch("agent_pool.subprocess.Popen", FakeProc), \
         patch("agent_pool.sys_executable", return_value="/usr/bin/python3"):
        ok = pool.start(bot["id"])
    assert ok is True
    assert captured["env"]["LIVE_AGENT_API_KEY"] == "sk-user-key-1234"
    assert captured["env"]["LIVE_AGENT_PROVIDER"] == "openai"
    assert captured["env"]["LIVE_AGENT_LEVERAGE"] == "5.0"
    assert captured["env"]["LIVE_AGENT_SYMBOLS"] == _symbols_to_universe(
        {"perps": 1, "spot": 0, "us-stock": 1, "forex": 1}, 5.0)
    assert reg.get_bot(bot["id"])["is_running"] == 1
    pool.stop(bot["id"])
    assert reg.get_bot(bot["id"])["is_running"] == 0


def test_crash_restart_limited(env):
    reg, bot = env
    pool = AgentPool(reg)

    class DeadProc:
        def __init__(self, cmd, env, **kw):
            self.pid = 4243

        def poll(self):
            return 1  # crashed

    with patch("agent_pool.subprocess.Popen", DeadProc), \
         patch("agent_pool.sys_executable", return_value="/usr/bin/python3"):
        pool.start(bot["id"])
        pool.healthcheck(max_restarts_per_hour=3)
        # crashed proc stays dead; after restart it's still dead -> flagged on next checks
        pool.healthcheck(max_restarts_per_hour=3)
        pool.healthcheck(max_restarts_per_hour=3)
        pool.healthcheck(max_restarts_per_hour=3)
    b = reg.get_bot(bot["id"])
    assert b["is_running"] == 0
    assert "crashed" in (b["last_error"] or "")