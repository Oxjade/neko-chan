import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import requests
from solders.keypair import Keypair

from ledger import ExecLedger
from order_model import OrderIntent
from sol_adapter import (
    DEVNET_RPC,
    MAINNET_RPC,
    PERPS_ORDERS_URL,
    PERPS_POSITIONS_URL,
    SOLAdapter,
    SWAP_QUOTE_URL,
    SWAP_SWAP_URL,
    TOKENS_SEARCH_URL,
    TRIGGER_BASE,
    USDC_MINT,
)


class Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = repr(payload)

    def json(self):
        return self._payload


def make_adapter():
    kp = Keypair()
    return SOLAdapter(ExecLedger(":memory:"), bytes(kp.secret()).hex())


def fake_network(monkeypatch, adapter, handler):
    monkeypatch.setattr(adapter.session, "request", handler)
    monkeypatch.setattr(adapter, "_sign_tx", lambda tx: "signed-b64")
    monkeypatch.setattr(adapter, "_broadcast", lambda signed: "sig-broadcast")
    return adapter


def intent(venue="xstocks-spot", symbol="AAPLx", side="buy", qty=2.0, **kw):
    return OrderIntent(chain="solana", venue=venue, symbol=symbol, side=side, qty=qty,
                       order_type=kw.pop("order_type", "market"),
                       leverage=kw.pop("leverage", 1.0), idempotency_key="k-1", **kw)


# ---------------- balance reads ----------------

def test_native_balance_parses_rpc_response(monkeypatch):
    a = fake_network(monkeypatch, make_adapter(), lambda m, u, **kw: Resp(
        {"jsonrpc": "2.0", "result": {"value": 1_234_567_890}}))
    assert a.get_balance("SOL") == 1.23456789


def test_usdc_balance_parses_token_account(monkeypatch):
    calls = []

    def handler(method, url, **kw):
        calls.append(kw["json"])
        return Resp({"result": {"value": [
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 42.5}}}}}},
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 7.5}}}}}},
        ]}})

    a = fake_network(monkeypatch, make_adapter(), handler)
    assert a.get_balance("USDC") == 50.0
    assert calls[0]["method"] == "getTokenAccountsByOwner"
    assert calls[0]["params"][1] == {"mint": USDC_MINT}


def test_usdc_balance_empty_returns_zero(monkeypatch):
    a = fake_network(monkeypatch, make_adapter(), lambda m, u, **kw: Resp({"result": {"value": []}}))
    assert a.get_balance("USDC") == 0.0


def test_balance_network_error_returns_zero(monkeypatch):
    a = fake_network(monkeypatch, make_adapter(),
                     lambda m, u, **kw: (_ for _ in ()).throw(requests.ConnectionError("down")))
    assert a.get_balance("SOL") == 0.0
    assert a.get_balance("USDC") == 0.0


# ---------------- place_order venue mapping ----------------

def test_place_order_jup_perp_posts_to_perps_orders(monkeypatch):
    urls = []
    bodies = []

    def handler(method, url, **kw):
        urls.append(url)
        bodies.append(kw["json"])
        return Resp({"transaction": "dG94"})

    a = fake_network(monkeypatch, make_adapter(), handler)
    res = a.place_order(intent("jup-perp", "SOL", qty=0.1, leverage=2,
                               stop_loss=140.0, take_profit=160.0), 150.0)
    assert res["ok"] is True
    assert res["tx_hash"] == "sig-broadcast"
    assert urls == [PERPS_ORDERS_URL]
    assert bodies[0]["side"] == "buy" and bodies[0]["size"] == 0.1
    assert bodies[0]["leverage"] == 2 and bodies[0]["reduceOnly"] is False


def test_place_order_jup_limit_uses_trigger_v2_oco(monkeypatch):
    urls = []
    created = {}

    def handler(method, url, **kw):
        urls.append(url)
        if url == f"{TRIGGER_BASE}/auth/challenge":
            return Resp({"message": "sign-me"})
        if url == f"{TRIGGER_BASE}/auth/verify":
            return Resp({"token": "jwt-1"})
        if url == f"{TRIGGER_BASE}/vault":
            return Resp({"vaultPubkey": "VAULT1"})
        if url == f"{TRIGGER_BASE}/deposit/craft":
            return Resp({"transaction": "dG94", "requestId": "r1"})
        if url == f"{TRIGGER_BASE}/orders/price":
            created.update(kw["json"])
            return Resp({"id": "ord-9", "txSignature": "tx9"})
        if url.startswith(TOKENS_SEARCH_URL):
            return Resp({"results": [{"address": "SOLMINT", "symbol": "SOL", "decimals": 9}]})
        raise AssertionError(f"unexpected url {url}")

    a = fake_network(monkeypatch, make_adapter(), handler)
    res = a.place_order(intent("jup-limit", "SOL", qty=1.0, order_type="limit", limit_price=148.0,
                               stop_loss=140.0, take_profit=160.0), 150.0)
    assert res["ok"] is True
    assert res["venue_order_id"] == "ord-9"
    assert urls[-1] == f"{TRIGGER_BASE}/orders/price"
    assert created["orderSubType"] == "oco"
    assert created["triggerCondition"] == "<"
    assert created["triggerMint"] == "SOLMINT"
    assert created["vaultPubkey"] == "VAULT1"
    assert created["depositRequestId"] == "r1"


def test_place_order_xstocks_quotes_and_swaps(monkeypatch):
    urls = []
    quotes = []
    swaps = []

    def handler(method, url, **kw):
        urls.append(url)
        if url.startswith(TOKENS_SEARCH_URL):
            return Resp({"results": [{"address": "AAPLxMINT", "symbol": "AAPLx", "decimals": 8}]})
        if url.startswith(SWAP_QUOTE_URL):
            quotes.append(kw["params"])
            return Resp({"outAmount": 12345})
        if url == SWAP_SWAP_URL:
            swaps.append(kw["json"])
            return Resp({"swapTransaction": "dG94"})
        raise AssertionError(f"unexpected url {url}")

    a = fake_network(monkeypatch, make_adapter(), handler)
    res = a.place_order(intent("xstocks-spot", "AAPLx", qty=2.0), 250.0)
    assert res["ok"] is True
    assert res["tx_hash"] == "sig-broadcast"
    assert urls[0].startswith(TOKENS_SEARCH_URL)
    assert urls[1].startswith(SWAP_QUOTE_URL)
    assert urls[2] == SWAP_SWAP_URL
    assert quotes[0]["inputMint"] == USDC_MINT
    assert quotes[0]["outputMint"] == "AAPLxMINT"
    assert quotes[0]["amount"] == int(2.0 * 250.0 * 1e6)
    assert swaps[0]["userPublicKey"] == a.pubkey


def test_place_order_xstocks_sell_uses_token_as_input(monkeypatch):
    quotes = []

    def handler(method, url, **kw):
        if url.startswith(TOKENS_SEARCH_URL):
            return Resp({"results": [{"address": "AAPLxMINT", "symbol": "AAPLx", "decimals": 8}]})
        if url.startswith(SWAP_QUOTE_URL):
            quotes.append(kw["params"])
            return Resp({"outAmount": 12345})
        if url == SWAP_SWAP_URL:
            return Resp({"swapTransaction": "dG94"})
        raise AssertionError(f"unexpected url {url}")

    a = fake_network(monkeypatch, make_adapter(), handler)
    res = a.place_order(intent("xstocks-spot", "AAPLx", side="sell", qty=2.0), 250.0)
    assert res["ok"] is True
    assert quotes[0]["inputMint"] == "AAPLxMINT"
    assert quotes[0]["outputMint"] == USDC_MINT
    assert quotes[0]["amount"] == int(2.0 * 1e8)


# ---------------- failure tolerance ----------------

def test_broadcast_failure_returns_ok_false_never_raises(monkeypatch):
    def handler(method, url, **kw):
        if url.startswith(TOKENS_SEARCH_URL):
            return Resp({"results": [{"address": "AAPLxMINT", "symbol": "AAPLx", "decimals": 8}]})
        if url.startswith(SWAP_QUOTE_URL):
            return Resp({"outAmount": 12345})
        if url == SWAP_SWAP_URL:
            return Resp({"swapTransaction": "dG94"})
        raise AssertionError(f"unexpected url {url}")

    a = fake_network(monkeypatch, make_adapter(), handler)
    monkeypatch.setattr(a, "_broadcast", lambda signed: (_ for _ in ()).throw(RuntimeError("rpc down")))
    res = a.place_order(intent("xstocks-spot", "AAPLx"), 250.0)
    assert res["ok"] is False
    assert "rpc down" in res["error"]


def test_place_order_network_error_returns_ok_false(monkeypatch):
    a = fake_network(monkeypatch, make_adapter(),
                     lambda m, u, **kw: (_ for _ in ()).throw(requests.ConnectionError("down")))
    res = a.place_order(intent("xstocks-spot", USDC_MINT), 250.0)
    assert res["ok"] is False
    assert "down" in res["error"]


def test_place_order_unknown_venue_rejected():
    a = make_adapter()
    res = a.place_order(intent("deepbook-spot", "SUI"), 1.0)
    assert res["ok"] is False


# ---------------- flat_and_cancel sequence ----------------

def test_flat_and_cancel_closes_perps_then_cancels_limits(monkeypatch):
    calls = []
    position_fetches = [0]
    close_bodies = []

    def handler(method, url, **kw):
        calls.append(url)
        if url.startswith(PERPS_POSITIONS_URL):
            position_fetches[0] += 1
            if position_fetches[0] == 1:
                return Resp({"positions": [
                    {"symbol": "SOL", "side": "long", "qty": 2.0, "entryPrice": 150.0, "leverage": 2},
                    {"symbol": "BTC", "side": "short", "qty": 0.5, "entryPrice": 60000.0, "leverage": 3},
                ]})
            return Resp({"positions": []})
        if url == PERPS_ORDERS_URL:
            close_bodies.append(kw["json"])
            return Resp({"transaction": "dG94"})
        if url == f"{TRIGGER_BASE}/auth/challenge":
            return Resp({"message": "m"})
        if url == f"{TRIGGER_BASE}/auth/verify":
            return Resp({"token": "jwt"})
        if url.startswith(f"{TRIGGER_BASE}/orders/history"):
            return Resp({"orders": [{"id": "lo-1"}]})
        if url == f"{TRIGGER_BASE}/orders/price/cancel/lo-1":
            return Resp({"cancelRequestId": "cr-1", "transaction": "dG94"})
        if url == f"{TRIGGER_BASE}/orders/price/confirm-cancel/lo-1":
            return Resp({"status": "cancelled"})
        raise AssertionError(f"unexpected url {url}")

    a = fake_network(monkeypatch, make_adapter(), handler)
    res = a.flat_and_cancel(7)
    assert res["ok"] is True
    assert len(res["flat"]) == 2
    assert all(c["ok"] for c in res["flat"])
    close_idx = [i for i, u in enumerate(calls) if u == PERPS_ORDERS_URL]
    cancel_idx = [i for i, u in enumerate(calls) if "cancel" in u]
    assert close_idx and cancel_idx
    assert max(close_idx) < min(cancel_idx)
    assert close_bodies[0]["reduceOnly"] is True
    assert close_bodies[0]["side"] == "sell" and close_bodies[0]["size"] == 2.0
    assert close_bodies[1]["side"] == "buy" and close_bodies[1]["size"] == 0.5
    assert res["cancel"]["limit_cancelled"] == [{"id": "lo-1", "ok": True}]


def test_cancel_all_best_effort_on_auth_failure(monkeypatch):
    def handler(method, url, **kw):
        if url.startswith(PERPS_POSITIONS_URL):
            return Resp({"positions": []})
        if url == f"{TRIGGER_BASE}/auth/challenge":
            return Resp({"message": "m"})
        if url == f"{TRIGGER_BASE}/auth/verify":
            return Resp({})
        raise AssertionError(f"unexpected url {url}")

    a = fake_network(monkeypatch, make_adapter(), handler)
    res = a.cancel_all(7)
    assert res["ok"] is False
    assert res["errors"]
    assert "auth failed" in res["errors"][0]


def test_cancel_all_never_closes_positions(monkeypatch):
    """cancel_all() must only cancel resting orders - never flatten perps.
    (Regression: it used to call _perp_close_all(), so flat_and_cancel closed
    positions twice and a 'cancel my orders' control liquidated the account.)"""
    calls = []
    close_bodies = []

    def handler(method, url, **kw):
        calls.append(url)
        if url.startswith(PERPS_POSITIONS_URL):
            return Resp({"positions": [
                {"symbol": "SOL", "side": "long", "qty": 2.0, "entryPrice": 150.0, "leverage": 2},
            ]})
        if url == PERPS_ORDERS_URL:
            close_bodies.append(kw["json"])
            return Resp({"transaction": "dG94"})
        if url == f"{TRIGGER_BASE}/auth/challenge":
            return Resp({"message": "m"})
        if url == f"{TRIGGER_BASE}/auth/verify":
            return Resp({"token": "jwt"})
        if url.startswith(f"{TRIGGER_BASE}/orders/history"):
            return Resp({"orders": [{"id": "lo-1"}]})
        if url == f"{TRIGGER_BASE}/orders/price/cancel/lo-1":
            return Resp({"cancelRequestId": "cr-1", "transaction": "dG94"})
        if url == f"{TRIGGER_BASE}/orders/price/confirm-cancel/lo-1":
            return Resp({"status": "cancelled"})
        raise AssertionError(f"unexpected url {url}")

    a = fake_network(monkeypatch, make_adapter(), handler)
    res = a.cancel_all(7)
    assert res["ok"] is True
    assert "limit_cancelled" in res
    assert "perp_closed" not in res
    # PERPS_POSITIONS_URL (positions read) must never be hit, and no perp close body sent.
    assert not any(u == PERPS_POSITIONS_URL for u in calls)
    assert close_bodies == []


# ---------------- rpc selection ----------------

def test_devnet_rpc_selected_when_testnet_true():
    kp = Keypair()
    a = SOLAdapter(ExecLedger(":memory:"), bytes(kp.secret()).hex(), testnet=True)
    assert a.rpc_url == DEVNET_RPC
    assert a.testnet is True


def test_mainnet_rpc_is_default():
    a = make_adapter()
    assert a.rpc_url == MAINNET_RPC
    assert a.testnet is False


def test_explicit_rpc_url_is_kept_on_mainnet():
    kp = Keypair()
    a = SOLAdapter(ExecLedger(":memory:"), bytes(kp.secret()).hex(),
                   rpc_url="https://custom.rpc.example")
    assert a.rpc_url == "https://custom.rpc.example"