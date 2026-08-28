"""Tests for the validated quant strategy engine (momentum20 + funding-carry overlay)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "agent"))

import pytest

from quant_strategy import (
    momentum20_return,
    realized_vol,
    risk_sized_units,
    momentum_decision,
    scan_momentum_book,
    pick_decision,
    MOMENTUM_THRESHOLD,
)


def _uptrend_closes(n=40, gain=0.005):
    """Closes that end 20d return > 2%."""
    px = 100.0
    out = []
    for _ in range(n):
        out.append(px)
        px *= 1 + gain
    return out


def _flat_closes(n=40):
    return [100.0] * n


def _downtrend_closes(n=40, loss=0.003):
    px = 100.0
    out = []
    for _ in range(n):
        out.append(px)
        px *= 1 - loss
    return out


def test_momentum20_return_positive_in_uptrend():
    closes = _uptrend_closes()
    r20 = momentum20_return(closes)
    assert r20 > MOMENTUM_THRESHOLD


def test_momentum20_return_zero_with_insufficient_data():
    assert momentum20_return([100.0, 101.0, 102.0]) == 0.0


def test_momentum20_flat_closes_no_signal():
    assert momentum20_return(_flat_closes()) <= MOMENTUM_THRESHOLD


def test_momentum_decision_longs_on_momentum():
    d = momentum_decision("BTC", "crypto", _uptrend_closes(), 100000.0,
                          has_long=False, has_short=False, current_price=120.0)
    assert d.action == "buy"
    assert d.qty > 0
    assert d.stop_pct == 8.0 and d.take_pct == 24.0  # 1:3 frozen


def test_momentum_decision_stays_cash_when_flat():
    d = momentum_decision("BTC", "crypto", _flat_closes(), 100000.0,
                          has_long=False, has_short=False, current_price=100.0)
    assert d.action == "hold"
    assert d.qty == 0.0


def test_momentum_decision_never_shorts():
    d = momentum_decision("BTC", "crypto", _downtrend_closes(), 100000.0,
                          has_long=False, has_short=False, current_price=90.0)
    assert d.action != "short"  # cash preferred over unvalidated directional short
    assert d.action in ("hold", "sell")


def test_momentum_decision_exits_when_momentum_decays():
    # already long, momentum has decayed -> flat the position
    d = momentum_decision("ETH", "crypto", _flat_closes(), 100000.0,
                          has_long=True, has_short=False, current_price=100.0)
    assert d.action == "sell"


def test_momentum_decision_holds_when_already_long_and_intact():
    d = momentum_decision("ETH", "crypto", _uptrend_closes(), 100000.0,
                          has_long=True, has_short=False, current_price=120.0)
    assert d.action == "hold"


def test_momentum_decision_crypto_only():
    d = momentum_decision("AAPL", "us-stock", _uptrend_closes(), 100000.0,
                          has_long=False, has_short=False, current_price=200.0)
    assert d.action == "hold"  # no evidence on equities -> capital preserved


def test_risk_sizing_caps_notional_at_max_position():
    qty, why = risk_sized_units(100000.0, entry=100.0, stop_pct=8.0, take_pct=24.0)
    notional = qty * 100.0
    assert 0 < notional <= 30000.0  # <= 30% of equity


def test_scan_momentum_book_respects_book_cap():
    closes = {"BTC": _uptrend_closes(), "ETH": _uptrend_closes()}
    prices = {"BTC": 100.0, "ETH": 100.0}
    portfolio = {"cash": 100000.0, "positions": []}
    decisions = scan_momentum_book(portfolio, closes, prices)
    buys = [d for d in decisions if d.action == "buy"]
    total = sum(d.qty * prices[d.symbol] for d in buys)
    # correlated BTC+ETH longs are ONE book: combined notional must stay <= 30%
    assert total <= 30000.0
    # and a single oversized demand gets demoted to hold
    big = {"BTC": _uptrend_closes(), "ETH": _uptrend_closes()}
    big_prices = {"BTC": 100.0, "ETH": 100.0}
    # force book over-cap: shrink equity so 2 positions would exceed 30%
    small = {"cash": 50000.0, "positions": []}
    decisions2 = scan_momentum_book(small, big, big_prices)
    buys2 = [d for d in decisions2 if d.action == "buy"]
    total2 = sum(d.qty * big_prices[d.symbol] for d in buys2)
    assert total2 <= 15000.0  # 30% of 50k


def test_pick_decision_prefers_exit_then_strongest_entry():
    from quant_strategy import QuantDecision
    buys = [QuantDecision("buy", "SOL", 5.0, 8.0, 24.0, "momentum20 LONG (20d +5.00%)"),
            QuantDecision("buy", "BTC", 1.0, 8.0, 24.0, "momentum20 LONG (20d +3.00%)")]
    pick = pick_decision(buys)
    assert pick.symbol == "SOL"  # strongest 20d wins
    # exit wins over entry
    sell = QuantDecision("sell", "ETH", 0.0, 0.0, 0.0, "decayed")
    pick2 = pick_decision([*buys, sell])
    assert pick2.action == "sell" and pick2.symbol == "ETH"