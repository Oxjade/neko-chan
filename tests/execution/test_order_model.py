import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest

from order_model import OrderIntent, resolve_adapter_name


def make_intent(**kw):
    base = dict(chain="hyperliquid", venue="hl-perp", symbol="BTC", side="buy",
                qty=0.01, order_type="market", limit_price=None, stop_loss=None,
                take_profit=None, leverage=1.0, idempotency_key="k1")
    base.update(kw)
    return OrderIntent(**base)


def test_valid_intent():
    i = make_intent()
    assert i.validate(78000.0) == []
    assert i.notional(78000.0) == pytest.approx(780.0)


def test_venue_chain_mismatch_rejected():
    i = make_intent(chain="solana", venue="hl-perp")
    assert any("does not belong" in e for e in i.validate(100.0))


def test_leverage_caps_per_venue():
    assert make_intent(leverage=51).validate(100.0)  # hl cap 50
    j = make_intent(chain="solana", venue="jup-perp", leverage=101)
    assert any("leverage" in e for e in j.validate(100.0))
    assert make_intent(chain="solana", venue="jup-perp", leverage=100).validate(100.0) == []


def test_limit_requires_price():
    i = make_intent(order_type="limit", limit_price=None)
    assert any("limit price" in e for e in i.validate(100.0))


def test_idempotency_required():
    i = make_intent(idempotency_key="")
    assert any("idempotency_key" in e for e in i.validate(100.0))


def test_adapter_resolution():
    assert resolve_adapter_name("hl-perp") == "hl_adapter"
    assert resolve_adapter_name("xstocks-spot") == "sol_adapter"
    assert resolve_adapter_name("deepbook-margin") == "sui_adapter"