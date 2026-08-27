import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest

from ledger import ExecLedger, PLATFORM_FEE_BPS
from order_model import OrderIntent


@pytest.fixture()
def ledger():
    tmp = tempfile.mkdtemp()
    L = ExecLedger(os.path.join(tmp, "exec.db"))
    yield L
    L.close()


def intent(**kw):
    base = dict(chain="hyperliquid", venue="hl-perp", symbol="BTC", side="buy",
                qty=0.01, order_type="market", stop_loss=75000, take_profit=84000,
                leverage=5.0, idempotency_key="k1")
    base.update(kw)
    return OrderIntent(**base)


def test_wallet_upsert_is_idempotent_per_bot_chain(ledger):
    w1 = ledger.upsert_wallet(1, "solana", "addr1", "pk1", b"enc", "h1")
    w2 = ledger.upsert_wallet(1, "solana", "addr2", "pk2", b"enc2", "h2")
    assert w1 == w2  # same row updated, not duplicated
    assert ledger.wallet(w1)["address"] == "addr2"


def test_order_idempotency(ledger):
    oid = ledger.create_order(intent(), 1)
    assert ledger.order_exists("k1")
    ledger.set_order_status(oid, "submitted", "venue-123")
    assert ledger.order_exists("k1")  # second submission blocked upstream


def test_fill_fees_flat_50bps(ledger):
    oid = ledger.create_order(intent(qty=1.0), 1)
    ledger.record_fill(oid, price=100.0, qty=1.0, fee_venue=0.05, tx_hash="0xtx", bot_id=1)
    platform = ledger.fees_for_bot(1, "platform")
    venue = ledger.fees_for_bot(1, "venue")
    assert platform == pytest.approx(0.5)  # 1.0 * 100 * 50bps
    assert venue == pytest.approx(0.05)
    assert PLATFORM_FEE_BPS == 50


def test_deposit_flow(ledger):
    w = ledger.upsert_wallet(1, "solana", "addr", "pk", b"e", "h")
    d = ledger.record_deposit(w, "USDC", 100.0, "0xdep")
    ledger.confirm_deposit(d)
    row = ledger.wallet(w)
    assert row["status"] == "created"  # status set separately
    assert ledger.fees_for_bot(1) == 0.0


def test_positions_and_chain_state(ledger):
    w = ledger.upsert_wallet(1, "hyperliquid", "0xaa", "pk", b"e", "h")
    ledger.upsert_position(1, "hyperliquid", "BTC", "long", 0.01, 78000.0, 5.0, 70000.0, 75000.0, 84000.0)
    ledger.save_chain_state(w, {"USDC": 500.0}, [{"symbol": "BTC"}], [])
    state = ledger.load_chain_state(w)
    assert state["balances"]["USDC"] == 500.0
    ledger.delete_position(1, "hyperliquid", "BTC")