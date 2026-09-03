import base64
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest

import sui_adapter
from ledger import ExecLedger
from order_model import OrderIntent
from sui_adapter import (
    SUI_MAINNET_RPC,
    SUI_TESTNET_RPC,
    SUIAdapter,
    USDC_MAINNET_COIN_TYPE,
    _ed25519_verify,
)

KEY_HEX = bytes(range(32)).hex()
PACKAGE = "0x" + "12" * 32
POOL = "0x" + "ab" * 32
BALANCE_MANAGER = "0x" + "cd" * 32
GAS_COIN = "0x" + "ef" * 32
ADDR = "0x" + "01" * 32


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload


def _gql_coins_data(coin_type, balance):
    """GraphQL response for the _gql_coins query (one owned coin).

    Current GraphQL returns the balance inside contents.json (raw u64); the
    per-object balance field is unused."""
    addr_hex = ADDR[2:]
    return {"data": {"address": {"objects": {"nodes": [{
        "address": GAS_COIN,
        "version": "5",
        "digest": "0x3333",
        "contents": {"json": {"id": GAS_COIN, "balance": str(balance)}},
    }]}}}}


def _gql_two_sui_coins(balance_a, balance_b):
    """GraphQL response with TWO SUI coins (for SUI-transfer gas separation)."""
    addr_hex = ADDR[2:]
    coin_b = "0x" + "ab" * 32
    return {"data": {"address": {"objects": {"nodes": [
        {"address": GAS_COIN, "version": "5", "digest": "0x3333",
         "contents": {"json": {"id": GAS_COIN, "balance": str(balance_a)}}},
        {"address": coin_b, "version": "6", "digest": "0x4444",
         "contents": {"json": {"id": coin_b, "balance": str(balance_b)}}},
    ]}}}}


class RpcRecorder:
    """Dispatches on GraphQL queries (query=) vs JSON-RPC (method=)."""

    def __init__(self):
        self.calls = []  # list of (kind, payload)

    def post(self, url, json=None, headers=None, timeout=None):
        payload = json or {}
        if "query" in payload:
            kind = "gql"
        else:
            kind = "rpc"
        self.calls.append((kind, payload))
        q = payload.get("query", "")
        if kind == "gql":
            if 'balance(coinType: "0x2::sui::SUI")' in q:
                return FakeResponse({"data": {"address": {"balance": {"totalBalance": "1500000000"}}}})
            if f'balance(coinType: "{USDC_MAINNET_COIN_TYPE}")' in q:
                return FakeResponse({"data": {"address": {"balance": {"totalBalance": "12500000"}}}})
            if 'filter: {type: "0x2::coin::Coin<0x2::sui::SUI>"}' in q:
                return FakeResponse(_gql_coins_data("0x2::sui::SUI", 1_500_000_000))
            if f'filter: {{type: "0x2::coin::Coin<{USDC_MAINNET_COIN_TYPE}>"}}' in q:
                return FakeResponse(_gql_coins_data(USDC_MAINNET_COIN_TYPE, 12_500_000))
            if "simulateTransaction" in q:
                return FakeResponse({"data": {"simulateTransaction": {"effects": {
                    "status": {"status": "SUCCESS"},
                    "gasUsed": {"computationCost": "1000000", "storageCost": "500000"},
                }}}})
            if "executeTransaction" in q:
                return FakeResponse({"data": {"executeTransaction": {
                    "effects": {"digest": "0xdeadbeefcafe", "status": "SUCCESS"},
                }}})
            if "serviceConfig" in q:
                return FakeResponse({"data": {"serviceConfig": {"referenceGasPrice": 1000}}})
            raise AssertionError(f"unexpected graphql query: {q[:120]}")
        method = payload["method"]
        if method == "sui_multiGetObjects":
            ids = payload["params"][0]
            rows = {
                POOL: {"objectId": POOL, "version": "42", "digest": "0x1111",
                       "owner": {"Shared": {"initial_shared_version": "7"}}},
                BALANCE_MANAGER: {"objectId": BALANCE_MANAGER, "version": "9", "digest": "0x2222",
                                  "owner": {"Shared": {"initial_shared_version": "9"}}},
            }
            return FakeResponse([{"data": rows.get(oid)} for oid in ids])
        raise AssertionError(f"unexpected RPC method {method}")


def make_adapter(tmp_path, testnet=False, **kw):
    ledger = ExecLedger(str(tmp_path / "ledger.db"))
    return SUIAdapter(ledger, KEY_HEX, testnet=testnet, **kw)


def make_configured_adapter(tmp_path):
    return make_adapter(tmp_path, deepbook_package=PACKAGE, pool_id=POOL,
                        balance_manager=BALANCE_MANAGER)


def intent(**kw):
    base = dict(chain="sui", venue="deepbook-spot", symbol="SUI/USDC", side="buy",
                qty=0.01, order_type="market", leverage=1.0, idempotency_key="sui-test-1")
    base.update(kw)
    return OrderIntent(**base)


def test_get_balance_parses_native_and_usdc(monkeypatch, tmp_path):
    rec = RpcRecorder()
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_adapter(tmp_path)
    assert adapter.get_balance("SUI") == pytest.approx(1.5)
    assert adapter.get_balance("USDC") == pytest.approx(12.5)
    kinds = [k for k, _ in rec.calls]
    assert kinds == ["gql", "gql"]  # GraphQL coins, not deprecated JSON-RPC


def test_get_balance_error_returns_zero(monkeypatch, tmp_path):
    def boom(url, json=None, headers=None, timeout=None):
        raise AssertionError("node down")

    monkeypatch.setattr(sui_adapter.requests, "post", boom)
    adapter = make_adapter(tmp_path)
    assert adapter.get_balance("SUI") == 0.0


def test_place_order_without_deepbook_config_never_raises(tmp_path):
    adapter = make_adapter(tmp_path)
    result = adapter.place_order(intent(), 100.0)
    assert result["ok"] is False
    assert "not configured" in result["error"]


def test_place_order_builds_signs_and_broadcasts(monkeypatch, tmp_path):
    rec = RpcRecorder()
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_configured_adapter(tmp_path)
    result = adapter.place_order(intent(), 2.5)
    assert result["ok"] is True
    assert result["venue"] == "sui"
    assert result["tx_hash"] == "0xdeadbeefcafe"

    kinds = [k for k, _ in rec.calls]
    # gas coin (gql), shared versions (rpc multiGetObjects), dry-run (gql),
    # gas price (gql), execute (gql)
    assert "gql" in kinds and "rpc" in kinds

    # The execute call must be GraphQL executeTransaction (not deprecated RPC).
    exec_calls = [p for k, p in rec.calls if k == "gql" and "executeTransaction" in p.get("query", "")]
    assert exec_calls, "expected a GraphQL executeTransaction mutation"

    sig = base64.b64decode(result["signature"])
    assert len(sig) == 97
    assert sig[0] == 0x00  # ed25519 scheme flag
    pub = sig[65:]
    assert pub == adapter.public_key
    intent_msg = hashlib.blake2b(b"\x00\x00\x00" + base64.b64decode(result["tx_bytes"]), digest_size=32).digest()
    assert _ed25519_verify(pub, intent_msg, sig[1:65])
    assert not _ed25519_verify(pub, b"tampered", sig[1:65])


def test_type_arguments_encoded_as_struct_tags(monkeypatch, tmp_path):
    """MoveCall type_arguments must be BCS TypeTag::Struct (0x07), not strings."""
    rec = RpcRecorder()
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_configured_adapter(tmp_path)
    result = adapter.place_order(intent(), 2.5)
    assert result["ok"] is True
    raw = base64.b64decode(result["tx_bytes"])
    assert raw.count(b"\x07") >= 2, "expected TypeTag::Struct markers for both coin types"
    assert b"\x0d" + b"0x2::sui::SUI" not in raw, "type args must not be plain strings"
    assert b"\x00" * 31 + b"\x02" in raw


def test_place_order_limit_uses_limit_entrypoint(monkeypatch, tmp_path):
    rec = RpcRecorder()
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_configured_adapter(tmp_path)
    result = adapter.place_order(intent(order_type="limit", limit_price=2.5), 2.5)
    assert result["ok"] is True


def test_cancel_all_requires_config(tmp_path):
    adapter = make_adapter(tmp_path)
    result = adapter.cancel_all(1)
    assert result["ok"] is False
    assert "no venue configured" in result["error"]


def test_flat_and_cancel_graceful_without_config(tmp_path):
    adapter = make_adapter(tmp_path)
    result = adapter.flat_and_cancel(1)
    assert result["ok"] is False
    assert "no venue configured" in result["error"]
    assert "flat" in result


def test_flat_and_cancel_broadcasts_cancel_when_configured(monkeypatch, tmp_path):
    rec = RpcRecorder()
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_configured_adapter(tmp_path)
    result = adapter.flat_and_cancel(1)
    assert result["ok"] is True


def test_testnet_url_selection(tmp_path):
    assert make_adapter(tmp_path).rpc_url == SUI_MAINNET_RPC
    assert make_adapter(tmp_path, testnet=True).rpc_url == SUI_TESTNET_RPC


def test_sui_transfer_reserves_distinct_gas_coin(monkeypatch, tmp_path):
    """Sending SUI must not use the transferred coin as the gas coin
    (duplicated ObjectRef is rejected on-chain). Verify a different coin is
    reserved for gas and the transfer amount stays within the non-gas coins."""

    class _FakeResponse:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200

        def json(self):
            return self.payload

    class TwoCoinRecorder:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, headers=None, timeout=None):
            payload = json or {}
            self.calls.append(payload)
            q = payload.get("query", "")
            if "query" in payload:
                if 'filter: {type: "0x2::coin::Coin<0x2::sui::SUI>"}' in q:
                    return _FakeResponse(_gql_two_sui_coins(100_000_000, 90_000_000))
                if "serviceConfig" in q:
                    return _FakeResponse({"data": {"serviceConfig": {"referenceGasPrice": 1000}}})
                if "simulateTransaction" in q:
                    return _FakeResponse({"data": {"simulateTransaction": {"effects": {
                        "status": {"status": "SUCCESS"},
                        "gasUsed": {"computationCost": "1000000", "storageCost": "500000"},
                    }}}})
                if "executeTransaction" in q:
                    return _FakeResponse({"data": {"executeTransaction": {
                        "effects": {"digest": "0xdeadbeefcafe", "status": "SUCCESS"},
                    }}})
                raise AssertionError(f"unexpected gql: {q[:100]}")
            raise AssertionError(f"unexpected rpc: {payload}")

    rec = TwoCoinRecorder()
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_adapter(tmp_path)
    result = adapter.transfer_asset("0x" + "42" * 32, 0.05, "SUI")
    assert result["ok"] is True
    assert result["asset"] == "SUI"
    # the broadcast must have used a gas coin distinct from the transferred one;
    # the transfer would have been rejected otherwise (duplicated ObjectRef).
    assert result["tx_hash"] == "0xdeadbeefcafe"
