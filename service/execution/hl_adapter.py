"""Hyperliquid real-trading adapter via API (agent) wallets.

Signing mirrors hyperliquid-python-sdk exactly:
- L1 exchange actions (order/cancel) are hashed as keccak(msgpack(action) +
  nonce as 8 big-endian bytes + vault flag byte) and wrapped in an EIP-712
  "Exchange" envelope carrying a phantom-agent message signed by the agent
  wallet ("a" source on mainnet, "b" on testnet).
- Agent approvals are user-signed typed data
  ("HyperliquidTransaction:ApproveAgent") signed by the master wallet;
  approval state is read back via the extraAgents info query.

API wallets can sign orders and cancels for the master account but have no
withdrawal rights, which is the delegation model from docs/real-trading.
"""

import logging
import random
import time
from decimal import Decimal

import msgpack
import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_hex

from exec_vault import generate_key_material
from ledger import ExecLedger

log = logging.getLogger(__name__)

HL_MAINNET_INFO = "https://api.hyperliquid.xyz/info"
HL_MAINNET_EXCHANGE = "https://api.hyperliquid.xyz/exchange"
HL_TESTNET_INFO = "https://api.hyperliquid-testnet.xyz/info"
HL_TESTNET_EXCHANGE = "https://api.hyperliquid-testnet.xyz/exchange"

REQUEST_TIMEOUT = 20.0
MAX_RETRIES = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
BACKOFF_BASE_SECONDS = 0.4
MARKETABLE_SLIPPAGE = 0.02

_EIP712_DOMAIN_TYPES = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

_APPROVE_AGENT_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "agentAddress", "type": "address"},
    {"name": "agentName", "type": "string"},
    {"name": "nonce", "type": "uint64"},
]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _float_to_wire(x: float) -> str:
    rounded = f"{x:.8f}"
    if abs(float(rounded) - x) >= 1e-12:
        raise ValueError(f"float_to_wire causes rounding: {x}")
    if rounded == "-0":
        rounded = "0"
    return f"{Decimal(rounded).normalize():f}"


def _normalize_coin(symbol: str) -> str:
    raw = (symbol or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        return raw
    s = raw.upper()
    for suffix in ("-PERP", "PERP", "-USD", "/USD", "-USDT", "/USDT"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    if s.endswith("USDT") and len(s) > 4:
        s = s[: -4]
    return s


def _retry_delay(attempt: int) -> float:
    base = BACKOFF_BASE_SECONDS * (2 ** attempt)
    return base + random.uniform(0.0, base * 0.25)


def _post_json_with_retry(url: str, payload: dict) -> dict | list:
    """POST JSON with backoff retries on 5xx/429/timeouts, mirroring price_fetcher."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code in RETRYABLE_STATUS:
                resp.raise_for_status()
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, (dict, list)):
                raise RuntimeError(f"unexpected response shape from {url}")
            return data
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            last_exc = exc
            if status not in RETRYABLE_STATUS or attempt >= MAX_RETRIES:
                raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt >= MAX_RETRIES:
                raise
        if attempt < MAX_RETRIES:
            delay = _retry_delay(attempt)
            log.warning(
                "hl request retry %d/%d for %s after %s; sleeping %.2fs",
                attempt + 1, MAX_RETRIES, url, last_exc.__class__.__name__, delay,
            )
            time.sleep(delay)
    raise RuntimeError(f"request to {url} failed: {last_exc}")


def _address_to_bytes(address: str) -> bytes:
    return bytes.fromhex(address[2:] if address.startswith("0x") else address)


def _l1_action_hash(action: dict, nonce: int, vault_address: str | None = None,
                    expires_after: int | None = None) -> bytes:
    data = msgpack.packb(action)
    data += nonce.to_bytes(8, "big")
    if vault_address is None:
        data += b"\x00"
    else:
        data += b"\x01"
        data += _address_to_bytes(vault_address)
    if expires_after is not None:
        data += b"\x00"
        data += expires_after.to_bytes(8, "big")
    return keccak(data)


def _l1_envelope(action_hash: bytes, is_mainnet: bool) -> dict:
    return {
        "domain": {
            "chainId": 1337,
            "name": "Exchange",
            "verifyingContract": "0x0000000000000000000000000000000000000000",
            "version": "1",
        },
        "types": {
            "Agent": [
                {"name": "source", "type": "string"},
                {"name": "connectionId", "type": "bytes32"},
            ],
            "EIP712Domain": _EIP712_DOMAIN_TYPES,
        },
        "primaryType": "Agent",
        "message": {"source": "a" if is_mainnet else "b", "connectionId": action_hash},
    }


def _user_signed_envelope(primary_type: str, payload_types: list, message: dict) -> dict:
    return {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": 0x66EEE,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "types": {primary_type: payload_types, "EIP712Domain": _EIP712_DOMAIN_TYPES},
        "primaryType": primary_type,
        "message": message,
    }


def _sign_typed_data(private_key: str, envelope: dict) -> dict:
    account = Account.from_key(private_key)
    signed = account.sign_message(encode_typed_data(full_message=envelope))
    return {"r": to_hex(signed["r"]), "s": to_hex(signed["s"]), "v": signed["v"]}


def sign_l1_action(private_key: str, action: dict, nonce: int, is_mainnet: bool,
                   vault_address: str | None = None, expires_after: int | None = None) -> dict:
    """Sign a Hyperliquid L1 exchange action (order/cancel) with a private key."""
    return _sign_typed_data(private_key, _l1_envelope(
        _l1_action_hash(action, nonce, vault_address, expires_after), is_mainnet))


def recover_l1_signer(action: dict, signature: dict, nonce: int, is_mainnet: bool,
                      vault_address: str | None = None, expires_after: int | None = None) -> str:
    """Recover the signer address of a signed L1 action (for verification)."""
    envelope = _l1_envelope(
        _l1_action_hash(action, nonce, vault_address, expires_after), is_mainnet)
    structured = encode_typed_data(full_message=envelope)
    return Account.recover_message(
        structured, vrs=[signature["v"], int(signature["r"], 16), int(signature["s"], 16)])


def sign_agent_approval(private_key: str, agent_address: str, name: str, nonce: int,
                        is_mainnet: bool) -> dict:
    """EIP-712 sign an ApproveAgent action with the master wallet key."""
    message = {
        "hyperliquidChain": "Mainnet" if is_mainnet else "Testnet",
        "agentAddress": agent_address.lower(),
        "agentName": name or "",
        "nonce": nonce,
    }
    return _sign_typed_data(private_key, _user_signed_envelope(
        "HyperliquidTransaction:ApproveAgent", _APPROVE_AGENT_TYPES, message))


def recover_agent_approval_signer(signature: dict, agent_address: str, name: str, nonce: int,
                                  is_mainnet: bool) -> str:
    """Recover the master wallet that signed an agent approval (for verification)."""
    message = {
        "hyperliquidChain": "Mainnet" if is_mainnet else "Testnet",
        "agentAddress": agent_address.lower(),
        "agentName": name or "",
        "nonce": nonce,
    }
    envelope = _user_signed_envelope(
        "HyperliquidTransaction:ApproveAgent", _APPROVE_AGENT_TYPES, message)
    structured = encode_typed_data(full_message=envelope)
    return Account.recover_message(
        structured, vrs=[signature["v"], int(signature["r"], 16), int(signature["s"], 16)])


class HLApiWallet:
    """A Hyperliquid API (agent) wallet: signs orders for the master account."""

    def __init__(self, private_key: str | None = None, testnet: bool = False):
        self.testnet = testnet
        self.info_url = HL_TESTNET_INFO if testnet else HL_MAINNET_INFO
        self.exchange_url = HL_TESTNET_EXCHANGE if testnet else HL_MAINNET_EXCHANGE
        if private_key is None:
            _, private_key = generate_key_material("hyperliquid")
        self._account = Account.from_key(private_key)
        self.address = self._account.address

    @classmethod
    def generate(cls, testnet: bool = False) -> "HLApiWallet":
        """Fresh agent keypair via exec_vault.generate_key_material("hyperliquid")."""
        return cls(None, testnet=testnet)

    def approve_agent_tx(self, master_private_key: str, agent_address: str | None = None,
                         name: str = "") -> dict:
        """Signed approveAgent exchange payload (POST body), signed by the master."""
        agent = (agent_address or self.address).lower()
        nonce = _now_ms()
        action = {
            "type": "approveAgent",
            "hyperliquidChain": "Testnet" if self.testnet else "Mainnet",
            "agentAddress": agent,
            "agentName": name or "",
            "nonce": nonce,
            "signatureChainId": "0x66eee",
        }
        signature = sign_agent_approval(master_private_key, agent, name, nonce,
                                        is_mainnet=not self.testnet)
        return {"action": action, "nonce": nonce, "signature": signature}

    def submit_agent_approval(self, master_private_key: str, name: str = "") -> dict:
        """Approve this agent wallet on-chain: master signs, exchange endpoint submits."""
        payload = self.approve_agent_tx(master_private_key, name=name)
        return _post_json_with_retry(self.exchange_url, payload)

    def is_agent_approved(self, master_address: str, agent_address: str | None = None) -> bool:
        """True when `agent_address` is a registered API wallet of `master_address`."""
        agent = (agent_address or self.address).lower()
        data = _post_json_with_retry(self.info_url, {"type": "extraAgents", "user": master_address})
        return any(str(a.get("address", "")).lower() == agent
                   for a in (data or []) if isinstance(a, dict))


class HLAdapter:
    """Order/cancel/state access to Hyperliquid for one master account via an agent wallet."""

    def __init__(self, ledger: ExecLedger, agent_private_key: str, master_address: str,
                 testnet: bool = False):
        self.ledger = ledger
        self.master_address = master_address
        self.testnet = testnet
        self.info_url = HL_TESTNET_INFO if testnet else HL_MAINNET_INFO
        self.exchange_url = HL_TESTNET_EXCHANGE if testnet else HL_MAINNET_EXCHANGE
        self._agent_key = agent_private_key
        self.agent_address = Account.from_key(agent_private_key).address
        self._coin_to_asset: dict[str, int] = {}

    # ---------------- reads ----------------

    def _ensure_meta(self) -> None:
        if self._coin_to_asset:
            return
        data = _post_json_with_retry(self.info_url, {"type": "meta"})
        if not isinstance(data, dict):
            raise RuntimeError("meta response is not an object")
        for idx, entry in enumerate(data.get("universe") or []):
            if isinstance(entry, dict):
                self._coin_to_asset[entry["name"]] = idx

    def _mid_price(self, coin: str) -> float:
        data = _post_json_with_retry(self.info_url, {"type": "l2Book", "coin": coin})
        levels = data.get("levels") or []
        if len(levels) < 2 or not levels[0] or not levels[1]:
            raise RuntimeError(f"empty l2Book for {coin}")
        bid = float(levels[0][0]["px"])
        ask = float(levels[1][0]["px"])
        return (bid + ask) / 2.0

    def _marketable_price(self, coin: str, side: str) -> str:
        """A limit price that guarantees a fill for a market IOC order."""
        data = _post_json_with_retry(self.info_url, {"type": "l2Book", "coin": coin})
        levels = data.get("levels") or []
        if len(levels) < 2 or not levels[0] or not levels[1]:
            raise RuntimeError(f"empty l2Book for {coin}")
        if side == "buy":
            px = float(levels[1][0]["px"]) * (1 + MARKETABLE_SLIPPAGE)
        else:
            px = float(levels[0][0]["px"]) * (1 - MARKETABLE_SLIPPAGE)
        return _float_to_wire(round(px, 6))

    def get_account_state(self) -> dict:
        """Balances + open positions of the master account. Never raises."""
        try:
            data = _post_json_with_retry(
                self.info_url, {"type": "clearinghouseState", "user": self.master_address})
            margin = data.get("marginSummary") or {}
            balances = {
                "USDC": float(margin.get("accountValue", 0.0)),
                "total_margin_used": float(margin.get("totalMarginUsed", 0.0)),
                "withdrawable": float(data.get("withdrawable", 0.0)),
            }
            positions = []
            for entry in data.get("assetPositions") or []:
                pos = entry.get("position") or {}
                szi = float(pos.get("szi", 0.0))
                if szi == 0.0:
                    continue
                positions.append({
                    "coin": pos.get("coin"),
                    "side": "long" if szi > 0 else "short",
                    "qty": abs(szi),
                    "entry_px": float(pos.get("entryPx", 0.0)),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0.0)),
                    "liquidation_px": float(pos.get("liquidationPx") or 0.0),
                    "leverage": pos.get("leverage") or {},
                })
            return {"ok": True, "balances": balances, "positions": positions}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_fills(self) -> list[dict]:
        """Recent fills of the master account, normalized for ledger reconciliation."""
        try:
            data = _post_json_with_retry(
                self.info_url, {"type": "userFills", "user": self.master_address})
            fills = []
            for f in data or []:
                if not isinstance(f, dict):
                    continue
                fills.append({
                    "coin": f.get("coin"),
                    "price": float(f.get("px", 0.0)),
                    "qty": abs(float(f.get("sz", 0.0))),
                    "side": "buy" if f.get("side") == "B" else "sell",
                    "dir": f.get("dir", ""),
                    "time": f.get("time"),
                    "tx_hash": f.get("hash", ""),
                    "oid": f.get("oid"),
                })
            return fills
        except Exception:
            return []

    # ---------------- writes ----------------

    def _reference_price(self, intent) -> float:
        if intent.limit_price and intent.limit_price > 0:
            return intent.limit_price
        coin = _normalize_coin(intent.symbol)
        try:
            return self._mid_price(coin)
        except Exception:
            return 0.0

    def _order_wire(self, intent) -> dict:
        coin = _normalize_coin(intent.symbol)
        self._ensure_meta()
        if coin not in self._coin_to_asset:
            raise RuntimeError(f"coin {coin} not listed on Hyperliquid")
        wire = {
            "a": self._coin_to_asset[coin],
            "b": intent.side == "buy",
            "s": _float_to_wire(intent.qty),
            "r": False,
            "t": {},
        }
        if intent.order_type == "market":
            wire["p"] = self._marketable_price(coin, intent.side)
            wire["t"] = {"limit": {"tif": "Ioc"}}
        elif intent.order_type == "limit":
            wire["p"] = _float_to_wire(intent.limit_price)
            wire["t"] = {"limit": {"tif": "Gtc"}}
        elif intent.order_type == "stop":
            wire["p"] = self._marketable_price(coin, intent.side)
            wire["t"] = {"trigger": {"isMarket": True,
                                     "triggerPx": _float_to_wire(intent.limit_price),
                                     "tpsl": "sl"}}
        else:  # take_profit
            wire["p"] = self._marketable_price(coin, intent.side)
            wire["t"] = {"trigger": {"isMarket": True,
                                     "triggerPx": _float_to_wire(intent.limit_price),
                                     "tpsl": "tp"}}
        if intent.idempotency_key:
            wire["c"] = "0x" + keccak(intent.idempotency_key.encode("utf-8"))[:16].hex()
        return wire

    @staticmethod
    def _parse_order_response(resp: dict) -> dict:
        statuses = ((resp.get("response") or {}).get("data") or {}).get("statuses") or []
        if not statuses:
            return {"ok": False, "error": f"no order status in exchange response: {resp}"}
        status = statuses[0]
        if isinstance(status, dict) and "error" in status:
            return {"ok": False, "error": status["error"]}
        if isinstance(status, dict) and "filled" in status:
            filled = status["filled"]
            return {
                "ok": True, "status": "filled",
                "venue_order_id": str(filled.get("oid", "")),
                "avg_px": float(filled.get("avgPx", 0.0)),
                "filled_qty": float(filled.get("totalSz", 0.0)),
                "tx_hash": "",
            }
        if isinstance(status, dict) and "resting" in status:
            return {
                "ok": True, "status": "resting",
                "venue_order_id": str(status["resting"].get("oid", "")),
                "avg_px": 0.0, "filled_qty": 0.0, "tx_hash": "",
            }
        return {"ok": False, "error": f"unexpected order status: {status}"}

    def _record_order(self, intent, bot_id: int, result: dict) -> None:
        if self.ledger.order_exists(intent.idempotency_key):
            return
        order_id = self.ledger.create_order(intent, bot_id)
        if not result.get("ok"):
            self.ledger.set_order_status(order_id, "rejected")
            return
        self.ledger.set_order_status(
            order_id, "submitted" if result.get("status") == "resting" else "filled",
            venue_order_id=result.get("venue_order_id"))
        if result.get("status") == "filled":
            self.ledger.record_fill(order_id, result["avg_px"], result["filled_qty"], 0.0,
                                    result.get("tx_hash") or "", bot_id)

    def place_order(self, intent, bot_id: int | None = None) -> dict:
        """Map an OrderIntent to a signed Hyperliquid order. Never raises."""
        try:
            errors = intent.validate(self._reference_price(intent))
            if errors:
                return {"ok": False, "error": "; ".join(errors)}
            wire = self._order_wire(intent)
            action = {"type": "order", "orders": [wire], "grouping": "na"}
            nonce = _now_ms()
            signature = sign_l1_action(self._agent_key, action, nonce,
                                       is_mainnet=not self.testnet)
            resp = _post_json_with_retry(
                self.exchange_url, {"action": action, "nonce": nonce, "signature": signature})
            result = self._parse_order_response(resp)
            if result.get("ok") and bot_id is not None:
                self._record_order(intent, bot_id, result)
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def cancel_all(self, bot_id: int | None = None) -> dict:
        """Cancel every open order of the master account. Never raises."""
        try:
            open_orders = _post_json_with_retry(
                self.info_url, {"type": "openOrders", "user": self.master_address})
            self._ensure_meta()
            cancels = []
            for o in open_orders or []:
                if not isinstance(o, dict):
                    continue
                coin = o.get("coin", "")
                if coin not in self._coin_to_asset:
                    continue
                cancels.append({"a": self._coin_to_asset[coin], "o": o.get("oid")})
            if not cancels:
                return {"ok": True, "cancelled": 0}
            action = {"type": "cancel", "cancels": cancels}
            nonce = _now_ms()
            signature = sign_l1_action(self._agent_key, action, nonce,
                                       is_mainnet=not self.testnet)
            resp = _post_json_with_retry(
                self.exchange_url, {"action": action, "nonce": nonce, "signature": signature})
            statuses = ((resp.get("response") or {}).get("data") or {}).get("statuses") or []
            cancelled = sum(1 for s in statuses if s == "success")
            result = {"ok": True, "cancelled": cancelled, "total": len(cancels)}
            errors = [s.get("error") for s in statuses
                      if isinstance(s, dict) and s.get("error")]
            if errors:
                result["errors"] = errors
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def flat_and_cancel(self, bot_id: int | None = None) -> dict:
        """Killswitch hook: close all positions (reduce-only market) then cancel all orders.

        Cancel-all still runs when flattening fails; failures are reported, never
        swallowed into an ok=True result.
        """
        result: dict = {"ok": False, "flattened": [], "cancelled": 0, "errors": []}
        try:
            state = self.get_account_state()
            if not state.get("ok"):
                result["errors"].append(state.get("error", "account state fetch failed"))
            else:
                wires, coins = [], []
                for pos in state.get("positions", []):
                    coin = pos["coin"]
                    try:
                        self._ensure_meta()
                        if coin not in self._coin_to_asset:
                            raise RuntimeError(f"coin {coin} not listed on Hyperliquid")
                        close_side = "buy" if pos["side"] == "short" else "sell"
                        wires.append({
                            "a": self._coin_to_asset[coin],
                            "b": close_side == "buy",
                            "p": self._marketable_price(coin, close_side),
                            "s": _float_to_wire(pos["qty"]),
                            "r": True,
                            "t": {"limit": {"tif": "Ioc"}},
                        })
                        coins.append(coin)
                    except Exception as exc:
                        result["errors"].append(f"{coin}: {exc}")
                if wires:
                    action = {"type": "order", "orders": wires, "grouping": "na"}
                    nonce = _now_ms()
                    signature = sign_l1_action(self._agent_key, action, nonce,
                                               is_mainnet=not self.testnet)
                    resp = _post_json_with_retry(self.exchange_url, {
                        "action": action, "nonce": nonce, "signature": signature})
                    statuses = ((resp.get("response") or {}).get("data") or {}).get("statuses") or []
                    for i, status in enumerate(statuses):
                        coin = coins[i] if i < len(coins) else f"order[{i}]"
                        if isinstance(status, dict) and status.get("error"):
                            result["errors"].append(f"{coin}: {status['error']}")
                        else:
                            result["flattened"].append(coin)
            cancel_res = self.cancel_all(bot_id)
            result["cancelled"] = cancel_res.get("cancelled", 0)
            if not cancel_res.get("ok"):
                result["errors"].append(cancel_res.get("error", "cancel_all failed"))
            result["ok"] = not result["errors"]
            return result
        except Exception as exc:
            result["errors"].append(str(exc))
            result["ok"] = False
            return result