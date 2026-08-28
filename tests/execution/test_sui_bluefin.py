"""Sui adapter + Bluefin perp integration: delegation, position sync, killswitch.

Bluefin is the perp CLOB on Sui (bluefin-perp venue). The SUIAdapter owns the
DeepBook spot path and delegates bluefin-perp orders / position reads to an
attached BluefinAdapter. This file covers the wiring end-to-end.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest

from bluefin_adapter import BluefinAdapter, build_bluefin
from ledger import ExecLedger
from order_model import OrderIntent
from sui_adapter import SUIAdapter

KEY_HEX = bytes(range(32)).hex()
PACKAGE = "0x" + "12" * 32
POOL = "0x" + "ab" * 32
BALANCE_MANAGER = "0x" + "cd" * 32


def make_adapter(tmp_path, **kw):
    from pathlib import Path
    ledger = ExecLedger(str(Path(tmp_path) / "ledger.db"))
    bluefin = kw.pop("bluefin", None)
    if bluefin is None:
        bluefin = build_bluefin(ledger, KEY_HEX, testnet=True)
    return SUIAdapter(ledger, KEY_HEX, testnet=True,
                      bluefin=bluefin, **kw)


def bluefin_intent(**kw):
    base = dict(chain="sui", venue="bluefin-perp", symbol="BTC", side="buy",
                qty=0.01, order_type="market", leverage=5.0, idempotency_key="sui-bf-1")
    base.update(kw)
    return OrderIntent(**base)


def test_adapter_exposes_bluefin():
    a = make_adapter(__import__("tempfile").mkdtemp())
    assert a.bluefin is not None
    assert isinstance(a.bluefin, BluefinAdapter)


def test_place_order_delegates_to_bluefin(monkeypatch, tmp_path):
    a = make_adapter(tmp_path)
    calls = {}

    def fake_place(intent, ref_price):
        calls["intent"] = intent
        calls["ref_price"] = ref_price
        return {"ok": True, "venue_order_id": "bf-123", "price": ref_price}

    monkeypatch.setattr(a.bluefin, "place_order", fake_place)
    result = a.place_order(bluefin_intent(), 64000.0)
    assert result["ok"] is True
    assert calls["intent"].venue == "bluefin-perp"
    assert calls["ref_price"] == 64000.0


def test_place_order_bluefin_without_adapter_rejected(tmp_path):
    ledger = ExecLedger(str(tmp_path / "ledger.db"))
    a = SUIAdapter(ledger, KEY_HEX, testnet=True)  # no bluefin attached
    result = a.place_order(bluefin_intent(), 64000.0)
    assert result["ok"] is False
    assert "no Bluefin adapter configured" in result["error"]


def test_get_positions_reads_bluefin_and_normalizes(monkeypatch, tmp_path):
    a = make_adapter(tmp_path)
    monkeypatch.setattr(a.bluefin, "positions", lambda: {
        "ok": True,
        "data": [
            {"symbol": "BTC-PERP", "quantity": "0.050000", "entryPrice": "64000000000",
             "unrealizedPnl": "12500000"},
            {"symbol": "ETH-PERP", "quantity": "-2.000000", "entryPrice": "2500000000",
             "unrealizedPnl": "-5000000"},
            {"symbol": "SOL-PERP", "quantity": "0.000000", "entryPrice": "0"},
        ],
    })
    positions = a.get_positions()
    assert len(positions) == 2
    assert positions[0] == {"symbol": "BTC", "side": "long", "qty": 0.05,
                            "entry": 64000.0, "pnl": 12.5, "venue": "bluefin"}
    assert positions[1] == {"symbol": "ETH", "side": "short", "qty": 2.0,
                            "entry": 2500.0, "pnl": -5.0, "venue": "bluefin"}


def test_get_positions_handles_bluefin_error(monkeypatch, tmp_path):
    a = make_adapter(tmp_path)
    monkeypatch.setattr(a.bluefin, "positions", lambda: {"ok": False, "error": "boom"})
    assert a.get_positions() == []


def test_get_positions_without_bluefin_empty(tmp_path):
    ledger = ExecLedger(str(tmp_path / "ledger.db"))
    a = SUIAdapter(ledger, KEY_HEX, testnet=True)
    assert a.get_positions() == []


def test_cancel_all_merges_deepbook_and_bluefin(monkeypatch, tmp_path):
    a = make_adapter(tmp_path, deepbook_package=PACKAGE, pool_id=POOL,
                     balance_manager=BALANCE_MANAGER)
    bf_calls = []
    monkeypatch.setattr(a.bluefin, "cancel_all", lambda: bf_calls.append(1) or {"ok": True})

    # Stub the on-chain DeepBook RPC path (dry-run + broadcast) so no network.
    monkeypatch.setattr(a, "_dry_run", lambda tx: {"gas_price": 1000, "budget": 5_000_000})
    monkeypatch.setattr(a, "_broadcast",
                        lambda *x, **k: {"digest": "0xdeepbookcancel", "tx_bytes": b"", "signature": ""})

    result = a.cancel_all(1)
    assert result["ok"] is True
    assert bf_calls == [1]
    venues = [v["venue"] for v in result["cancelled"]]
    assert venues == ["deepbook", "bluefin"]


def test_cancel_all_only_bluefin(monkeypatch, tmp_path):
    a = make_adapter(tmp_path)  # deepbook unconfigured, bluefin attached
    monkeypatch.setattr(a.bluefin, "cancel_all", lambda: {"ok": True})
    result = a.cancel_all(1)
    assert result["ok"] is True
    assert [v["venue"] for v in result["cancelled"]] == ["bluefin"]


def test_flat_and_cancel_flattens_bluefin_positions(monkeypatch, tmp_path):
    a = make_adapter(tmp_path)
    monkeypatch.setattr(a.bluefin, "cancel_all", lambda: {"ok": True})
    monkeypatch.setattr(a.bluefin, "flat_and_cancel", lambda bid: {
        "ok": True, "closed": [{"BTC-PERP": True}], "cancelled": True, "errors": [],
    })
    result = a.flat_and_cancel(1)
    assert result["ok"] is True
    assert result["closed"] == [{"BTC-PERP": True}]
    assert "bluefin flattened 1 positions" in result["flat"]
