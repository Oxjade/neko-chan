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


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload


class RpcRecorder:
    def __init__(self, canned):
        self.canned = canned
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        method = json["method"]
        handler = self.canned.get(method)
        if handler is None:
            raise AssertionError(f"unexpected RPC method {method} (payload {json})")
        result = handler(json["params"]) if callable(handler) else handler
        return FakeResponse({"jsonrpc": "2.0", "id": json.get("id", 1), "result": result})


def canned_rpc():
    def balance(params):
        coin = params[1]
        if coin == "0x2::sui::SUI":
            return {"coinType": coin, "coinObjectCount": 2,
                    "totalBalance": "1500000000", "lockedBalance": {"value": "0"}}
        return {"coinType": coin, "coinObjectCount": 1,
                "totalBalance": "12500000", "lockedBalance": {"value": "0"}}

    def multi(params):
        ids, _opts = params
        rows = {
            POOL: {"objectId": POOL, "version": "42", "digest": "0x1111",
                   "owner": {"Shared": {"initial_shared_version": "7"}}},
            BALANCE_MANAGER: {"objectId": BALANCE_MANAGER, "version": "9", "digest": "0x2222",
                              "owner": {"Shared": {"initial_shared_version": "9"}}},
        }
        return [{"data": rows.get(oid)} for oid in ids]

    def coins(params):
        return {"data": [{"coinObjectId": GAS_COIN, "version": "5",
                          "digest": "0x3333", "balance": "9999999999"}],
                "nextCursor": None, "hasNextPage": False}

    def dryrun(params):
        return {"effects": {"status": {"status": "success"},
                            "gasUsed": {"computationCost": "1000000",
                                        "storageCost": "500000", "storageRebate": "0"}}}

    def execute(params):
        return {"digest": "0xdeadbeefcafe", "effects": {"status": {"status": "success"}}}

    return {
        "suix_getBalance": balance,
        "sui_multiGetObjects": multi,
        "suix_getCoins": coins,
        "suix_getReferenceGasPrice": "1000",
        "sui_dryRunTransactionBlock": dryrun,
        "sui_executeTransactionBlock": execute,
    }


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
    rec = RpcRecorder(canned_rpc())
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_adapter(tmp_path)
    assert adapter.get_balance("SUI") == pytest.approx(1.5)
    assert adapter.get_balance("USDC") == pytest.approx(12.5)
    methods = [c["method"] for c in rec.calls]
    assert methods == ["suix_getBalance", "suix_getBalance"]
    assert rec.calls[0]["params"][1] == "0x2::sui::SUI"
    assert rec.calls[1]["params"][1] == USDC_MAINNET_COIN_TYPE


def test_get_balance_error_returns_zero(monkeypatch, tmp_path):
    def boom(params):
        raise AssertionError("node down")

    rec = RpcRecorder({"suix_getBalance": boom})
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_adapter(tmp_path)
    assert adapter.get_balance("SUI") == 0.0


def test_place_order_without_deepbook_config_never_raises(tmp_path):
    adapter = make_adapter(tmp_path)
    result = adapter.place_order(intent(), 100.0)
    assert result["ok"] is False
    assert "not configured" in result["error"]


def test_place_order_builds_signs_and_broadcasts(monkeypatch, tmp_path):
    rec = RpcRecorder(canned_rpc())
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_configured_adapter(tmp_path)
    result = adapter.place_order(intent(), 2.5)
    assert result["ok"] is True
    assert result["venue"] == "sui"
    assert result["tx_hash"] == "0xdeadbeefcafe"

    methods = [c["method"] for c in rec.calls]
    assert methods == [
        "sui_dryRunTransactionBlock",
        "suix_getReferenceGasPrice",
        "suix_getCoins",
        "sui_multiGetObjects",
        "sui_executeTransactionBlock",
    ]

    dry_json = rec.calls[0]["params"][1]
    assert dry_json["kind"] == "ProgrammableTransaction"
    move = dry_json["transactions"][0]["MoveCall"]
    assert move["package"] == PACKAGE
    assert move["module"] == "deepbook"
    assert move["function"] == "place_market_order"
    assert len(move["arguments"]) == 7
    assert len(dry_json["inputs"]) == 7
    assert dry_json["inputs"][0]["Object"]["SharedObject"]["objectId"] == POOL
    assert dry_json["inputs"][1]["Object"]["SharedObject"]["objectId"] == BALANCE_MANAGER

    execute_call = rec.calls[-1]
    assert execute_call["method"] == "sui_executeTransactionBlock"
    tx_bytes, signatures, _options = execute_call["params"]
    assert tx_bytes == result["tx_bytes"]
    assert signatures == [result["signature"]]

    raw = base64.b64decode(tx_bytes)
    assert raw[0] == 0x00 and raw[1] == 0x00  # TransactionData::V1, ProgrammableTransaction
    assert bytes.fromhex(POOL[2:]) in raw
    assert bytes.fromhex(BALANCE_MANAGER[2:]) in raw

    sig = base64.b64decode(result["signature"])
    assert len(sig) == 97
    assert sig[0] == 0x00  # ed25519 scheme flag
    pub = sig[65:]
    assert pub == adapter.public_key
    intent_msg = hashlib.blake2b(b"\x00\x00\x00" + raw, digest_size=32).digest()
    assert _ed25519_verify(pub, intent_msg, sig[1:65])
    assert not _ed25519_verify(pub, b"tampered", sig[1:65])


def test_type_arguments_encoded_as_struct_tags(monkeypatch, tmp_path):
    """MoveCall type_arguments must be BCS TypeTag::Struct (0x07), not strings."""
    rec = RpcRecorder(canned_rpc())
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_configured_adapter(tmp_path)
    result = adapter.place_order(intent(), 2.5)
    assert result["ok"] is True
    tx_bytes, _signatures, _opts = rec.calls[-1]["params"]
    raw = base64.b64decode(tx_bytes)
    # Every coin type (0x2::sui::SUI, USDC) must appear as a TypeTag::Struct (0x07),
    # NOT as a bare length-prefixed string (0x0d == len 13 of "0x2::sui::SUI").
    assert raw.count(b"\x07") >= 2, "expected TypeTag::Struct markers for both coin types"
    assert b"\x0d" + b"0x2::sui::SUI" not in raw, "type args must not be plain strings"
    # Padded address for 0x2 must be present (00..00 02).
    assert b"\x00" * 31 + b"\x02" in raw


def test_place_order_limit_uses_limit_entrypoint(monkeypatch, tmp_path):
    rec = RpcRecorder(canned_rpc())
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_configured_adapter(tmp_path)
    result = adapter.place_order(intent(order_type="limit", limit_price=2.5), 2.5)
    assert result["ok"] is True
    move = rec.calls[0]["params"][1]["transactions"][0]["MoveCall"]
    assert move["function"] == "place_limit_order"


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
    rec = RpcRecorder(canned_rpc())
    monkeypatch.setattr(sui_adapter.requests, "post", rec.post)
    adapter = make_configured_adapter(tmp_path)
    result = adapter.flat_and_cancel(1)
    assert result["ok"] is True
    move = rec.calls[0]["params"][1]["transactions"][0]["MoveCall"]
    assert move["function"] == "cancel_all_orders"
    assert rec.calls[-1]["method"] == "sui_executeTransactionBlock"


def test_testnet_url_selection(tmp_path):
    assert make_adapter(tmp_path).rpc_url == SUI_MAINNET_RPC
    assert make_adapter(tmp_path, testnet=True).rpc_url == SUI_TESTNET_RPC


def test_rpc_retries_once_on_5xx(monkeypatch, tmp_path):
    calls = []

    def flaky(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return FakeResponse({"error": {"code": -32000, "message": "node busy"}},
                            status_code=503)

    monkeypatch.setattr(sui_adapter.requests, "post", flaky)
    adapter = make_adapter(tmp_path)
    assert adapter.get_balance("SUI") == 0.0
    assert len(calls) == 2  # retried exactly once, then gave up gracefully