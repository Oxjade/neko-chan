"""Sui adapter + Aftermath perp integration: delegation, position sync, killswitch.

Aftermath is the perp CLOB on Sui (aftermath-perp venue). The SUIAdapter owns the
DeepBook spot path and delegates aftermath-perp orders / position reads to an
attached AftermathAdapter. This file covers the wiring end-to-end.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest

from aftermath_adapter import AftermathAdapter, build_aftermath
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
    aftermath = kw.pop("aftermath", None)
    if aftermath is None:
        aftermath = build_aftermath(ledger, KEY_HEX)
    return SUIAdapter(ledger, KEY_HEX, testnet=True,
                      aftermath=aftermath, **kw)


def aftermath_intent(**kw):
    base = dict(chain="sui", venue="aftermath-perp", symbol="BTC", side="buy",
                qty=0.01, order_type="market", leverage=5.0, idempotency_key="sui-af-1")
    base.update(kw)
    return OrderIntent(**base)


def test_adapter_exposes_aftermath():
    a = make_adapter(tempfile.mkdtemp())
    assert a.aftermath is not None
    assert isinstance(a.aftermath, AftermathAdapter)


def test_place_order_delegates_to_aftermath(monkeypatch, tmp_path):
    a = make_adapter(tmp_path)
    calls = {}

    def fake_place(intent, ref_price):
        calls["intent"] = intent
        calls["ref_price"] = ref_price
        return {"ok": True, "venue_order_id": "af-123", "price": ref_price}

    monkeypatch.setattr(a.aftermath, "place_order", fake_place)
    result = a.place_order(aftermath_intent(), 64000.0)
    assert result["ok"] is True
    assert calls["intent"].venue == "aftermath-perp"
    assert calls["ref_price"] == 64000.0


def test_place_order_aftermath_without_adapter_rejected(tmp_path):
    ledger = ExecLedger(str(tmp_path / "ledger.db"))
    a = SUIAdapter(ledger, KEY_HEX, testnet=True)  # no aftermath attached
    result = a.place_order(aftermath_intent(), 64000.0)
    assert result["ok"] is False
    assert "no Aftermath adapter configured" in result["error"]


def test_get_positions_reads_aftermath_and_normalizes(monkeypatch, tmp_path):
    a = make_adapter(tmp_path)
    monkeypatch.setattr(a.aftermath, "positions", lambda: {
        "ok": True,
        "data": [
            {"symbol": "BTC/USD:USDC", "contracts": 0.05, "entryPrice": 64000.0,
             "unrealizedPnl": 12.5},
            {"symbol": "ETH/USD:USDC", "contracts": -2.0, "entryPrice": 2500.0,
             "unrealizedPnl": -5.0},
            {"symbol": "SOL/USD:USDC", "contracts": 0.0, "entryPrice": 0.0},
        ],
    })
    positions = a.get_positions()
    assert len(positions) == 2
    assert positions[0] == {"symbol": "BTC", "side": "long", "qty": 0.05,
                            "entry": 64000.0, "pnl": 12.5, "venue": "aftermath"}
    assert positions[1] == {"symbol": "ETH", "side": "short", "qty": 2.0,
                            "entry": 2500.0, "pnl": -5.0, "venue": "aftermath"}


def test_get_positions_handles_aftermath_error(monkeypatch, tmp_path):
    a = make_adapter(tmp_path)
    monkeypatch.setattr(a.aftermath, "positions", lambda: {"ok": False, "error": "boom"})
    assert a.get_positions() == []


def test_get_positions_without_aftermath_empty(tmp_path):
    ledger = ExecLedger(str(tmp_path / "ledger.db"))
    a = SUIAdapter(ledger, KEY_HEX, testnet=True)
    assert a.get_positions() == []


def test_cancel_all_merges_deepbook_and_aftermath(monkeypatch, tmp_path):
    a = make_adapter(tmp_path, deepbook_package=PACKAGE, pool_id=POOL,
                     balance_manager=BALANCE_MANAGER)
    af_calls = []
    monkeypatch.setattr(a.aftermath, "cancel_all", lambda: af_calls.append(1) or {"ok": True})

    monkeypatch.setattr(a, "_dry_run", lambda tx: {"gas_price": 1000, "budget": 5_000_000})
    monkeypatch.setattr(a, "_broadcast",
                        lambda *x, **k: {"digest": "0xdeepbookcancel", "tx_bytes": b"", "signature": ""})

    result = a.cancel_all(1)
    assert result["ok"] is True
    assert af_calls == [1]
    venues = [v["venue"] for v in result["cancelled"]]
    assert venues == ["deepbook", "aftermath"]


def test_cancel_all_only_aftermath(monkeypatch, tmp_path):
    a = make_adapter(tmp_path)
    monkeypatch.setattr(a.aftermath, "cancel_all", lambda: {"ok": True})
    result = a.cancel_all(1)
    assert result["ok"] is True
    assert [v["venue"] for v in result["cancelled"]] == ["aftermath"]


def test_flat_and_cancel_flattens_aftermath_positions(monkeypatch, tmp_path):
    a = make_adapter(tmp_path)
    monkeypatch.setattr(a.aftermath, "cancel_all", lambda: {"ok": True})
    monkeypatch.setattr(a.aftermath, "flat_and_cancel", lambda bid: {
        "ok": True, "closed": [{"BTC/USD:USDC": True}], "cancelled": True, "errors": [],
    })
    result = a.flat_and_cancel(1)
    assert result["ok"] is True
    assert result["closed"] == [{"BTC/USD:USDC": True}]
    assert "aftermath flattened 1 positions" in result["flat"]


def test_aftermath_adapter_terms_auth():
    """Verify the Aftermath terms-signature authentication works."""
    from aftermath_adapter import _sign_terms_message
    seed = bytes(range(32))
    from sui_adapter import _ed25519_pubkey
    pub = _ed25519_pubkey(seed)
    bytes_b64, sig_b64 = _sign_terms_message(seed, pub)
    assert isinstance(bytes_b64, str) and len(bytes_b64) > 10
    assert isinstance(sig_b64, str) and len(sig_b64) > 20
    import base64
    decoded = base64.b64decode(bytes_b64)
    assert decoded == b"Aftermath Terms and Conditions"