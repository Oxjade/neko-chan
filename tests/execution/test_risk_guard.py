import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest

from order_model import OrderIntent
from risk_guard import BotRiskProfile, RiskGuard, WalletState


def intent(**kw):
    base = dict(chain="hyperliquid", venue="hl-perp", symbol="BTC", side="buy",
                qty=0.005, order_type="market", stop_loss=48100, take_profit=52500,
                leverage=5.0, idempotency_key="k1")
    base.update(kw)
    return OrderIntent(**base)


def wallet(**kw):
    base = dict(usd_balance=1000.0, open_exposure_usd=0.0, open_positions=0, realized_pnl_today=0.0)
    base.update(kw)
    return WalletState(**base)


def test_clean_pass():
    g = RiskGuard()
    assert g.check(1, intent(), 50000.0, wallet()) == []


def test_notional_cap():
    g = RiskGuard()
    v = g.check(1, intent(qty=0.1), 50000.0, wallet())
    assert any("notional" in e for e in v)  # $7,800 > $500


def test_exposure_cap():
    g = RiskGuard()
    v = g.check(1, intent(), 50000.0, wallet())  # $250 ok alone
    assert v == []
    v = g.check(1, intent(qty=0.006), 50000.0, wallet(open_exposure_usd=200.0))
    assert any("exposure" in e for e in v)  # 200+300 > 30% of 1000


def test_leverage_cap_combined_with_venue():
    g = RiskGuard(BotRiskProfile(max_leverage=5.0))
    v = g.check(1, intent(leverage=10.0), 50000.0, wallet())
    assert any("leverage" in e for e in v)
    v = g.check(1, intent(leverage=5.0), 50000.0, wallet())
    assert v == []


def test_mandatory_stop_on_leverage():
    g = RiskGuard()
    v = g.check(1, intent(leverage=5.0, stop_loss=None), 50000.0, wallet())
    assert any("mandatory stop-loss" in e for e in v)
    # stop too tight / too wide vs ref price 50000
    v = g.check(1, intent(stop_loss=49700.0), 50000.0, wallet())
    assert any("too tight" in e for e in v)
    v = g.check(1, intent(stop_loss=45500.0), 50000.0, wallet())
    assert any("too wide" in e for e in v)


def test_daily_loss_halt():
    g = RiskGuard()
    w = wallet(realized_pnl_today=-40.0)  # -4% of 1000
    v = g.check(1, intent(), 78000.0, w)
    assert any("daily loss halt" in e for e in v)


def test_max_positions():
    g = RiskGuard()
    v = g.check(1, intent(), 78000.0, wallet(open_positions=5))
    assert any("max open positions" in e for e in v)


def test_killswitch_blocks_everything():
    g = RiskGuard()
    g.engage_killswitch(1)
    v = g.check(1, intent(), 50000.0, wallet())
    assert any("killswitch" in e for e in v)
    g.release_killswitch(1)
    assert g.check(1, intent(), 50000.0, wallet()) == []