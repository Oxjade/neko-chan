import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("TG_EXEC_MASTER_KEY", Fernet.generate_key().decode())

from ledger import ExecLedger
from risk_guard import RiskGuard
from killswitch import KillSwitch
from hooks import register_chain_hooks, build_adapters
from exec_vault import ExecVault


@pytest.fixture()
def env():
    tmp = tempfile.mkdtemp()
    ledger = ExecLedger(os.path.join(tmp, "exec.db"))
    guard = RiskGuard()
    ks = KillSwitch(guard, ledger)
    yield ledger, guard, ks
    ledger.close()


def test_register_hooks_from_adapters(env):
    ledger, guard, ks = env
    calls = {}

    def fake_flat(bot_id):
        calls[bot_id] = True
        return {"ok": True}

    adapters = {"hyperliquid": SimpleNamespace(flat_and_cancel=fake_flat),
                "solana": SimpleNamespace(flat_and_cancel=fake_flat)}
    register_chain_hooks(ks, adapters)
    result = ks.engage(7, "integration test")
    assert result["fully_flattened"] is True
    assert calls.get(7) is True


def test_build_adapters_only_configured_chains():
    vault = ExecVault()
    enc = vault.encrypt("0x" + "ab" * 32)
    ledger = ExecLedger(os.path.join(tempfile.mkdtemp(), "e.db"))
    try:
        adapters = build_adapters(ledger, vault, {
            "hyperliquid": {"key_enc": enc, "master_address": "0x" + "cd" * 20, "testnet": True},
        })
        assert list(adapters.keys()) == ["hyperliquid"]
        assert adapters["hyperliquid"].testnet is True
    finally:
        ledger.close()


def test_build_adapters_skips_unconfigured():
    vault = ExecVault()
    ledger = ExecLedger(os.path.join(tempfile.mkdtemp(), "e.db"))
    try:
        assert build_adapters(ledger, vault, {}) == {}
    finally:
        ledger.close()