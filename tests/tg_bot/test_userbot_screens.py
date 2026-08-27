import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("TG_VAULT_MASTER_KEY", Fernet.generate_key().decode())

from key_vault import KeyVault
from store import Registry
from userbot import render_dashboard_text, render_positions_text


@pytest.fixture()
def reg():
    tmp = tempfile.mkdtemp()
    r = Registry(os.path.join(tmp, "reg.db"), KeyVault())
    yield r
    r.close()


PORTFOLIO = {"cash": 98000.0, "positions": [
    {"symbol": "BTC", "market": "crypto", "side": "long", "quantity": 0.1,
     "entry_price": 78600.0, "current_price": 78900.0, "leverage": 5.0,
     "stop_loss": 70000.0, "take_profit": 85000.0},
    {"symbol": "EURUSD", "market": "forex", "side": "long", "quantity": 5000,
     "entry_price": 1.1659, "current_price": 1.1660, "leverage": 1.0,
     "stop_loss": 1.14, "take_profit": 1.21},
]}

EMPTY = {"cash": 100000.0, "positions": []}


def test_dashboard_is_full_telemetry_panel():
    text = render_dashboard_text({"bot_name": "Whale", "is_running": 1,
                                  "interval_sec": 120, "last_heartbeat": None}, PORTFOLIO)
    assert "LIVE TELEMETRY" in text
    assert "EQUITY" in text and "PERFORMANCE" in text and "POSITIONS" in text
    assert "NEXT DECISION" in text
    assert "BTC" in text and "5x" in text
    assert "<b>" in text  # HTML formatting fills the screen structure


def test_dashboard_works_with_zero_trades():
    """Even with 0 trades the panel is complete, no empty cues."""
    text = render_dashboard_text({"bot_name": "FreshBot", "is_running": 1,
                                  "interval_sec": 300, "last_heartbeat": None}, EMPTY)
    assert "LIVE TELEMETRY" in text
    assert "no open positions" in text
    assert "$100,000" in text
    assert "NEXT DECISION" in text
    assert "PERFORMANCE" in text


def test_positions_renders_leverage_and_symbols():
    text = render_positions_text(PORTFOLIO)
    assert "BTC" in text and "LONG" in text
    assert "5x" in text
    assert "EURUSD" in text


def test_positions_without_prices_fall_back_to_entry():
    pf = {"positions": [{"symbol": "BTC", "side": "long", "quantity": 1,
                         "entry_price": 50000.0, "current_price": None}]}
    text = render_positions_text(pf)
    assert "+0.00" in text or "now 50000.00" in text