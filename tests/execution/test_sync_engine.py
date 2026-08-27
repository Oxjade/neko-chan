import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest

from ledger import ExecLedger
from sync_engine import SyncEngine


@pytest.fixture()
def env():
    tmp = tempfile.mkdtemp()
    ledger = ExecLedger(os.path.join(tmp, "exec.db"))
    wallet_id = ledger.upsert_wallet(1, "hyperliquid", "0xaa", "pk", b"e", "h")
    yield ledger, wallet_id
    ledger.close()


def test_sync_pulls_and_caches(env):
    ledger, wallet_id = env

    def fetcher(wallet):
        return {"balances": {"USDC": 500.0}, "positions": [{"symbol": "BTC"}], "orders": []}

    se = SyncEngine(ledger)
    se.register_fetcher("hyperliquid", fetcher)
    result = se.sync(1, "hyperliquid")
    assert result["ok"] is True
    state = ledger.load_chain_state(wallet_id)
    assert state["balances"]["USDC"] == 500.0
    assert result["drift"] is None


def test_sync_logs_drift_instead_of_trusting_cache(env):
    ledger, wallet_id = env
    se = SyncEngine(ledger)
    se.register_fetcher("hyperliquid", lambda w: {"balances": {"USDC": 500.0},
                                                  "positions": [], "orders": []})
    se.sync(1, "hyperliquid")
    # on-chain now disagrees with the cached snapshot
    se.register_fetcher("hyperliquid", lambda w: {"balances": {"USDC": 620.0},
                                                  "positions": [], "orders": []})
    result = se.sync(1, "hyperliquid")
    assert result["drift"] is not None
    assert any("balance USDC" in d for d in result["drift"])
    assert len(se.drift_events) == 1


def test_sync_without_fetcher_graceful(env):
    ledger, wallet_id = env
    result = SyncEngine(ledger).sync(1, "hyperliquid")
    assert result["ok"] is False and "no fetcher" in result["error"]


def test_sync_ignores_positions_order(env):
    ledger, wallet_id = env
    se = SyncEngine(ledger)
    se.register_fetcher("hyperliquid", lambda w: {"balances": {"USDC": 100.0},
                                                  "positions": [{"symbol": "BTC"}, {"symbol": "ETH"}],
                                                  "orders": []})
    se.sync(1, "hyperliquid")
    se.register_fetcher("hyperliquid", lambda w: {"balances": {"USDC": 100.0},
                                                  "positions": [{"symbol": "ETH"}, {"symbol": "BTC"}],
                                                  "orders": []})
    result = se.sync(1, "hyperliquid")
    assert result["drift"] is None