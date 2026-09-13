"""A marketable limit entry must fill immediately (no resting). The live agent
sets ENTRY_OFFSET_BPS=2bps inside the market; regression risk: if the sign ever
reverts to placing a buy BELOW / a short ABOVE the market, entries silently sit
in paper_pending and never open -> the 'indecisive / not using limit orders'
symptom. Lock the correct side in as a test."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

from paper_gateway import PaperGateway
from paper_store import PaperStore


def _gw():
    tmp = tempfile.mkdtemp()
    store = PaperStore(os.path.join(tmp, "p.db"))
    return PaperGateway(store), store


def test_marketable_buy_fills_immediately_no_rest():
    gw, store = _gw()
    ref = 80_000.0
    marketable_buy = ref * 1.0002     # 2bps above -> crosses
    fill = gw.open(7, "BTC", "long", 0.001, ref, leverage=5.0,
                   order_type="limit", limit_price=marketable_buy)
    assert fill.get("ok") and not fill.get("pending"), fill
    assert len(store.positions(7)) == 1
    pending = store._conn.execute(
        "SELECT COUNT(*) FROM paper_pending WHERE bot_id=7").fetchone()[0]
    assert pending == 0


def test_marketable_short_fills_immediately_no_rest():
    gw, store = _gw()
    ref = 80_000.0
    marketable_short = ref * 0.9998   # 2bps below -> crosses
    fill = gw.open(8, "ETH", "short", 0.01, ref, leverage=5.0,
                   order_type="limit", limit_price=marketable_short)
    assert fill.get("ok") and not fill.get("pending"), fill
    assert len(store.positions(8)) == 1


def test_non_marketable_rests_as_pending_positive_control():
    """Positive control for the two tests above: a buy BELOW market is NOT
    marketable and must rest. If this ever 'fills', the other tests above pass
    for the wrong reason (the gateway ignored order_type entirely)."""
    gw, store = _gw()
    ref = 80_000.0
    non_marketable = ref * 0.999      # 10bps below -> sits on the bid side
    fill = gw.open(9, "SOL", "long", 0.1, ref, leverage=3.0,
                   order_type="limit", limit_price=non_marketable)
    assert fill.get("pending") is True
    assert store.positions(9) == []   # nothing opened yet
