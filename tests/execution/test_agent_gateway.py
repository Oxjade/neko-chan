"""Tests for the live agent's real-execution routing (execution gateway bridge)."""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "agent"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

os.environ.setdefault("LIVE_AGENT_EXECUTION", "0")
os.environ.setdefault("LIVE_AGENT_NAME", "TestAgent")

import pytest

import live_agent
from order_model import OrderIntent


class FakeRouter:
    def __init__(self):
        self.last = None

    def submit_and_sync(self, bot_id, intent, ref_price):
        self.last = (bot_id, intent, ref_price)
        return {"ok": True, "order_id": 42, "status": "submitted"}


class FakeGateway:
    def __init__(self, adapters, router=None, chain_states=None):
        self.adapters = adapters
        self.router = router or FakeRouter()
        self.chain_states = chain_states or {
            "hyperliquid": {"balances": {"USDC": 100.0}, "positions": []},
            "solana": {"balances": {"USDC": 50.0}, "positions": []},
            "sui": {"balances": {"USDC": 20.0}, "positions": []},
        }

        def wallet_by_bot_chain(bot_id, chain):
            return {"id": {"hyperliquid": 1, "solana": 2, "sui": 3}.get(chain, 0), "chain": chain}

        def load_chain_state(wallet_id):
            chain = {1: "hyperliquid", 2: "solana", 3: "sui"}.get(wallet_id)
            state = self.chain_states.get(chain, {"balances": {}, "positions": []})
            return {"balances": state["balances"], "positions": state["positions"]}

        self.ledger = types.SimpleNamespace(
            wallet_by_bot_chain=wallet_by_bot_chain,
            load_chain_state=load_chain_state,
        )
        self.sync_engine = types.SimpleNamespace()
        self.ready = True

    def sync(self, bot_id, chain):
        return {"ok": True}

    def route_and_sync(self, bot_id, intent, ref_price):
        return self.router.submit_and_sync(bot_id, intent, ref_price)


@pytest.fixture(autouse=True)
def _reset_module_global():
    live_agent._exec_gateway = None
    yield
    live_agent._exec_gateway = None


def _gw(chains=("hyperliquid", "solana", "sui")):
    return FakeGateway({c: object() for c in chains})


def test_venue_resolution_crypto_prefers_hyperliquid():
    gw = _gw(chains=("hyperliquid", "solana"))
    assert live_agent._resolve_real_venue("BTC", "crypto", gw) == ("hyperliquid", "hl-perp")


def test_venue_resolution_falls_back_to_jupiter():
    gw = _gw(chains=("solana",))
    assert live_agent._resolve_real_venue("ETH", "crypto", gw) == ("solana", "jup-perp")


def test_venue_resolution_us_stock_requires_solana():
    gw = _gw(chains=("hyperliquid",))
    assert live_agent._resolve_real_venue("AAPL", "us-stock", gw) is None
    gw = _gw(chains=("solana",))
    assert live_agent._resolve_real_venue("AAPL", "us-stock", gw) == ("solana", "xstocks-spot")


def test_venue_resolution_forex_unsupported_v1():
    gw = _gw()
    assert live_agent._resolve_real_venue("EURUSD", "forex", gw) is None


def test_route_real_order_open_attaches_stop_and_tp():
    gw = _gw()
    res = live_agent.route_real_order(
        gw, 7, "BTC", "crypto", "buy", 0.01,
        stop_pct=5.0, take_pct=10.0, ref_price=80000, leverage=20)
    assert res["ok"] is True
    bot_id, intent, ref = gw.router.last
    assert bot_id == 7 and ref == 80000
    assert isinstance(intent, OrderIntent)
    assert intent.chain == "hyperliquid" and intent.venue == "hl-perp"
    assert intent.side == "buy" and intent.leverage == 20
    assert intent.stop_loss == pytest.approx(76000.0)
    assert intent.take_profit == pytest.approx(88000.0)
    assert intent.idempotency_key.startswith("agent:TestAgent:BTC:buy:")


def test_route_real_order_close_is_1x_no_rearmed_stops():
    gw = _gw()
    res = live_agent.route_real_order(
        gw, 7, "BTC", "crypto", "sell", 0.01,
        stop_pct=5.0, take_pct=10.0, ref_price=81000, leverage=2)
    assert res["ok"] is True
    _bot_id, intent, _ref = gw.router.last
    assert intent.leverage == 1.0
    assert intent.stop_loss is None and intent.take_profit is None


def test_route_real_order_rejects_unsupported_market():
    gw = _gw(chains=("hyperliquid",))
    res = live_agent.route_real_order(
        gw, 7, "EURUSD", "forex", "buy", 1000.0,
        stop_pct=0, take_pct=0, ref_price=1.1, leverage=1)
    assert res["ok"] is False
    assert "no real venue" in res["error"]


def test_get_real_portfolio_aggregates_chains():
    states = {
        "hyperliquid": {"balances": {"USDC": 100.0}, "positions": []},
        "solana": {"balances": {"USDC": 50.0}, "positions": []},
        "sui": {"balances": {"USDC": 20.0},
                "positions": [{"symbol": "SUI/USDC", "side": "long", "qty": 10.0, "entry_px": 1.5}]},
    }
    gw = FakeGateway({"hyperliquid": object(), "solana": object(), "sui": object()},
                     chain_states=states)
    portfolio = live_agent.get_real_portfolio(gw, 7)
    assert portfolio["cash"] == pytest.approx(170.0)  # 100 + 50 + 20 across chains
    assert len(portfolio["positions"]) == 1
    assert portfolio["positions"][0]["quantity"] == 10.0  # signed: positive long
    assert portfolio["positions"][0]["side"] == "long"


def test_gateway_none_when_execution_disabled():
    live_agent.EXEC_ENABLED = False
    assert live_agent._get_exec_gateway() is None

# ---------------- per-asset leverage clamping ----------------

def test_clamp_leverage_asset_caps():
    from live_agent import clamp_leverage
    # BTC caps at 20x on Aftermath
    assert clamp_leverage("BTC", "crypto", 50) == 20
    assert clamp_leverage("BTC", "crypto", 20) == 20
    # SOL caps at 20x on Aftermath (market-specifications: SOL max 20x)
    assert clamp_leverage("SOL", "crypto", 25) == 20
    assert clamp_leverage("SOL", "crypto", 30) == 20
    # Unknown symbol defaults to 10x cap; the floor is bounded by the cap so
    # it never forces leverage above what the venue allows.
    assert clamp_leverage("ATOM", "crypto", 10) == 10
    assert clamp_leverage("ATOM", "crypto", 15) == 10
    # 1x stays 1x (safe default)
    assert clamp_leverage("BTC", "crypto", 1) == 1
    # non-crypto is always 1x
    assert clamp_leverage("AAPL", "us-stock", 10) == 1
