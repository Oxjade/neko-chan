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
import json
import logging
import time

import requests

from ledger import ExecLedger
from order_model import OrderIntent

log = logging.getLogger("execution")

SUI_MAINNET_RPC = "https://fullnode.mainnet.sui.io:443"
SUI_TESTNET_RPC = "https://fullnode.testnet.sui.io:443"
# Blockvision retains suix_getCoins (fullnode.* deprecated it). Used as fallback
# for coin-object queries when GraphQL `objects` returns empty (mainnet SUI).
SUI_MAINNET_RPC_FALLBACK = "https://sui-mainnet-endpoint.blockvision.org:443"

SUI_COIN_TYPE = "0x2::sui::SUI"

# Circle-issued USDC on Sui mainnet (well-known coin type). Pass usdc_coin_type
# explicitly for testnet or for the newer 0x...::usdc::USDC deployment.
USDC_MAINNET_COIN_TYPE = (
    "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
)
# Testnet USDC = the Aftermath testnet settleId (only this type can fund the
# Aftermath perp account; the generic faucet USDC is a different token).
USDC_TESTNET_COIN_TYPE = (
    "0xcdd397f2cffb7f5d439f56fc01afe5585c5f06e3bcd2ee3a21753c566de313d9::usdc::USDC"
)

DEEPBOOK_MODULE = "deepbook"
DEEPBOOK_DECIMALS = 6  # DeepBook prices/quantities are u64 with 6 decimals
# Aftermath perp prices on Sui are raw float from the CCXT API.
AFTERMATH_PRICE_SCALE = 1.0
GAS_MIN_BALANCE = 5_000_000  # require at least 0.005 SUI for gas (mainnet sends often have ~0.06 SUI)
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
    """BCS u16: fixed 2-byte little-endian (NOT uleb128)."""
    return (v & 0xFFFF).to_bytes(2, "little")


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


def _bcs_call_arg_imm_or_owned(object_id: str, version: int, digest: str) -> bytes:
    # CallArg::Object(ObjectArg::ImmOrOwnedObject { address, version, digest })
    return b"\x01" + b"\x00" + _bcs_object_ref(object_id, version, digest)


def _bcs_call_arg_pure(value_bytes: bytes) -> bytes:
    # CallArg::Pure(Vector<u8>)
    return b"\x00" + _bcs_bytes(value_bytes)


def _bcs_argument_input(n: int) -> bytes:
    # Argument::Input(u16)
    return b"\x01" + _bcs_u16(n)


def _bcs_argument_result(n: int) -> bytes:
    # Argument::Result(u16)
    return b"\x02" + _bcs_u16(n)


def _bcs_command_split_coins(coin: bytes, amounts: list[bytes]) -> bytes:
    # Command::SplitCoins { coin: Argument, amounts: vector<Argument> }
    return b"\x02" + coin + _bcs_vec(amounts)


def _bcs_command_transfer_objects(coins: list[bytes], to: bytes) -> bytes:
    # Command::TransferObjects { coins: vector<Argument>, to: Argument }
    return b"\x01" + _bcs_vec(coins) + to


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


def _serialize_tx_ptb_v1(inputs: list[bytes], commands: list[bytes], sender: str,
                         gas_coin: dict, gas_price: int, budget: int,
                         shared_versions: dict) -> bytes:
    """Serialize a ProgrammableTransaction whose inputs are already BCS CallArg
    bytes and commands are already BCS Command bytes (used by transfer_asset)."""
    # TransactionKind::ProgrammableTransaction
    kind = b"\x00" + _bcs_programmable(inputs, commands)
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


def _digest_to_hex(digest: str) -> str:
    """Sui object digests are base58 in GraphQL responses; the BCS object ref
    needs the raw 32 bytes as hex. Converts base58 -> '0x' + hex."""
    if not digest:
        return ""
    if digest.startswith("0x") and len(digest) == 66:
        return digest
    try:
        import base58 as _b58
        return "0x" + _b58.b58decode(digest).hex()
    except Exception:
        return digest


# ---------------------------------------------------------------- Adapter

class SUIAdapter:
    def __init__(self, ledger: ExecLedger, keypair_hex: str,
                 rpc_url: str = SUI_MAINNET_RPC, testnet: bool = False,
                 network: str = "", usdc_coin_type: str | None = None,
                 deepbook_package: str | None = None,
                 pool_id: str | None = None,
                 balance_manager: str | None = None,
                 pool_coin_types: list[str] | None = None,
                 aftermath: object | None = None):
        self.ledger = ledger
        self.testnet = testnet
        # Explicit network name wins (mainnet|testnet); Aftermath has no devnet.
        self.network = (network or ("testnet" if testnet else "mainnet")).strip().lower()
        if self.network not in ("mainnet", "testnet"):
            self.network = "testnet" if testnet else "mainnet"
        if rpc_url and rpc_url != SUI_MAINNET_RPC:
            self.rpc_url = rpc_url
        else:
            self.rpc_url = {
                "testnet": SUI_TESTNET_RPC,
                "mainnet": SUI_MAINNET_RPC,
            }.get(self.network, SUI_TESTNET_RPC if testnet else rpc_url)
        self.usdc_coin_type = usdc_coin_type or (
            USDC_TESTNET_COIN_TYPE if self.network == "testnet" else USDC_MAINNET_COIN_TYPE)
        self.deepbook_package = deepbook_package
        self.pool_id = pool_id
        self.balance_manager = balance_manager
        self.pool_coin_types = pool_coin_types or [SUI_COIN_TYPE, self.usdc_coin_type]
        self.aftermath = aftermath  # optional AftermathAdapter for aftermath-perp venue
        self._shared_cache: dict = {}
        self._rpc_seq = 0

        self._seed: bytes | None = None
        self._kp = None  # pysui keypair when available
        self._pub: bytes | None = None
        self.address = ""
        self._sign_error = ""
        try:
            # Accept both raw hex seed (32 bytes) and the bech32 'suiprivkey1...'
            # keystring that wallets use. The onboarding generates bech32.
            if keypair_hex.startswith("suiprivkey"):
                from pysui.sui.sui_crypto import SuiKeyPair
                self._kp = SuiKeyPair.from_bech32(keypair_hex)
                self._seed = bytes(self._kp.private_key.key_bytes)
            else:
                hexed = keypair_hex[2:] if keypair_hex.startswith("0x") else keypair_hex
                self._seed = bytes.fromhex(hexed)
                if len(self._seed) != 32:
                    raise ValueError(f"keypair_hex must decode to 32 bytes, got {len(self._seed)}")
                try:
                    from pysui.sui.sui_crypto import SuiKeyPair
                    self._kp = SuiKeyPair.from_b64(base64.b64encode(b"\x00" + self._seed).decode())
                except ImportError:
                    self._kp = None
            if self._kp is not None:
                self._pub = bytes(self._kp.public_key.key_bytes)
            else:
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

    # ---- GraphQL endpoint (replaces deprecated JSON-RPC for balance/coins) ----
    GQL_URL = "https://graphql.{network}.sui.io/graphql"

    def _gql(self, query: str) -> dict:
        """Execute a GraphQL query against the public Sui GraphQL endpoint."""
        url = self.GQL_URL.format(network=self.network)
        try:
            r = requests.post(url, json={"query": query}, timeout=RPC_TIMEOUT)
            if r.status_code != 200:
                raise SuiRpcError("graphql", f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if data.get("errors"):
                raise SuiRpcError("graphql", str(data["errors"][:2]))
            return data.get("data", {})
        except requests.RequestException as exc:
            raise SuiRpcError("graphql", f"transport error: {exc}") from exc

    def _gql_balance(self, coin_type: str) -> int:
        """Total balance for a coin type via GraphQL `address { balance }`.

        This is the reliable way to read a total (SUI, USDC) on mainnet.
        The `objects` coin query returns empty for Coin on current mainnet
        GraphQL, but `balance`/`balances` works correctly."""
        try:
            q = '{ address(address: "' + self.address + '") { balance(coinType: "' + coin_type + '") { totalBalance } } }'
            data = self._gql(q)
            raw = ((data.get("address") or {}).get("balance") or {}).get("totalBalance") or "0"
            return int(str(raw).strip() or 0)
        except Exception:
            return 0

    def _gql_coins(self, coin_type: str) -> list[dict]:
        """Owned coin objects via GraphQL (JSON-RPC suix_getCoins is deprecated).

        The objects filter requires the full `0x2::coin::Coin<...>` type (the
        raw coin type alone matches nothing), and the true balance is in
        `contents.json.balance` (raw u64) - the per-object balance field
        returns 0 on current GraphQL.

        On mainnet the GraphQL `objects` coin query currently returns empty
        even when `balance` shows funds (verified 2026-09). Fall back to a
        JSON-RPC `suix_getCoins` via Blockvision when that happens."""
        addr = self.address[2:] if self.address.startswith("0x") else self.address
        raw_coin = coin_type
        if coin_type.startswith("0x2::coin::Coin<"):
            # unwrap for fallback RPC which wants the inner type
            try:
                raw_coin = coin_type.split("<", 1)[1].rsplit(">", 1)[0]
            except Exception:
                raw_coin = coin_type
        if not coin_type.startswith("0x2::coin::Coin<"):
            coin_type = f"0x2::coin::Coin<{coin_type}>"
        q = (
            '{ address(address: "0x' + addr + '") {'
            '  objects(first: 50, filter: {type: "' + coin_type + '"}) {'
            '    nodes {'
            '      address'
            '      version'
            '      digest'
            '      contents { json }'
            '    }'
            '  }'
            '} }'
        )
        data = self._gql(q)
        nodes = (data.get("address") or {}).get("objects") or {}
        nodes = nodes.get("nodes") or []
        out = []
        for n in nodes:
            oid = n.get("address")
            if not oid:
                continue
            bal = 0
            try:
                bal = int(((n.get("contents") or {}).get("json") or {}).get("balance") or 0)
            except Exception:
                pass
            out.append({
                "objectId": oid,
                "version": int(n.get("version", 0)),
 "digest": _digest_to_hex(n.get("digest", "")),
                  "balance": bal,
              })
        if out:
            return out
        # Fallback for mainnet where GraphQL objects returns empty: try
        # Blockvision JSON-RPC `suix_getCoins` (still serves it).
        if self.network == "mainnet":
            try:
                import requests as _rq
                import time as _time
                for url in (SUI_MAINNET_RPC_FALLBACK, self.rpc_url):
                    for attempt in range(3):
                        try:
                            r = _rq.post(url, json={
                                "jsonrpc": "2.0", "id": 1, "method": "suix_getCoins",
                                "params": [self.address, raw_coin, None, 50],
                            }, timeout=8)
                            j = r.json()
                            # Blockvision rate-limit returns {"error_msg": "too frequent"}
                            if isinstance(j, dict) and j.get("error_msg") and "frequent" in str(j.get("error_msg")).lower():
                                _time.sleep(1.5 * (attempt + 1))
                                continue
                            data_list = (j.get("result") or {}).get("data") or []
                            if data_list:
                                fallback = []
                                for c in data_list:
                                    fallback.append({
                                        "objectId": c.get("coinObjectId") or c.get("objectId"),
                                        "version": int(c.get("version", 0)),
                                        "digest": _digest_to_hex(str(c.get("digest") or "")),
                                        "balance": int(c.get("balance") or 0),
                                    })
                                if fallback:
                                    return fallback
                            break
                        except Exception:
                            _time.sleep(0.5)
                            continue
            except Exception:
                pass
        return out

    # ------------------------------------------------------------ orders

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
            # Use GraphQL `balance` (reliable on mainnet) instead of summing
            # coin objects via `objects` which currently returns empty.
            total = self._gql_balance(coin_type)
            if total == 0:
                # Fallback to summing coin objects if balance field is empty
                try:
                    coins = self._gql_coins(coin_type)
                    total = sum(c["balance"] for c in coins) or 0
                except Exception:
                    pass
            balance = total / (10**decimals)
        except Exception:  # noqa: BLE001
            log.exception("get_balance failed for %s", asset)
            return 0.0
        # AFTERMATH ACCOUNT: when the perp adapter is attached, USDC balance
        # includes the collateral held inside the Aftermath trading account
        # (the wallet's on-chain USDC plus what was deposited into the perp
        # account). This is what the risk guard must see for exposure caps.
        if asset == "USDC" and self.aftermath is not None:
            try:
                acct = self.aftermath.accounts()
                for a in acct or []:
                    if isinstance(a, dict):
                        coll = a.get("collateral")
                        if coll:
                            balance += float(coll)
            except Exception:
                pass
            # Fallback: use the native collateral() when CCXT accounts lack the field.
            if balance == 0:
                try:
                    balance += self.aftermath.collateral()
                except Exception:
                    pass
        return balance

    def get_positions(self) -> list[dict]:
        """Open perp positions. DeepBook margin is not indexed (v1); Aftermath
        perp positions come from the CCXT API when an Aftermath adapter is
        attached. Returns normalized rows: {symbol, side, qty, entry, pnl}."""
        if self.aftermath is not None:
            return self._aftermath_positions()
        return []

    def _aftermath_positions(self) -> list[dict]:
        """Normalized positions from the Aftermath CCXT positions endpoint."""
        try:
            resp = self.aftermath.positions()
        except Exception as exc:
            log.warning("[sui] aftermath positions failed: %s", exc)
            return []
        if not resp.get("ok"):
            log.warning("[sui] aftermath positions error: %s", resp.get("error"))
            return []
        rows = resp.get("data")
        if not isinstance(rows, list):
            return []
        out = []
        for p in rows:
            if not isinstance(p, dict):
                continue
            symbol = str(p.get("symbol") or "")
            qty_raw = float(p.get("contracts") or p.get("baseAssetAmount") or 0.0)
            if not symbol or qty_raw == 0:
                continue
            side = "long" if qty_raw > 0 else "short"
            out.append({
                "symbol": symbol.split("/")[0].split(":")[0],
                "side": side,
                "qty": abs(qty_raw),
                "entry": float(p.get("entryPrice") or 0.0),
                "pnl": float(p.get("unrealizedPnl") or 0.0),
                "venue": "aftermath",
            })
        return out

    # ------------------------------------------------------------ orders

    def _shared_versions(self) -> dict:
        if len(self._shared_cache) == 2:
            return self._shared_cache
        ids = [oid for oid in (self.pool_id, self.balance_manager) if oid]
        if not ids:
            return {}
        try:
            res = self._rpc("sui_multiGetObjects", [
                ids,
                {"showOwner": True, "showContent": False, "showType": False, "showDisplay": False},
            ])
        except SuiRpcError as exc:
            # JSON-RPC multiGetObjects deprecated on public fullnodes; DeepBook
            # shared-object txs are not the transfer path, so empty is safe.
            log.warning("[sui] shared versions unavailable: %s", exc)
            return {}
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
        coins = self._gql_coins(SUI_COIN_TYPE)
        # Prefer a coin with enough for gas; otherwise fall back to the largest
        # available coin (better to try with 0.02 SUI than fail when user has 0.06).
        for c in coins:
            if c["balance"] >= GAS_MIN_BALANCE:
                return {"objectId": c["objectId"], "version": c["version"], "digest": c["digest"]}
        if coins:
            # No coin meets the ideal threshold — use the largest available and let
            # the node validate the budget (dry-run already caps it).
            best = max(coins, key=lambda x: x["balance"])
            if best["balance"] >= 1_000_000:  # at least 0.001 SUI
                return {"objectId": best["objectId"], "version": best["version"], "digest": best["digest"]}
        raise SuiRpcError("graphql coins", f"no SUI gas coin with >= {GAS_MIN_BALANCE/1e9:.3f} SUI; have {coins[0]['balance']/1e9:.4f} SUI" if coins else "no SUI coins found")

    def _get_coins(self, coin_type: str, limit: int = 50) -> list[dict]:
        """Owned coin objects of a type: [{objectId, version, digest, balance}].

        Uses GraphQL (suix_getCoins is deprecated on public fullnodes)."""
        return self._gql_coins(coin_type)

    def transfer_asset(self, recipient: str, amount: float, asset: str = "USDC") -> dict:
        """Send `amount` of `asset` (USDC or SUI) to `recipient` on-chain.

        Builds a SplitCoins + TransferObjects programmable transaction (merge
        dust coins first if the primary is short), dry-runs it, then signs and
        broadcasts with the wallet key. Returns {ok, digest, tx_bytes} or
        {ok: False, error: ...}.
        """
        if self._sign_error:
            return {"ok": False, "error": self._sign_error}
        try:
            coin_type, decimals = self._coin_spec(asset)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not recipient or len(recipient.replace("0x", "")) < 40:
            return {"ok": False, "error": "invalid recipient address"}
        if amount <= 0:
            return {"ok": False, "error": "amount must be positive"}
        amount_raw = int(round(amount * (10 ** decimals)))
        if amount_raw <= 0:
            return {"ok": False, "error": f"amount too small for {asset} decimals"}

        try:
            coins = self._get_coins(coin_type)
            if not coins:
                return {"ok": False, "error": f"no {asset} balance on {self.address[:12]}…"}
            coins.sort(key=lambda c: c["balance"], reverse=True)
            gas_coin = None
            if asset == "SUI":
                # When sending SUI, the gas coin must differ from the transfer
                # source (same ObjectRef twice is rejected). Reserve the
                # LARGEST coin as gas and transfer from the rest, merging all
                # other coins into the transfer primary first so the amount
                # can span multiple coins.
                if len(coins) >= 2:
                    gas_coin = coins[0]
                    primary = coins[1]
                    others = [c for c in coins[2:] if c["objectId"] != primary["objectId"]]
                    # merge the gas-adjacent coins too: merge every coin EXCEPT
                    # the reserved gas coin into the primary so the transfer
                    # amount can use the full non-gas balance.
                    others += [c for c in coins if c["objectId"] not in
                               (primary["objectId"], gas_coin["objectId"])]
                else:
                    gas_coin = None  # single coin: split a gas amount out below
                    primary = coins[0]
                    others = []
            else:
                primary = coins[0]
                others = [c for c in coins[1:] if c["objectId"] != primary["objectId"]]
            total = sum(c["balance"] for c in coins)
            if amount_raw > total:
                return {"ok": False,
                        "error": f"insufficient {asset}: have {total / 10**decimals:.6f}, need {amount:.6f}"}
            # When sending SUI, the transfer primary must hold the amount after
            # merging - cap the request at the primary+others balance minus gas.
            if asset == "SUI" and gas_coin is not None:
                spendable = total - int(gas_coin["balance"])
                if amount_raw > spendable:
                    return {"ok": False,
                            "error": f"insufficient {asset} after gas reserve: "
                                     f"spendable {spendable/10**decimals:.6f}, need {amount:.6f}"}

            # Build BCS inputs/commands and JSON for dry-run.
            bcs_inputs, bcs_commands = [], []
            json_inputs, json_transactions = [], []

            # Merge step: merge dust coins into primary.
            if others:
                for c in others:
                    bcs_inputs.append(_bcs_call_arg_imm_or_owned(
                        primary["objectId"], primary["version"], primary["digest"]))
                    bcs_inputs.append(_bcs_call_arg_imm_or_owned(
                        c["objectId"], c["version"], c["digest"]))
                    bcs_commands.append(b"\x03" + _bcs_argument_input(0) + _bcs_vec(
                        [_bcs_argument_input(1)]))
                    json_inputs.append({"Object": {"ImmOrOwnedObject": {
                        "objectId": primary["objectId"], "version": primary["version"],
                        "digest": primary["digest"]}}})
                    json_inputs.append({"Object": {"ImmOrOwnedObject": {
                        "objectId": c["objectId"], "version": c["version"],
                        "digest": c["digest"]}}})
                    # MergeCoins command: merge the second coin into the first
                    json_transactions.append({
                        "MergeCoins": {"destination": {"Input": 0}, "sources": [{"Input": 1}]}})

            # Transfer step: SplitCoins(primary, [amount]) + TransferObjects([res0], recipient)
            recipient_addr = recipient if recipient.startswith("0x") else f"0x{recipient}"
            coin_idx = len(bcs_inputs)  # index of the primary coin input in the BCS array
            bcs_inputs.append(_bcs_call_arg_imm_or_owned(
                primary["objectId"], primary["version"], primary["digest"]))
            bcs_inputs.append(_bcs_call_arg_pure(_bcs_u64(amount_raw)))
            bcs_inputs.append(_bcs_call_arg_pure(_bcs_addr(recipient_addr)))
            json_inputs.append({"Object": {"ImmOrOwnedObject": {
                "objectId": primary["objectId"], "version": primary["version"],
                "digest": primary["digest"]}}})
            json_inputs.append({"Pure": "0x" + _bcs_u64(amount_raw).hex()})
            json_inputs.append({"Pure": "0x" + _bcs_addr(recipient_addr).hex()})

            coin_arg = _bcs_argument_input(coin_idx)
            amount_arg = _bcs_argument_input(coin_idx + 1)
            recv_arg = _bcs_argument_input(coin_idx + 2)
            bcs_commands.append(_bcs_command_split_coins(coin_arg, [amount_arg]))
            bcs_commands.append(_bcs_command_transfer_objects([_bcs_argument_result(0)], recv_arg))
            json_transactions.append(
                {"SplitCoins": {"coin": {"Input": coin_idx}, "amounts": [{"Input": coin_idx + 1}]}})
            json_transactions.append(
                {"TransferObjects": {"coins": [{"Result": 0}], "to": {"Input": coin_idx + 2}}})

            tx_json = {
                "kind": "ProgrammableTransaction",
                "inputs": json_inputs,
                "transactions": json_transactions,
            }
            gas = self._dry_run(tx_json)
            out = self._broadcast_ptb(bcs_inputs, bcs_commands, gas["gas_price"], gas["budget"],
                                      gas_coin=gas_coin)
            return {
                "ok": True,
                "venue": "sui",
                "asset": asset,
                "amount": amount,
                "recipient": recipient_addr,
                "tx_hash": out["digest"],
                "digest": out["digest"],
                "tx_bytes": out["tx_bytes"],
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("transfer_asset failed %s->%s", asset, recipient)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    def _broadcast_ptb(self, inputs: list[bytes], commands: list[bytes],
                       gas_price: int, budget: int, gas_coin: dict | None = None) -> dict:
        """Broadcast a raw programmable-transaction (input bytes already BCS).

        gas_coin: optional explicit gas coin. When sending SUI the gas coin
        must NOT be the coin being transferred (duplicated ObjectRef), so
        callers pass a different coin here."""
        coin = gas_coin or self._pick_gas_coin()
        # Cap budget to gas coin balance to avoid "gas budget is too high"
        # when the user has a small SUI balance (e.g. 0.06 SUI) and the dry-run
        # returns a budget near or above it.
        try:
            if budget >= int(coin.get("balance", 0)):
                budget = max(5_000_000, int(coin["balance"]) - 1_000_000)
        except Exception:
            pass
        tx_bytes = _serialize_tx_ptb_v1(
            inputs, commands, self.address,
            coin,
            gas_price, budget, self._shared_versions(),
        )
        return self._broadcast_tx(tx_bytes)

    def _dry_run(self, tx_json: dict) -> dict:
        """Estimate gas for a transaction.

        Tries GraphQL simulateTransaction (available on mainnet GraphQL); when
        the mutation is absent (testnet), falls back to a fixed budget derived
        from the reference gas price. A transfer PTB costs ~0.001-0.005 SUI."""
        q = (
            'mutation { '
            '  simulateTransaction(transaction: ' + json.dumps(tx_json) + ', '
            '    doGasSelection: true) { '
            '    effects { '
            '      status { status error } '
            '      gasUsed { computationCost storageCost } '
            '    } '
            '  } '
            '}'
        )
        try:
            data = self._gql(q)
        except SuiRpcError as exc:
            if "simulateTransaction" in str(exc) or "GRAPHQL" in str(exc):
                # simulateTransaction not exposed on this network (testnet) -
                # use a fixed budget from the reference gas price. A transfer
                # PTB with storage costs ~0.005-0.02 SUI; budget generously.
                gas_price = self._gql_gas_price()
                return {"gas_price": gas_price, "budget": max(20_000_000, gas_price * 20_000)}
            raise
        effects = (data.get("simulateTransaction") or {}).get("effects") or {}
        status = effects.get("status") or {}
        if str(status.get("status")) != "SUCCESS":
            raise SuiRpcError(
                "simulateTransaction",
                "dry run failure: " + str(status.get("error") or status.get("status") or "unknown"),
            )
        gas = effects.get("gasUsed") or {}
        budget = int(gas.get("computationCost", 0)) + int(gas.get("storageCost", 0))
        budget = max(budget + budget // 2, 500_000)
        gas_price = int(self._gql_gas_price())
        return {"gas_price": gas_price, "budget": budget}

    def _gql_gas_price(self) -> int:
        """Reference gas price via GraphQL, falling back to the standard 1000
        MIST when the field is not exposed (testnet ServiceConfig lacks it)."""
        try:
            q = '{ serviceConfig { referenceGasPrice } }'
            data = self._gql(q)
            cfg = data.get("serviceConfig") or {}
            return int(cfg.get("referenceGasPrice") or 1000)
        except SuiRpcError:
            return 1000

    def _broadcast(self, call_args: list[dict], command: dict,
                   gas_price: int, budget: int) -> dict:
        tx_bytes = _serialize_tx_data_v1(
            call_args, command, self.address,
            self._pick_gas_coin(), gas_price, budget, self._shared_versions(),
        )
        return self._broadcast_tx(tx_bytes)

    def _broadcast_tx(self, tx_bytes: bytes) -> dict:
        """Broadcast tx bytes via GraphQL executeTransaction mutation.

        GraphQL signature: executeTransaction(transactionDataBcs: Base64!,
        signatures: [Signature!]!). The digest and status live on effects
        (ExecutionResult has only effects). Returns {tx_bytes, signature,
        digest, status}."""
        signature = self._sign(tx_bytes)
        tx_b64 = base64.b64encode(tx_bytes).decode()
        q = (
            'mutation { '
            '  executeTransaction(transactionDataBcs: "' + tx_b64 + '", '
            '    signatures: ["' + signature + '"]) { '
            '    effects { digest status } '
            '  } '
            '}'
        )
        data = self._gql(q)
        exec_res = data.get("executeTransaction") or {}
        effects = exec_res.get("effects") or {}
        digest = effects.get("digest") or ""
        status = str(effects.get("status") or "")
        return {"tx_bytes": tx_b64, "signature": signature, "digest": digest,
                "status": status}

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
        if intent.venue == "aftermath-perp":
            if self.aftermath is None:
                return {"ok": False, "error": "aftermath-perp venue requested but no Aftermath adapter configured"}
            try:
                return self.aftermath.place_order(intent, ref_price)
            except Exception as exc:
                log.exception("aftermath place_order failed for %s", intent.idempotency_key)
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
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
        """Killswitch cancel: cancels DeepBook orders (on-chain) and Aftermath
        orders (CCXT REST) when the respective venue is configured.
        Best-effort per venue; returns a merged result."""
        merged = {"ok": True, "cancelled": [], "errors": []}
        if self.deepbook_package and self.pool_id and self.balance_manager:
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
                merged["cancelled"].append({"venue": "deepbook", "tx_hash": out["digest"]})
            except Exception as exc:
                log.exception("deepbook cancel_all failed for bot %s", bot_id)
                merged["errors"].append(f"deepbook: {exc}"[:200])
                merged["ok"] = False
        if self.aftermath is not None:
            try:
                res = self.aftermath.cancel_all()
                merged["cancelled"].append({"venue": "aftermath", "ok": bool(res.get("ok"))})
                if not res.get("ok"):
                    merged["errors"].append(f"aftermath: {res.get('error')}"[:200])
                    merged["ok"] = False
            except Exception as exc:
                log.exception("aftermath cancel_all failed for bot %s", bot_id)
                merged["errors"].append(f"aftermath: {exc}"[:200])
                merged["ok"] = False
        if not merged["cancelled"]:
            return {
                "ok": False,
                "error": "no venue configured (need deepbook package/pool/balance_manager or Aftermath adapter)",
            }
        return merged

    def flat_and_cancel(self, bot_id) -> dict:
        """Killswitch hook: cancels open orders and closes open positions.
        Aftermath perp positions are flattened via the CCXT API; DeepBook
        margin positions are not indexed in v1 (best-effort cancel only)."""
        result = self.cancel_all(bot_id)
        result["closed"] = []
        if self.aftermath is not None:
            try:
                af = self.aftermath.flat_and_cancel(bot_id)
                result["closed"].extend(af.get("closed") or [])
                result["flat"] = f"aftermath flattened {len(af.get('closed') or [])} positions"
            except Exception as exc:
                log.exception("aftermath flat failed for bot %s", bot_id)
                result["errors"].append(f"aftermath-flat: {exc}"[:200])
                result["ok"] = False
        else:
            result["flat"] = "v1 stub: no aftermath adapter (DeepBook margin not indexed)"
        return result