"""Bluefin Pro (Sui) real-trading adapter: perp CLOB via the off-chain API.

Bluefin is a perp CLOB on Sui: orders are placed off-chain REST and verified
on-chain at settlement time via the order signature. This adapter implements
the EXACT signing scheme of the official Bluefin Pro SDK
(@bluefin-exchange/pro-sdk, verified from its published source):

  Order signing (Sui PersonalMessage over pretty-printed JSON):
    1. Build the UI request object (exact key order):
         {type, ids, account, market, price, quantity, leverage, side,
          positionType, expiration, salt, signedAt}
    2. json = JSON.stringify(ui, null, 2)          # 2-space indent, exact order
    3. msg  = BCS byteVector(json) = uleb128(len) + utf8(json)
    4. intent_bytes = [0x03, 0x00, 0x00]           # IntentScope::PersonalMessage
    5. digest = blake2b-256(intent_bytes + msg)
    6. sig   = ed25519_sign(seed, digest)
    7. serialized = base64( 0x00 || sig(64) || pubkey(32) )   # flag || sig || pk

  Login signing: JSON.stringify(loginRequest) compact (no indent), same
  PersonalMessage scheme, signature sent in the 'payloadSignature' header.

Endpoints (v2.1, from the SDK config):
  auth  : https://auth.api.sui-prod.bluefin.io /auth/v2/token
  trade : https://trade.api.sui-prod.bluefin.io /api/v1/trade/orders
  market: https://api.sui-prod.bluefin.io /api/v1/exchange/...

All methods are failure-tolerant: network/format errors -> {"ok": False, ...}.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import time
from typing import Any

import requests

from ledger import ExecLedger
from order_model import OrderIntent

log = logging.getLogger("execution")

# Bluefin env (Sui). Current API (v2.1, from the official SDK config):
#   auth  = auth.api.sui-prod.bluefin.io
#   trade = trade.api.sui-prod.bluefin.io
#   market = api.sui-prod.bluefin.io
API_TRADE = "https://trade.api.sui-prod.bluefin.io"
API_AUTH = "https://auth.api.sui-prod.bluefin.io"
API_MARKET = "https://api.sui-prod.bluefin.io"
API_STAGING_TRADE = "https://trade.api.sui-staging.bluefin.io"
API_STAGING_AUTH = "https://auth.api.sui-staging.bluefin.io"
API_STAGING_MARKET = "https://api.sui-staging.bluefin.io"

# Bluefin Pro perp market symbols (v2.1).
MARKET_SYMBOLS: dict[str, str] = {
    "BTC": "BTC-PERP", "ETH": "ETH-PERP", "SOL": "SOL-PERP",
    "SUI": "SUI-PERP", "ARB": "ARB-PERP", "AVAX": "AVAX-PERP",
    "BNB": "BNB-PERP", "DOGE": "DOGE-PERP", "LINK": "LINK-PERP",
    "LTC": "LTC-PERP", "OP": "OP-PERP", "MATIC": "MATIC-PERP",
    "SEI": "SEI-PERP",
}

# e9 scale: Bluefin v2.1 uses 10^9 for price/quantity/leverage.
E9 = 1_000_000_000

# Sui IntentScope::PersonalMessage = 3, IntentVersion::V0 = 0, AppId::Sui = 0.
# The Intent is BCS-serialized as three enum variant bytes in that order.
SUI_INTENT_PERSONAL_MESSAGE = b"\x03\x00\x00"


def _bcs_uleb128(n: int) -> bytes:
    """BCS uleb128 encoding (used for the byteVector length prefix)."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _bcs_byte_vector(data: bytes) -> bytes:
    """BCS vector<u8>: uleb128(length) || bytes."""
    return _bcs_uleb128(len(data)) + data


def _sign_personal_message(seed32: bytes, pub32: bytes, message: bytes) -> str:
    """Sui PersonalMessage Ed25519 signature, exactly like @mysten/sui.

    scheme = flag(0x00 ED25519) || signature(64) || public_key(32), base64.
    This is what the Bluefin Pro SDK sends as the order 'signature' and the
    login 'payloadSignature' header.
    """
    from sui_adapter import _ed25519_sign
    # @mysten/sui signPersonalMessage: BCS byteVector of the raw message,
    # wrapped in an IntentMessage(Intent{PersonalMessage,V0,Sui}, bytes).
    vec = _bcs_byte_vector(message)
    intent_msg = SUI_INTENT_PERSONAL_MESSAGE + vec
    digest = hashlib.blake2b(intent_msg, digest_size=32).digest()
    sig = _ed25519_sign(seed32, digest)
    serialized = b"\x00" + sig + pub32
    return base64.b64encode(serialized).decode()


def _to_ui_order_request(signed: dict) -> dict:
    """Build the UI order request object the SDK signs (exact key order).

    Mirrors the SDK's toUICreateOrderRequest(): a fixed key order, price/
    quantity/leverage kept as e9 strings, numeric fields as decimal strings.
    """
    side = "LONG" if str(signed.get("side", "")).upper() in ("LONG", "BUY", "B") else "SHORT"
    return {
        "type": "Bluefin Pro Order",
        "ids": str(signed["idsId"]),
        "account": str(signed["accountAddress"]),
        "market": str(signed["symbol"]),
        "price": str(signed["priceE9"]),
        "quantity": str(signed["quantityE9"]),
        "leverage": str(signed["leverageE9"]),
        "side": side,
        "positionType": "ISOLATED" if signed.get("isIsolated") else "CROSS",
        "expiration": str(signed["expiresAtMillis"]),
        "salt": str(signed["salt"]),
        "signedAt": str(signed["signedAtMillis"]),
    }


def _order_signature(seed32: bytes, pub32: bytes, signed: dict) -> str:
    """Sign an order request with the exact SDK scheme (pretty-printed JSON)."""
    ui = _to_ui_order_request(signed)
    order_json = json.dumps(ui, indent=2)
    return _sign_personal_message(seed32, pub32, order_json.encode("utf-8"))


class BluefinAdapter:
    """Thin failure-tolerant Bluefin Pro (v2.1) REST client with local signing."""

    def __init__(self, ledger: ExecLedger, seed32: bytes, pubkey32: bytes, address: str,
                 testnet: bool = True, api_base: str | None = None):
        self.ledger = ledger
        self.seed = seed32
        self.pubkey = pubkey32
        self.address = address if address.startswith("0x") else f"0x{address}"
        self.trade_api = (api_base or (API_STAGING_TRADE if testnet else API_TRADE)).rstrip("/")
        self.auth_api = (API_STAGING_AUTH if testnet else API_AUTH).rstrip("/")
        self.market_api = (API_STAGING_MARKET if testnet else API_MARKET).rstrip("/")
        self._token: str | None = None
        self._token_at = 0.0
        self._ids_id: str | None = None
        log.info("[bluefin] adapter ready env=%s addr=%s", "staging" if testnet else "prod", self.address)

    # ---------------------------------------------------------------- helpers

    def _req(self, method: str, path: str, json_body: dict | None = None,
             public: bool = False, auth_api: bool = False,
             market_api: bool = False,
             extra_headers: dict | None = None) -> dict:
        base = self.market_api if market_api else (self.auth_api if auth_api else self.trade_api)
        url = f"{base}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        if not public:
            token = self._ensure_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        try:
            r = requests.request(method, url, headers=headers, json=json_body, timeout=20)
            if r.status_code in (401, 403):
                self._token = None  # force re-onboard on next call
                log.warning("[bluefin] auth %s on %s, clearing token", r.status_code, path)
            data = r.json() if r.content else {}
            if r.status_code >= 400:
                return {"ok": False, "error": f"HTTP {r.status_code}: {data}"[:300], "raw": data}
            return {"ok": True, "data": data, "raw": data}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"request failed: {exc}"[:300]}

    def _ensure_token(self) -> str | None:
        """Authenticate via POST /auth/v2/token (exact SDK scheme).

        LoginRequest {accountAddress, signedAtMillis, audience:'api'} is
        JSON.stringify'd COMPACT (no indent), signed as a Sui PersonalMessage,
        and the serialized signature is sent in the 'payloadSignature' header.
        This matches @bluefin-exchange/pro-sdk signLoginRequest() exactly."""
        if self._token and time.time() - self._token_at < 3600:
            return self._token
        signed_at = int(time.time() * 1000)
        audience = os.environ.get("BLUEFIN_AUDIENCE", "api")
        login = {
            "accountAddress": self.address,
            "signedAtMillis": signed_at,
            "audience": audience,
        }
        login_json = json.dumps(login, separators=(",", ":"))  # compact, no spaces
        sig = _sign_personal_message(self.seed, self.pubkey, login_json.encode("utf-8"))
        resp = self._req("POST", "/auth/v2/token",
                         json_body=login,
                         extra_headers={"payloadSignature": sig},
                         public=True, auth_api=True)
        if not resp.get("ok"):
            log.warning("[bluefin] auth v2 failed: %s", resp.get("error"))
            return None
        tok = (resp["data"].get("accessToken") or resp["data"].get("token"))
        if not tok:
            return None
        self._token = tok
        self._token_at = time.time()
        return tok

    # ---------------------------------------------------------------- market

    def market(self, symbol: str) -> str | None:
        sym = (symbol or "").upper()
        mapped = MARKET_SYMBOLS.get(sym)
        if mapped:
            return mapped
        listed = self.markets()
        if sym in listed:
            return f"{sym}-PERP"
        return None

    def markets(self) -> list[str]:
        """Tradable perp base symbols from market API /api/v1/exchange/info."""
        cached = getattr(self, "_markets_cache", None)
        if cached:
            return list(cached)
        try:
            resp = self._req("GET", "/api/v1/exchange/info", public=True, market_api=True)
            data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            markets = data.get("markets") or []
            out = []
            if isinstance(markets, list):
                for m in markets:
                    sym = str(m.get("symbol") or m.get("name") or "")
                    if sym.endswith("-PERP"):
                        out.append(sym[:-5].upper())
            if out:
                self._markets_cache = sorted(set(out))
                return self._markets_cache
        except Exception as exc:  # noqa: BLE001
            log.warning("[bluefin] exchange info failed (%s); using static map", exc)
        self._markets_cache = sorted({v.split("-")[0].upper() for v in MARKET_SYMBOLS.values()})
        return list(self._markets_cache)

    def ids_id(self) -> str:
        """The 'idsId' (internal datastore id) required in signed fields."""
        if self._ids_id:
            return self._ids_id
        try:
            resp = self._req("GET", "/api/v1/exchange/info", public=True, market_api=True)
            data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            ids = data.get("idsId") or data.get("ids_id")
            if ids:
                self._ids_id = str(ids)
                return self._ids_id
        except Exception as exc:  # noqa: BLE001
            log.warning("[bluefin] idsId resolve failed: %s", exc)
        return "0"  # fallback: server may infer; logged for diagnosis

    # ---------------------------------------------------------------- orders

    def place_order(self, intent: OrderIntent, ref_price: float) -> dict:
        market = self.market(intent.symbol)
        if not market:
            return {"ok": False, "error": f"unsupported bluefin market {intent.symbol}"}
        price_e9 = "0" if intent.order_type == "market" else str(int(round(ref_price * E9)))
        qty_e9 = str(int(round(intent.qty * E9)))
        leverage_e9 = str(int(round(intent.leverage * E9)))
        salt = str(random.randint(1, 2**31))
        signed_at = int(time.time() * 1000)
        expires_at = signed_at + 30 * 24 * 3600 * 1000
        side = "SHORT" if intent.side == "sell" else "LONG"
        is_isolated = False
        ids = self.ids_id()
        account = self.address

        # Signed fields (exact SDK shape + key order).
        signed = {
            "symbol": market,
            "accountAddress": account,
            "priceE9": price_e9,
            "quantityE9": qty_e9,
            "side": side,
            "leverageE9": leverage_e9,
            "isIsolated": is_isolated,
            "salt": salt,
            "idsId": ids,
            "expiresAtMillis": expires_at,
            "signedAtMillis": signed_at,
        }
        sig = _order_signature(self.seed, self.pubkey, signed)

        order_type = "MARKET" if intent.order_type == "market" else "LIMIT"
        body = {
            "signedFields": signed,
            "signature": sig,
            "type": order_type,
            "reduceOnly": False,
            "postOnly": False,
            "timeInForce": None if intent.order_type == "market" else "GTT",
        }
        return self._req("POST", "/api/v1/trade/orders", json_body=body)

    # ---------------------------------------------------------------- state

    def positions(self) -> dict:
        return self._req("GET", f"/api/v1/accounts/positions/{self.address}", public=False)

    def open_orders(self) -> dict:
        return self._req("GET", "/api/v1/trade/openOrders", public=False)

    def cancel_all(self, symbol: str | None = None) -> dict:
        body = {}
        if symbol and self.market(symbol):
            body["symbol"] = self.market(symbol)
        return self._req("PUT", "/api/v1/trade/orders/cancel", json_body=body or None)

    # ---------------------------------------------------------------- killswitch hook

    def flat_and_cancel(self, bot_id: int) -> dict:
        """Bluefin perp killswitch: cancel all orders + close positions (market)."""
        result: dict[str, Any] = {"ok": True, "closed": [], "cancelled": False, "errors": []}
        cancel = self.cancel_all()
        result["cancelled"] = bool(cancel.get("ok"))
        if not cancel.get("ok"):
            result["errors"].append(f"cancel_all: {cancel.get('error')}")
            result["ok"] = False
        pos = self.positions()
        if not pos.get("ok"):
            result["errors"].append(f"positions: {pos.get('error')}")
            result["ok"] = False
            return result
        rows = pos.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("positions") or []
        for p in rows if isinstance(rows, list) else []:
            market = p.get("symbol") or p.get("market")
            qty = p.get("quantity") or p.get("position") or p.get("qty")
            if market and qty and float(qty) != 0:
                close = self.place_order(
                    OrderIntent(chain="sui", venue="bluefin-perp",
                                symbol=market.split("-")[0],
                                side="sell" if float(qty) > 0 else "buy",
                                qty=abs(float(qty)), order_type="market",
                                idempotency_key=f"kill-{bot_id}-{market}"),
                    ref_price=float(p.get("entryPrice") or p.get("markPrice") or 0) / E9 or 1.0,
                )
                result["closed"].append({market: close.get("ok")})
                if not close.get("ok"):
                    result["errors"].append(f"close {market}: {close.get('error')}")
                    result["ok"] = False
        return result


def build_bluefin(ledger: ExecLedger, keypair_hex: str, testnet: bool = True,
                  api_base: str | None = None) -> BluefinAdapter:
    """Factory: derive adapter from a Sui seed (raw hex or bech32 keystring)."""
    if keypair_hex.startswith("suiprivkey"):
        from pysui.sui.sui_crypto import SuiKeyPair
        kp = SuiKeyPair.from_bech32(keypair_hex)
        seed = bytes(kp.private_key.key_bytes)
        pub = bytes(kp.public_key.key_bytes)
        addr = "0x" + hashlib.blake2b(b"\x00" + pub, digest_size=32).hexdigest()
    else:
        hexed = keypair_hex[2:] if keypair_hex.startswith("0x") else keypair_hex
        seed = bytes.fromhex(hexed)
        if len(seed) != 32:
            raise ValueError(f"bluefin keypair_hex must be 32 bytes, got {len(seed)}")
        from sui_adapter import _ed25519_pubkey
        pub = _ed25519_pubkey(seed)
        addr = "0x" + hashlib.blake2b(b"\x00" + pub, digest_size=32).hexdigest()
    return BluefinAdapter(ledger, seed, pub, addr, testnet=testnet, api_base=api_base)
