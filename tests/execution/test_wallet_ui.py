import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest

from wallet_ui import render_wallet_panel, render_killswitch_line


def test_panel_shows_connected_chain_masked(env=None):
    bots = [{"id": 1, "bot_name": "Whale", "paused": 0}]
    wallets = {"solana": {"address": "8xZqKjL2pAbCdEfGh1234567890abcdef", "status": "active"}}
    state = {"solana": {"balances": {"USDC": 412.50, "native": 0.02}, "positions": [{"symbol": "SOL"}]}}
    text = render_wallet_panel(bots, wallets, state)
    # masked, never full address
    assert "8xZq…" in text or "8xZqKj…" in text
    assert "8xZqKjL2pAbCdEfGh1234567890abcdef" not in text
    assert "$412.50" in text and "1 open" in text


def test_panel_shows_not_connected_and_coming_soon(env=None):
    text = render_wallet_panel([], {}, {})
    assert "NOT CONNECTED" in text
    assert "COMING SOON" in text
    assert "Hyperliquid" in text and "Sui" in text and "Solana" in text


def test_killswitch_line():
    assert "KILL-SWITCH ENGAGED" in render_killswitch_line(True)
    assert "armed" in render_killswitch_line(False)