"""Unit tests for hl_adapter. All network I/O is mocked; no real requests."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest
import requests
from cryptography.fernet import Fernet
from eth_account import Account
from eth_utils import keccak

os.environ.setdefault("TG_EXEC_MASTER_KEY", Fernet.generate_key().decode())

import hl_adapter
from hl_adapter import (
    HLApiWallet,
    HLAdapter,
    HL_MAINNET_INFO,
    HL_MAINNET_EXCHANGE,
    HL_TESTNET_INFO,
    HL_TESTNET_EXCHANGE,
    recover_agent_approval_signer,
    recover_l1_signer,
    sign_agent_approval,
)
from ledger import ExecLedger
from order_model import OrderIntent

MASTER_KEY = "0x" + "1" * 64
AGENT_KEY = "0x" + "2" * 64
MASTER_ADDR = Account.from_key(MASTER_KEY).address
AGENT_ADDR = Account.from_key(AGENT_KEY).address


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeHL:
    def __init__(self, router):
        self.router = router
        self.calls = []

    def __call__(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self.router(url, json)


def info_router(payload):
    t = payload.get("type")
    if t == "meta":
        return FakeResponse({"universe": [{"name": "BTC", "szDecimals": 5},
                                          {"name": "ETH", "szDecimals": 5}]})
    if t == "l2Book":
        return FakeResponse({"levels": [[{"px": "63000", "n": 1, "sz": "1"}],
                                        [{"px": "63100", "n": 1, "sz": "1"}]]})
    if t == "clearinghouseState":
        return FakeResponse({
            "marginSummary": {"accountValue": "1000", "totalMarginUsed": "50"},
            "withdrawable": "900",
            "assetPositions": [{"position": {
                "coin": "BTC", "szi": "0.05", "entryPx": "60000",
                "unrealizedPnl": "10", "liquidationPx": "50000",
                "leverage": {"type": "cross", "value": 2}}}],
        })
    if t == "openOrders":
        return FakeResponse([{"coin": "BTC", "oid": 123, "limitPx": "64000",
                              "sz": "0.01", "side": "B"}])
    if t == "userFills":
        return FakeResponse([{"coin": "BTC", "px": "63100", "sz": "-0.001", "side": "B",
                              "dir": "Buy", "time": 1700000000000, "hash": "0xabc", "oid": 9}])
    if t == "extraAgents":
        return FakeResponse([{"name": "neko", "address": AGENT_ADDR, "validUntil": 0}])
    raise AssertionError(f"unexpected info request type {t}")


def standard_router(url, payload):
    if url.endswith("/info"):
        return info_router(payload)
    assert url.endswith("/exchange"), url
    action = payload["action"]
    if action["type"] == "order":
        return FakeResponse({"status": "ok", "response": {"type": "order",
                                                          "data": {"statuses": [{"resting": {"oid": 777}}]}}})
    if action["type"] == "cancel":
        return FakeResponse({"status": "ok", "response": {"type": "cancel",
                                                          "data": {"statuses": ["success"] * len(action["cancels"])}}})
    if action["type"] == "approveAgent":
        return FakeResponse({"status": "ok", "response": {"type": "approveAgent"}})
    raise AssertionError(f"unexpected exchange action {action['type']}")


@pytest.fixture
def ledger(tmp_path):
    return ExecLedger(str(tmp_path / "ledger.db"))


@pytest.fixture
def fake(monkeypatch):
    def _make(router):
        fh = FakeHL(router)
        monkeypatch.setattr(requests, "post", fh)
        return fh

    return _make


def market_intent(**overrides) -> OrderIntent:
    base = dict(chain="hyperliquid", venue="hl-perp", symbol="BTC", side="buy",
                qty=0.001, order_type="market", leverage=1, idempotency_key="k1")
    base.update(overrides)
    return OrderIntent(**base)


def test_approval_payload_signing_deterministic():
    nonce = 123456789
    sig1 = sign_agent_approval(MASTER_KEY, AGENT_ADDR, "neko", nonce, is_mainnet=False)
    sig2 = sign_agent_approval(MASTER_KEY, AGENT_ADDR, "neko", nonce, is_mainnet=False)
    assert sig1 == sig2
    assert sig1["v"] in (27, 28)
    assert sig1["r"].startswith("0x") and len(sig1["r"]) == 66
    assert sig1["s"].startswith("0x") and len(sig1["s"]) == 66
    int(sig1["r"], 16)
    int(sig1["s"], 16)
    recovered = recover_agent_approval_signer(sig1, AGENT_ADDR, "neko", nonce, is_mainnet=False)
    assert recovered.lower() == MASTER_ADDR.lower()


def test_wallet_approve_agent_tx_shape_and_flow(fake):
    fh = fake(standard_router)
    wallet = HLApiWallet(AGENT_KEY, testnet=True)
    tx = wallet.approve_agent_tx(MASTER_KEY, name="neko")
    action = tx["action"]
    assert action["type"] == "approveAgent"
    assert action["hyperliquidChain"] == "Testnet"
    assert action["agentAddress"] == wallet.address.lower()
    assert action["agentName"] == "neko"
    assert action["signatureChainId"] == "0x66eee"
    assert set(tx["signature"]) == {"r", "s", "v"}
    assert tx["nonce"] == action["nonce"] > 0
    assert wallet.is_agent_approved(MASTER_ADDR) is True
    resp = wallet.submit_agent_approval(MASTER_KEY, name="neko")
    assert resp["status"] == "ok"
    assert fh.calls[-1]["json"]["action"]["type"] == "approveAgent"


def test_wallet_generate_reuses_vault_material(monkeypatch):
    monkeypatch.setattr(hl_adapter, "generate_key_material",
                        lambda chain: ("0x" + "a" * 40, "0x" + "b" * 64))
    wallet = HLApiWallet.generate(testnet=True)
    assert wallet.address == Account.from_key("0x" + "b" * 64).address
    assert wallet.testnet is True


def test_place_order_market_maps_to_exchange_payload(fake, ledger):
    fh = fake(standard_router)
    adapter = HLAdapter(ledger, AGENT_KEY, MASTER_ADDR, testnet=True)
    result = adapter.place_order(market_intent(), bot_id=1)
    assert result["ok"] is True
    assert result["venue_order_id"] == "777"
    assert result["status"] == "resting"
    posted = fh.calls[-1]["json"]
    action = posted["action"]
    assert action["type"] == "order"
    order = action["orders"][0]
    assert order["a"] == 0
    assert order["b"] is True
    assert order["s"] == "0.001"
    assert order["t"] == {"limit": {"tif": "Ioc"}}
    assert float(order["p"]) > 63100
    assert order["r"] is False
    assert order["c"] == "0x" + keccak(b"k1")[:16].hex()
    assert action["grouping"] == "na"
    assert posted["nonce"] > 0
    assert set(posted["signature"]) == {"r", "s", "v"}
    recovered = recover_l1_signer(action, posted["signature"], posted["nonce"], is_mainnet=False)
    assert recovered.lower() == adapter.agent_address.lower()
    assert ledger.order_exists("k1") is True


@pytest.mark.parametrize("order_type,tpsl", [("stop", "sl"), ("take_profit", "tp")])
def test_trigger_intents_map_to_trigger_orders(fake, ledger, order_type, tpsl):
    fh = fake(standard_router)
    adapter = HLAdapter(ledger, AGENT_KEY, MASTER_ADDR, testnet=True)
    intent = market_intent(order_type=order_type, limit_price=55000.0, side="sell",
                           idempotency_key=f"k-{order_type}")
    result = adapter.place_order(intent, bot_id=1)
    assert result["ok"] is True
    order = fh.calls[-1]["json"]["action"]["orders"][0]
    assert order["t"] == {"trigger": {"isMarket": True, "triggerPx": "55000", "tpsl": tpsl}}
    assert order["b"] is False
    assert order["a"] == 0


def test_flat_and_cancel_closes_positions_then_cancels(fake, ledger):
    fh = fake(standard_router)
    adapter = HLAdapter(ledger, AGENT_KEY, MASTER_ADDR, testnet=True)
    result = adapter.flat_and_cancel(bot_id=1)
    assert result["ok"] is True
    assert result["flattened"] == ["BTC"]
    assert result["cancelled"] == 1
    exchange_calls = [c for c in fh.calls if c["url"].endswith("/exchange")]
    assert [c["json"]["action"]["type"] for c in exchange_calls] == ["order", "cancel"]
    flat_order = exchange_calls[0]["json"]["action"]["orders"][0]
    assert flat_order["a"] == 0
    assert flat_order["b"] is False
    assert flat_order["s"] == "0.05"
    assert flat_order["r"] is True
    assert flat_order["t"] == {"limit": {"tif": "Ioc"}}
    cancel_payload = exchange_calls[1]["json"]["action"]
    assert cancel_payload["type"] == "cancel"
    assert cancel_payload["cancels"] == [{"a": 0, "o": 123}]


def test_network_failure_never_raises(fake, ledger, monkeypatch):
    monkeypatch.setattr(hl_adapter, "BACKOFF_BASE_SECONDS", 0.0)

    def boom(url, payload):
        raise requests.ConnectionError("connection refused")

    fh = fake(boom)
    adapter = HLAdapter(ledger, AGENT_KEY, MASTER_ADDR, testnet=False)
    intent = market_intent(order_type="limit", limit_price=64000.0, idempotency_key="k-net")
    result = adapter.place_order(intent, bot_id=1)
    assert result["ok"] is False
    assert "refused" in result["error"]
    assert len(fh.calls) == hl_adapter.MAX_RETRIES + 1
    assert adapter.get_account_state()["ok"] is False
    assert adapter.cancel_all()["ok"] is False
    assert adapter.get_fills() == []
    assert adapter.flat_and_cancel()["ok"] is False


def test_http_500_retries_then_returns_error(fake, ledger, monkeypatch):
    monkeypatch.setattr(hl_adapter, "BACKOFF_BASE_SECONDS", 0.0)

    def flaky(url, payload):
        if url.endswith("/exchange"):
            raise requests.HTTPError("HTTP 500", response=FakeResponse({}, status_code=500))
        return info_router(payload)

    fh = fake(flaky)
    adapter = HLAdapter(ledger, AGENT_KEY, MASTER_ADDR, testnet=False)
    intent = market_intent(order_type="limit", limit_price=64000.0, idempotency_key="k-500")
    result = adapter.place_order(intent, bot_id=1)
    assert result["ok"] is False
    assert "500" in result["error"]
    exchange_calls = [c for c in fh.calls if c["url"].endswith("/exchange")]
    assert len(exchange_calls) == hl_adapter.MAX_RETRIES + 1


def test_get_account_state_and_fills_shape(fake, ledger):
    fake(standard_router)
    adapter = HLAdapter(ledger, AGENT_KEY, MASTER_ADDR, testnet=True)
    state = adapter.get_account_state()
    assert state["ok"] is True
    assert state["balances"]["USDC"] == 1000.0
    assert state["balances"]["withdrawable"] == 900.0
    pos = state["positions"][0]
    assert pos["coin"] == "BTC" and pos["side"] == "long" and pos["qty"] == 0.05
    fills = adapter.get_fills()
    assert fills[0]["coin"] == "BTC"
    assert fills[0]["side"] == "buy" and fills[0]["tx_hash"] == "0xabc"


def test_testnet_url_selection(ledger):
    t = HLAdapter(ledger, AGENT_KEY, MASTER_ADDR, testnet=True)
    assert t.info_url == HL_TESTNET_INFO
    assert t.exchange_url == HL_TESTNET_EXCHANGE
    m = HLAdapter(ledger, AGENT_KEY, MASTER_ADDR, testnet=False)
    assert m.info_url == HL_MAINNET_INFO
    assert m.exchange_url == HL_MAINNET_EXCHANGE
    assert HLApiWallet(AGENT_KEY, testnet=True).exchange_url == HL_TESTNET_EXCHANGE
    assert HLApiWallet(AGENT_KEY).exchange_url == HL_MAINNET_EXCHANGE