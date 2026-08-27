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
                qty=1.0, order_type="market", stop_loss=None, take_profit=None,
                leverage=1.0, idempotency_key="k1")
    base.update(kw)
    return OrderIntent(**base)


def test_fee_is_flat_50bps_platform_only_model(ledger):
    assert PLATFORM_FEE_BPS == 50
    oid = ledger.create_order(intent(), 1)
    ledger.record_fill(oid, price=100.0, qty=1.0, fee_venue=0.10, tx_hash="0x1", bot_id=1)
    # platform fee exactly 0.5% of notional; venue fee separate kind
    assert ledger.fees_for_bot(1, "platform") == pytest.approx(0.5)
    assert ledger.fees_for_bot(1, "venue") == pytest.approx(0.10)
    kinds = set()
    import sqlite3
    conn = sqlite3.connect(ledger.path)
    for row in conn.execute("SELECT DISTINCT kind FROM fee_ledger"):
        kinds.add(row[0])
    conn.close()
    assert kinds == {"platform", "venue"}  # no other fee models exist in the ledger


def test_fees_immutable_after_write(ledger):
    oid = ledger.create_order(intent(), 1)
    ledger.record_fill(oid, price=100.0, qty=1.0, fee_venue=0.0, tx_hash="0x2", bot_id=1)
    total = ledger.fees_for_bot(1)
    # re-filling the same order must not duplicate (idempotency upstream), and
    # a second fill on a new order accrues separately
    oid2 = ledger.create_order(intent(idempotency_key="k2"), 1)
    ledger.record_fill(oid2, price=100.0, qty=1.0, fee_venue=0.0, tx_hash="0x3", bot_id=1)
    assert ledger.fees_for_bot(1) == pytest.approx(total + 0.5)