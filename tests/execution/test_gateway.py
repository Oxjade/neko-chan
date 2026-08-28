import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest
from cryptography.fernet import Fernet

os.environ["TG_EXEC_MASTER_KEY"] = Fernet.generate_key().decode()
os.environ["REAL_TRADING_ENABLED"] = "1"

from gateway import ExecGateway


def _set_chain_env(enable: bool = True):
    vals = {
        "EXEC_HL_AGENT_KEY": "0x" + "ab" * 32,
        "EXEC_HL_MASTER_ADDRESS": "0x" + "cd" * 20,
        "EXEC_HL_TESTNET": "1",
        "EXEC_SOL_KEYPAIR_HEX": "ab" * 32,
        "EXEC_SUI_KEYPAIR_HEX": bytes(range(32)).hex(),
        "EXEC_SUI_DEEPBOOK_PACKAGE": "0x" + "12" * 32,
        "EXEC_SUI_POOL_ID": "0x" + "ab" * 32,
        "EXEC_SUI_BALANCE_MANAGER": "0x" + "cd" * 32,
    }
    for k, v in vals.items():
        if enable:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def _clear_chain_env():
    _set_chain_env(enable=False)


def test_gateway_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "0")
    _clear_chain_env()
    g = ExecGateway.build(ledger_path=str(tmp_path / "a.db"))
    assert g.ready is False
    assert g.route(1, None, 1.0) == {"ok": False, "error": "real execution not configured"}
    assert g.route_and_sync(1, None, 1.0) == {"ok": False, "error": "real execution not configured"}
    assert g.engage_killswitch(1, "test") == {"ok": False, "error": "killswitch not configured"}
    assert g.sync(1, "solana") == {"ok": False, "error": "sync engine not configured"}
    assert g.scan_deposits(1, "solana") == []


def test_gateway_enabled_but_no_keys_is_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "1")
    _clear_chain_env()
    g = ExecGateway.build(ledger_path=str(tmp_path / "b.db"))
    assert g.ready is False
    assert g.adapters == {}


def test_gateway_wires_full_stack(monkeypatch, tmp_path):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "1")
    _set_chain_env()
    g = ExecGateway.build(ledger_path=str(tmp_path / "c.db"))
    assert g.ready is True
    assert set(g.adapters.keys()) == {"hyperliquid", "solana", "sui"}
    assert g.killswitch is not None
    assert set(g.killswitch._hooks.keys()) == {"hyperliquid", "solana", "sui"}
    assert g.sync_engine is not None
    assert set(g.sync_engine.fetchers.keys()) == {"hyperliquid", "solana", "sui"}
    assert g.deposit_watch is not None
    assert set(g.deposit_watch._checkers.keys()) == {"solana", "sui"}


def test_gateway_provisions_wallets(monkeypatch, tmp_path):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "1")
    _set_chain_env()
    g = ExecGateway.build(ledger_path=str(tmp_path / "d.db"))
    wallets = g.provision_all_wallets(7)
    assert set(wallets.keys()) == {"hyperliquid", "solana", "sui"}
    for chain, wid in wallets.items():
        w = g.ledger.wallet(wid)
        assert w is not None
        assert w["bot_id"] == 7 and w["chain"] == chain
    # idempotent re-provision returns the same wallet
    again = g.provision_all_wallets(7)
    assert again == wallets


def test_gateway_sync_and_deposit_scan_never_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("REAL_TRADING_ENABLED", "1")
    _set_chain_env()
    g = ExecGateway.build(ledger_path=str(tmp_path / "e.db"))
    g.provision_all_wallets(9)
    # Stub every adapter's network reads so the test is hermetic.
    import requests as _requests

    for chain in g.adapters:
        adapter = g.adapters[chain]
        if hasattr(adapter, "get_account_state"):
            monkeypatch.setattr(adapter, "get_account_state",
                                lambda: {"ok": True, "balances": {"USDC": 100.0}, "positions": []})
        if hasattr(adapter, "get_balance"):
            monkeypatch.setattr(adapter, "get_balance", lambda *a: 50.0)
        if hasattr(adapter, "get_positions"):
            monkeypatch.setattr(adapter, "get_positions", lambda: [])
    monkeypatch.setattr(_requests, "post", lambda *a, **k: None)

    for chain in g.adapters:
        res = g.sync(9, chain)
        assert res.get("ok") is True, f"sync {chain}: {res}"
    for chain in g.deposit_watch._checkers:
        assert g.scan_deposits(9, chain) == []
    g.release_killswitch(9)  # no-op safe