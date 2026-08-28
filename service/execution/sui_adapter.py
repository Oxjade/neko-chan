"""Sui real-trading adapter: DeepBookV3 spot/margin via the Sui JSON-RPC.

v1 scope: balance reads (SUI + USDC), stub position reads, spot order
build + dry-run + sign + broadcast, and a best-effort cancel for the
killswitch hook. DeepBook package/pool/balance-manager addresses are
build-phase research items and arrive via constructor params; until they
are set, order paths return {"ok": False, "error": "...not configured"}.

Transaction flow: build a ProgrammableTransaction JSON (move call into
deepbook::deepbook), dry-run it via sui_dryRunTransactionBlock, resolve
shared-object versions + a gas coin, serialize the full TransactionData
to BCS ourselves (minimal pure-python BCS below), sign the intent hash
blake2b256([0,0,0] || tx_bytes) with Ed25519 (pysui when available, else
a pure-python fallback), and broadcast via sui_executeTransactionBlock.
"""

import base64
import hashlib
import logging
import time

import requests

from ledger import ExecLedger
from order_model import OrderIntent

log = logging.getLogger("execution")

SUI_MAINNET_RPC = "https://fullnode.mainnet.sui.io:443"
SUI_TESTNET_RPC = "https://fullnode.testnet.sui.io:443"

SUI_COIN_TYPE = "0x2::sui::SUI"

# Circle-issued USDC on Sui mainnet (well-known coin type). Pass usdc_coin_type
# explicitly for testnet or for the newer 0x...::usdc::USDC deployment.
USDC_MAINNET_COIN_TYPE = (
    "0x5d4b302506645c37ff133b98c4b50a5ae14841659738d6d733d59d0d217a93bf::coin::COIN"
)

DEEPBOOK_MODULE = "deepbook"
DEEPBOOK_DECIMALS = 6  # DeepBook prices/quantities are u64 with 6 decimals
GAS_MIN_BALANCE = 1_000_000_000  # require at least 1 SUI for gas in v1
RPC_TIMEOUT = 20


class SuiRpcError(Exception):
    def __init__(self, method: str, message: str, status: int | None = None):
        super().__init__(f"{method}: {message}")
        self.method = method
        self.message = message
        self.status = status


# ---------------------------------------------------------------- BCS
# Minimal pure-python BCS encoding for the subset Sui transactions need.

def _uleb128(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _bcs_u8(v: int) -> bytes:
    return bytes([v & 0xFF])


def _bcs_u16(v: int) -> bytes:
    return _uleb128(v & 0xFFFF)


def _bcs_u64(v: int) -> bytes:
    return (v & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")


def _bcs_bool(v: bool) -> bytes:
    return b"\x01" if v else b"\x00"


def _bcs_str(s: str) -> bytes:
    b = s.encode()
    return _uleb128(len(b)) + b


def _bcs_addr(a: str) -> bytes:
    b = bytes.fromhex(a[2:] if a.startswith("0x") else a)
    if len(b) != 32:
        raise ValueError(f"not a 32-byte Sui address: {a}")
    return b


def _bcs_addr_padded(a: str) -> bytes:
    raw = a[2:] if a.startswith("0x") else a
    if len(raw) % 2:
        raw = "0" + raw
    return bytes.fromhex(raw).rjust(32, b"\x00")


def _bcs_bytes(b: bytes) -> bytes:
    return _uleb128(len(b)) + b


def _bcs_vec(items: list[bytes]) -> bytes:
    return _uleb128(len(items)) + b"".join(items)


def _bcs_object_ref(object_id: str, version: int, digest: str) -> bytes:
    digest_hex = digest[2:] if digest.startswith("0x") else digest
    return _bcs_addr(object_id) + _bcs_u64(version) + _bcs_bytes(bytes.fromhex(digest_hex))


def _bcs_call_arg_shared(object_id: str, initial_shared_version: int, mutable: bool) -> bytes:
    # CallArg::Object(ObjectArg::SharedObject)
    return b"\x01" + b"\x01" + _bcs_addr(object_id) + _bcs_u64(initial_shared_version) + _bcs_bool(mutable)


def _bcs_call_arg_pure(value_bytes: bytes) -> bytes:
    # CallArg::Pure(Vector<u8>)
    return b"\x00" + _bcs_bytes(value_bytes)


def _bcs_argument_input(n: int) -> bytes:
    # Argument::Input(u16)
    return b"\x01" + _bcs_u16(n)


def _bcs_command_move_call(command: dict, arguments: list[bytes]) -> bytes:
    # Command::MoveCall
    return (
        b"\x00"
        + _bcs_addr(command["package"])
        + _bcs_str(command["module"])
        + _bcs_str(command["function"])
        + _bcs_vec([_bcs_type_tag(t) for t in command["type_arguments"]])
        + _bcs_vec(arguments)
    )


def _bcs_programmable(inputs: list[bytes], commands: list[bytes]) -> bytes:
    return _bcs_vec(inputs) + _bcs_vec(commands)


def _bcs_type_tag(tag: str) -> bytes:
    tag = tag.strip()
    primitives = {
        "bool": 0, "u8": 1, "u64": 2, "u128": 3, "address": 4,
        "signer": 5, "u16": 8, "u32": 9, "u256": 10,
    }
    if tag in primitives:
        return bytes([primitives[tag]])
    if tag.startswith("vector<") and tag.endswith(">"):
        inner = tag[7:-1].strip()
        return b"\x06" + _bcs_type_tag(inner)
    if "::" not in tag:
        raise ValueError(f"invalid type tag: {tag}")
    idx = tag.index("::")
    addr = tag[:idx]
    rest = tag[idx + 2:]
    idx2 = rest.index("::")
    module = rest[:idx2]
    name_and_params = rest[idx2 + 2:]
    if "<" in name_and_params:
        name, params_str = name_and_params.split("<", 1)
        if not params_str.endswith(">"):
            raise ValueError(f"unmatched < in type tag: {tag}")
        params_str = params_str[:-1]
        params = []
        depth = 0
        current = []
        for ch in params_str:
            if ch == "<":
                depth += 1
                current.append(ch)
            elif ch == ">":
                depth -= 1
                current.append(ch)
            elif ch == "," and depth == 0:
                params.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        if current:
            params.append("".join(current).strip())
        type_params = [_bcs_type_tag(p) for p in params]
    else:
        name = name_and_params
        type_params = []
    return b"\x07" + _bcs_addr_padded(addr) + _bcs_str(module) + _bcs_str(name) + _bcs_vec(type_params)


def _serialize_tx_data_v1(call_args: list[dict], command: dict, sender: str,
                          gas_coin: dict, gas_price: int, budget: int,
                          shared_versions: dict) -> bytes:
    inputs = []
    for arg in call_args:
        if arg["kind"] == "shared":
            info = shared_versions.get(arg["object_id"])
            if not info:
                raise SuiRpcError(
                    "sui_multiGetObjects",
                    f"no shared object info for {arg['object_id']}",
                )
            inputs.append(_bcs_call_arg_shared(
                arg["object_id"], info["initial_shared_version"], arg["mutable"]))
        else:
            inputs.append(_bcs_call_arg_pure(arg["bytes"]))
    arguments = [_bcs_argument_input(i) for i in range(len(inputs))]
    # TransactionKind::ProgrammableTransaction
    kind = b"\x00" + _bcs_programmable(inputs, [_bcs_command_move_call(command, arguments)])
    gas_data = (
        _bcs_vec([_bcs_object_ref(gas_coin["objectId"], gas_coin["version"], gas_coin["digest"])])
        + _bcs_addr(sender)
        + _bcs_u64(gas_price)
        + _bcs_u64(budget)
    )
    # TransactionData::V1 { kind, sender, gas_data, expiration: None }
    return b"\x00" + kind + _bcs_addr(sender) + gas_data + b"\x00"


def _json_shared_input(object_id: str, mutable: bool) -> dict:
    return {"Object": {"SharedObject": {"objectId": object_id, "mutable": mutable}}}


def _json_pure_input(value_bytes: bytes) -> dict:
    return {"Pure": "0x" + value_bytes.hex()}


def _json_kind(call_args: list[dict], command: dict) -> dict:
    inputs = []
    for arg in call_args:
        if arg["kind"] == "shared":
            inputs.append(_json_shared_input(arg["object_id"], arg["mutable"]))
        else:
            inputs.append(_json_pure_input(arg["bytes"]))
    arguments = [{"Input": i} for i in range(len(inputs))]
    transactions = [{
        "MoveCall": {
            "package": command["package"],
            "module": command["module"],
            "function": command["function"],
            "type_arguments": command["type_arguments"],
            "arguments": arguments,
        }
    }]
    return {"kind": "ProgrammableTransaction", "inputs": inputs, "transactions": transactions}


# ---------------------------------------------------------------- Ed25519
# Minimal pure-python Ed25519 (RFC 8032) sign/verify fallback used only when
# pysui is unavailable.

_ED_Q = 2**255 - 19
_ED_L = 2**252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_Q - 2, _ED_Q) % _ED_Q
_ED_I = pow(2, (_ED_Q - 1) // 4, _ED_Q)
_ED_B = (
    15112221349535400772501151409588531511454012693041857206046113283949847762202,
    46316835694926478169428394003475163141307993866256225615783033603165251855960,
)


def _ed_point_add(p: tuple, q: tuple) -> tuple:
    x1, y1 = p
    x2, y2 = q
    den_x = (1 + _ED_D * x1 * x2 * y1 * y2) % _ED_Q
    den_y = (1 - _ED_D * x1 * x2 * y1 * y2) % _ED_Q
    inv_x = pow(den_x, _ED_Q - 2, _ED_Q)
    inv_y = pow(den_y, _ED_Q - 2, _ED_Q)
    return (
        (x1 * y2 + y1 * x2) * inv_x % _ED_Q,
        (y1 * y2 + x1 * x2) * inv_y % _ED_Q,
    )


def _ed_scalarmult(p: tuple, n: int) -> tuple:
    acc = (0, 1)
    while n:
        if n & 1:
            acc = _ed_point_add(acc, p)
        p = _ed_point_add(p, p)
        n >>= 1
    return acc


def _ed_encode(p: tuple) -> bytes:
    x, y = p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _ed_decompress(encoded: bytes) -> tuple | None:
    if len(encoded) != 32:
        return None
    y = int.from_bytes(encoded, "little")
    x_sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _ED_Q:
        return None
    x2 = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_Q - 2, _ED_Q) % _ED_Q
    x = pow(x2, (_ED_Q + 3) // 8, _ED_Q)
    if (x * x - x2) % _ED_Q != 0:
        x = x * _ED_I % _ED_Q
    if (x * x - x2) % _ED_Q != 0:
        return None
    if (x & 1) != x_sign:
        x = _ED_Q - x
    return x, y


def _ed25519_pubkey(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _ed_encode(_ed_scalarmult(_ED_B, a))


def _ed25519_sign(seed: bytes, message: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % _ED_L
    r_bytes = _ed_encode(_ed_scalarmult(_ED_B, r))
    pub = _ed25519_pubkey(seed)
    k = int.from_bytes(hashlib.sha512(r_bytes + pub + message).digest(), "little") % _ED_L
    s = (r + k * a) % _ED_L
    return r_bytes + s.to_bytes(32, "little")


def _ed25519_verify(pub: bytes, message: bytes, signature: bytes) -> bool:
    if len(pub) != 32 or len(signature) != 64:
        return False
    point_a = _ed_decompress(pub)
    point_r = _ed_decompress(signature[:32])
    if point_a is None or point_r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _ED_L:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + pub + message).digest(), "little") % _ED_L
    lhs = _ed_scalarmult(_ED_B, s)
    rhs = _ed_point_add(point_r, _ed_scalarmult(point_a, k % _ED_L))
    return lhs == rhs


# ---------------------------------------------------------------- Adapter

class SUIAdapter:
    def __init__(self, ledger: ExecLedger, keypair_hex: str,
                 rpc_url: str = SUI_MAINNET_RPC, testnet: bool = False,
                 usdc_coin_type: str = USDC_MAINNET_COIN_TYPE,
                 deepbook_package: str | None = None,
                 pool_id: str | None = None,
                 balance_manager: str | None = None,
                 pool_coin_types: list[str] | None = None):
        self.ledger = ledger
        self.testnet = testnet
        self.rpc_url = SUI_TESTNET_RPC if testnet else rpc_url
        self.usdc_coin_type = usdc_coin_type
        self.deepbook_package = deepbook_package
        self.pool_id = pool_id
        self.balance_manager = balance_manager
        self.pool_coin_types = pool_coin_types or [SUI_COIN_TYPE, usdc_coin_type]
        self._shared_cache: dict = {}
        self._rpc_seq = 0

        self._seed: bytes | None = None
        self._kp = None  # pysui keypair when available
        self._pub: bytes | None = None
        self.address = ""
        self._sign_error = ""
        try:
            hexed = keypair_hex[2:] if keypair_hex.startswith("0x") else keypair_hex
            self._seed = bytes.fromhex(hexed)
            if len(self._seed) != 32:
                raise ValueError(f"keypair_hex must decode to 32 bytes, got {len(self._seed)}")
            try:
                from pysui.sui.sui_crypto import SuiKeyPair
                self._kp = SuiKeyPair.from_b64(base64.b64encode(b"\x00" + self._seed).decode())
                self._pub = bytes(self._kp.public_key.key_bytes)
            except ImportError:
                self._pub = _ed25519_pubkey(self._seed)
            if len(self._pub) != 32:
                raise ValueError(f"unexpected public key length {len(self._pub)}")
            self.address = "0x" + hashlib.blake2b(b"\x00" + self._pub, digest_size=32).hexdigest()
        except Exception as exc:
            log.error("sui keypair init failed: %s", exc)
            self._sign_error = f"invalid keypair_hex: {exc}"

    @property
    def public_key(self) -> bytes:
        return self._pub or b""

    # ------------------------------------------------------------ RPC

    def _rpc(self, method: str, params: list) -> dict:
        body = {"jsonrpc": "2.0", "id": self._rpc_seq, "method": method, "params": params}
        self._rpc_seq += 1
        resp = None
        for attempt in range(2):
            try:
                resp = requests.post(
                    self.rpc_url, json=body,
                    headers={"Content-Type": "application/json"}, timeout=RPC_TIMEOUT,
                )
            except requests.RequestException as exc:
                if attempt == 0:
                    log.warning("RPC %s transport error, retrying once: %s", method, exc)
                    continue
                raise SuiRpcError(method, f"transport error: {exc}") from exc
            if resp.status_code >= 500 and attempt == 0:
                log.warning("RPC %s HTTP %s, retrying once", method, resp.status_code)
                continue
            break
        if resp is None:
            raise SuiRpcError(method, "no response")
        if resp.status_code >= 400:
            raise SuiRpcError(method, f"HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SuiRpcError(method, f"invalid JSON response: {exc}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise SuiRpcError(method, msg)
        return payload.get("result") if isinstance(payload, dict) else payload

    # ------------------------------------------------------------ balances / positions

    def _coin_spec(self, asset: str) -> tuple[str, int]:
        if asset == "SUI":
            return SUI_COIN_TYPE, 9
        if asset == "USDC":
            return self.usdc_coin_type, 6
        raise ValueError(f"unsupported asset for sui chain: {asset}")

    def get_balance(self, asset: str) -> float:
        try:
            coin_type, decimals = self._coin_spec(asset)
            res = self._rpc("suix_getBalance", [self.address, coin_type])
            total = int(res.get("totalBalance", 0))
            return total / (10**decimals)
        except Exception:  # noqa: BLE001
            log.exception("get_balance failed for %s", asset)
            return 0.0

    def get_positions(self) -> list[dict]:
        # v1 stub: DeepBook margin positions live on-chain in the balance
        # manager; indexing them is a build-phase research item.
        return []

    # ------------------------------------------------------------ orders

    def _shared_versions(self) -> dict:
        if len(self._shared_cache) == 2:
            return self._shared_cache
        ids = [oid for oid in (self.pool_id, self.balance_manager) if oid]
        res = self._rpc("sui_multiGetObjects", [
            ids,
            {"showOwner": True, "showContent": False, "showType": False, "showDisplay": False},
        ])
        cache = {}
        for item in res or []:
            data = item.get("data") if isinstance(item, dict) else None
            if not data:
                continue
            shared = (data.get("owner") or {}).get("Shared") or {}
            cache[data["objectId"]] = {
                "initial_shared_version": int(shared.get("initial_shared_version", 0)),
                "version": int(data.get("version", 0)),
            }
        self._shared_cache = cache
        return cache

    def _pick_gas_coin(self) -> dict:
        res = self._rpc("suix_getCoins", [self.address, SUI_COIN_TYPE, None, 10])
        for coin in (res or {}).get("data") or []:
            if int(coin.get("balance", 0)) >= GAS_MIN_BALANCE:
                return {
                    "objectId": coin["coinObjectId"],
                    "version": int(coin["version"]),
                    "digest": coin["digest"],
                }
        raise SuiRpcError("suix_getCoins", "no SUI gas coin with >= 1 SUI balance")

    def _dry_run(self, tx_json: dict) -> dict:
        dry = self._rpc("sui_dryRunTransactionBlock", [self.address, tx_json])
        if dry.get("errors"):
            raise SuiRpcError("sui_dryRunTransactionBlock", "; ".join(dry["errors"]))
        effects = dry.get("effects") or {}
        status = effects.get("status") or {}
        if status.get("status") == "failure":
            raise SuiRpcError(
                "sui_dryRunTransactionBlock",
                "dry run failure: " + str(status.get("error", "unknown")),
            )
        gas = effects.get("gasUsed") or {}
        budget = int(gas.get("computationCost", 0)) + int(gas.get("storageCost", 0))
        budget = max(budget + budget // 2, 500_000)
        gas_price = int(self._rpc("suix_getReferenceGasPrice", []))
        return {"gas_price": gas_price, "budget": budget}

    def _broadcast(self, call_args: list[dict], command: dict,
                   gas_price: int, budget: int) -> dict:
        tx_bytes = _serialize_tx_data_v1(
            call_args, command, self.address,
            self._pick_gas_coin(), gas_price, budget, self._shared_versions(),
        )
        signature = self._sign(tx_bytes)
        tx_b64 = base64.b64encode(tx_bytes).decode()
        res = self._rpc("sui_executeTransactionBlock", [
            tx_b64, [signature], {"showEffects": True},
        ])
        digest = res.get("digest") or ""
        return {"tx_bytes": tx_b64, "signature": signature, "digest": digest}

    def _sign(self, tx_bytes: bytes) -> str:
        if self._kp is not None:
            return self._kp.new_sign_secure(base64.b64encode(tx_bytes).decode())
        if self._seed is None or self._pub is None:
            raise SuiRpcError("sign", "no usable keypair")
        intent_msg = hashlib.blake2b(b"\x00\x00\x00" + tx_bytes, digest_size=32).digest()
        sig = _ed25519_sign(self._seed, intent_msg)
        return base64.b64encode(b"\x00" + sig + self._pub).decode()

    def _order_call_args(self, intent: OrderIntent, ref_price: float) -> dict | None:
        if not (self.deepbook_package and self.pool_id and self.balance_manager):
            return None
        if intent.venue == "deepbook-margin":
            return {"error": "deepbook-margin not supported in v1 (spot only)"}
        if intent.order_type not in ("market", "limit"):
            return {"error": f"order_type {intent.order_type} not supported on DeepBook v1 (market|limit)"}
        scale = 10**DEEPBOOK_DECIMALS
        price = 0 if intent.order_type == "market" else int(round(ref_price * scale))
        quantity = int(round(intent.qty * scale))
        client_order_id = int.from_bytes(
            hashlib.sha256(intent.idempotency_key.encode()).digest()[:8], "little"
        )
        function = "place_market_order" if intent.order_type == "market" else "place_limit_order"
        return {
            "function": function,
            "client_order_id": client_order_id,
            "price": price,
            "quantity": quantity,
            "call_args": [
                {"kind": "shared", "object_id": self.pool_id, "mutable": True},
                {"kind": "shared", "object_id": self.balance_manager, "mutable": True},
                {"kind": "pure", "bytes": _bcs_u64(client_order_id)},
                {"kind": "pure", "bytes": _bcs_u64(price)},
                {"kind": "pure", "bytes": _bcs_u64(quantity)},
                {"kind": "pure", "bytes": _bcs_u8(0)},  # self_matching_prevention: none
                {"kind": "pure", "bytes": _bcs_u64(0)},  # expiration_timestamp: none
            ],
        }

    def build_spot_order_tx(self, intent: OrderIntent, ref_price: float) -> dict:
        if not (self.deepbook_package and self.pool_id and self.balance_manager):
            return {
                "ok": False,
                "error": "DeepBook package/pool not configured (need deepbook_package, pool_id, balance_manager)",
            }
        if self._sign_error:
            return {"ok": False, "error": self._sign_error}
        built = self._order_call_args(intent, ref_price)
        if built is None:
            return {
                "ok": False,
                "error": "DeepBook package/pool not configured (need deepbook_package, pool_id, balance_manager)",
            }
        if "error" in built:
            return {"ok": False, "error": built["error"]}
        command = {
            "package": self.deepbook_package,
            "module": DEEPBOOK_MODULE,
            "function": built["function"],
            "type_arguments": self.pool_coin_types,
        }
        return {
            "ok": True,
            "sender": self.address,
            "function": built["function"],
            "client_order_id": built["client_order_id"],
            "price": built["price"],
            "quantity": built["quantity"],
            "call_args": built["call_args"],
            "command": command,
            "transaction_json": _json_kind(built["call_args"], command),
        }

    def place_order(self, intent: OrderIntent, ref_price: float) -> dict:
        if self._sign_error:
            return {"ok": False, "error": self._sign_error}
        errors = intent.validate(ref_price)
        if errors:
            return {"ok": False, "error": "; ".join(errors)}
        try:
            built = self.build_spot_order_tx(intent, ref_price)
            if not built.get("ok"):
                return built
            gas = self._dry_run(built["transaction_json"])
            out = self._broadcast(built["call_args"], built["command"], gas["gas_price"], gas["budget"])
            return {
                "ok": True,
                "venue": "sui",
                "venue_order_id": out["digest"],
                "tx_hash": out["digest"],
                "symbol": intent.symbol,
                "side": intent.side,
                "qty": intent.qty,
                "price": ref_price,
                "tx_bytes": out["tx_bytes"],
                "signature": out["signature"],
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("place_order failed for %s", intent.idempotency_key)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    # ------------------------------------------------------------ killswitch hooks

    def cancel_all(self, bot_id) -> dict:
        if not (self.deepbook_package and self.pool_id and self.balance_manager):
            return {
                "ok": False,
                "error": "DeepBook package/pool not configured; cannot cancel (killswitch needs balance_manager configured)",
            }
        try:
            call_args = [
                {"kind": "shared", "object_id": self.pool_id, "mutable": True},
                {"kind": "shared", "object_id": self.balance_manager, "mutable": True},
            ]
            command = {
                "package": self.deepbook_package,
                "module": DEEPBOOK_MODULE,
                "function": "cancel_all_orders",
                "type_arguments": self.pool_coin_types,
            }
            gas = self._dry_run(_json_kind(call_args, command))
            out = self._broadcast(call_args, command, gas["gas_price"], gas["budget"])
            return {"ok": True, "tx_hash": out["digest"], "cancelled": "best-effort cancel_all_orders"}
        except Exception as exc:  # noqa: BLE001
            log.exception("cancel_all failed for bot %s", bot_id)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    def flat_and_cancel(self, bot_id) -> dict:
        # Killswitch hook. v1: positions are a stub ([]), so flattening is a
        # no-op; the critical part is cancelling open orders on the pool.
        result = self.cancel_all(bot_id)
        result["flat"] = "v1 stub: no margin positions indexed"
        return result