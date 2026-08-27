import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest

from ledger import ExecLedger
from deposit_watch import DepositWatch


@pytest.fixture()
def env():
    tmp = tempfile.mkdtemp()
    ledger = ExecLedger(os.path.join(tmp, "exec.db"))
    wallet_id = ledger.upsert_wallet(1, "solana", "addr1", "pk", b"enc", "h")
    yield ledger, wallet_id
    ledger.close()


def test_deposit_detected_and_wallet_active(env):
    ledger, wallet_id = env
    pushed = []

    def checker(wallet):
        return [{"asset": "USDC", "amount": 50.0, "tx_hash": "0xdep"}]

    watch = DepositWatch(ledger, notifier=lambda wid, text: pushed.append((wid, text)))
    watch.register_checker("solana", checker)
    found = watch.scan(1, "solana")
    assert len(found) == 1
    assert found[0]["amount"] == 50.0
    assert ledger.wallet(wallet_id)["status"] == "active"
    assert len(pushed) == 1 and "ACTIVE" in pushed[0][1]


def test_deposit_ignores_invalid_events(env):
    ledger, wallet_id = env
    watch = DepositWatch(ledger)

    def bad(wallet):
        return [{"asset": "USDC", "amount": 0, "tx_hash": ""},
                {"asset": "USDC", "amount": -5, "tx_hash": "x"},
                {"asset": "", "amount": 10, "tx_hash": "y"}]

    watch.register_checker("solana", bad)
    assert watch.scan(1, "solana") == []
    assert ledger.wallet(wallet_id)["status"] != "active"


def test_deposit_checker_error_is_graceful(env):
    ledger, wallet_id = env

    def boom(wallet):
        raise RuntimeError("rpc down")

    watch = DepositWatch(ledger)
    watch.register_checker("solana", boom)
    assert watch.scan(1, "solana") == []


def test_no_checker_noop(env):
    ledger, wallet_id = env
    assert DepositWatch(ledger).scan(1, "solana") == []