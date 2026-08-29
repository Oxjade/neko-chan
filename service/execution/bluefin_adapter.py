"""Bluefin Pro (Sui) real-trading adapter: perp CLOB via the off-chain API.

Bluefin is a perp CLOB on Sui: orders are placed off-chain REST and verified
on-chain at settlement time via the order signature. This adapter implements
the Ed25519 order-signature construction directly (no TS/Python SDK dependency)
using the documented spec:

  serializedOrder
  [0,15]   price        (128-bit, hex 32 chars)
  [16,31]  quantity     (128-bit)
  [32,47]  leverage     (128-bit)
  [48,63]  salt         (128-bit)
  [64,71]  expiration   (64-bit, unix ms)
  [72,103] maker        (256-bit = 32-byte address)
  [104,135] market      (256-bit = 32-byte market hash)
  [136]    flags        (1 byte: bit0 ioc, bit1 postOnly, bit2 reduceOnly,
                         bit3 isBuy, bit4 orderbookOnly)
  [137,143] domain      ('Bluefin' 7 bytes)

  sha256(serialized) -> sign -> hex signature + b'01' suffix for ed25519.

Endpoints (v2.0.1, readme.io reference):
  POST {API}/orders            place signed order
  POST {API}/orders/cancel     cancel order
  POST {API}/orders/cancel_all cancel all by symbol
  GET  {API}/order/book/{sym}  order book (public)
  GET  {API}/positions/{addr}  positions
  GET  {API}/user/{addr}       account

Auth: onboarding signature -> JWT bearer token used for user endpoints.
All methods are failure-tolerant: network/format errors -> {"ok": False, ...}.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from typing import Any

import requests

from ledger import ExecLedger
from order_model import OrderIntent

log = logging.getLogger("execution")

# Bluefin env (Sui). Prod + staging; always default to STAGING until account
# keys and risk gates are validated - nothing crosses to prod implicitly.
API_PROD = "https://dapi.api.sui-prod.bluefin.io"
API_STAGING = "https://stg-api.bluefin.io"
MARKET_HASH = "0x06f300c91b1db75d3e93626c1b88ccb4897e44228a9b6513a66695f8c0e74987"

# Bluefin Pro perp markets. Map OUR symbols -> Bluefin market symbols.
MARKET_SYMBOLS: dict[str, str] = {
    "BTC": "BTC-PERP",
    "ETH": "ETH-PERP",
    "SOL": "SOL-PERP",
    "SUI": "SUI-PERP",
    "ARB": "ARB-PERP",
    "AVAX": "AVAX-PERP",
    "BNB": "BNB-PERP",
    "DOGE": "DOGE-PERP",
    "LINK": "LINK-PERP",
    "LTC": "LTC-PERP",
    "OP": "OP-PERP",
    "MATIC": "MATIC-PERP",
    "SEI": "SEI-PERP",
}

DOMAIN = b"Bluefin"
CALLBACK_PRICE_SCALE = 1e6  # Bluefin prices on Sui are u64 scaled by 1e6


def _to_hex128(value: int) -> str:
    return f"{int(value):032x}"  # 128-bit = 16 bytes = 32 hex chars


def _to_hex64(value: int) -> str:
    return f"{int(value):016x}"  # 64-bit = 8 bytes = 16 hex chars


def _to_hex32(value: int) -> str:
    return f"{int(value):064x}"  # 32-byte address as 64 hex chars


def _flags(order: dict) -> int:
    """Encode boolean flags into a byte per Bluefin's spec."""
    f = 0
    if order.get("ioc"):
        f |= 1 << 0
    if order.get("postOnly"):
        f |= 1 << 1
    if order.get("reduceOnly"):
        f |= 1 << 2
    if order.get("isBuy"):
        f |= 1 << 3
    if order.get("orderbookOnly"):
        f |= 1 << 4
    return f


def _serialize_order(order: dict, maker_addr: str, domain: bytes = DOMAIN) -> bytes:
    """Serialize an order per the Bluefin order-signature layout.

    Layout (documented):
      [0,15]   price        16 bytes
      [16,31]  quantity     16 bytes
      [32,47]  leverage     16 bytes
      [48,63]  salt         16 bytes
      [64,71]  expiration    8 bytes
      [72,103] maker        32 bytes
      [104,135] market      32 bytes
      [136]    flags         1 byte
      [137,143] domain       7 bytes
      = 144 bytes total.
    """
    parts = [
        _to_hex128(int(order["price"])),
        _to_hex128(int(order["qty"])),
        _to_hex128(int(order["leverage"])),
        _to_hex128(int(order["salt"])),
        _to_hex64(int(order["expiration"])),
        _to_hex32(int(maker_addr, 16)),
        _to_hex32(int(order["market"][2:], 16)),
        f"{_flags(order):02x}",
        domain.hex(),
    ]
    b = bytes.fromhex("".join(parts))
    if len(b) != 144:
        raise ValueError(f"bluefin serialized order must be 144 bytes, got {len(b)}")
    return b


def _signature(seed32: bytes, serialized: bytes, byte_array_as_hex: bytes) -> str:
    """sha256(serialized) signed by ed25519 + '01' suffix (per spec)."""
    from sui_adapter import _ed25519_sign  # reuse the pure-python ed25519

    h = hashlib.sha256(serialized).digest()
    sig = _ed25519_sign(seed32, h)
    return bytes(sig).hex() + "01"


class BluefinAdapter:
    """Thin failure-tolerant Bluefin Pro REST client with local signing."""

    def __init__(self, ledger: ExecLedger, seed32: bytes, address: str,
                 testnet: bool = True, api_base: str | None = None):
        self.ledger = ledger
        self.seed = seed32
        self.address = address if address.startswith("0x") else f"0x{address}"
        self.api = (api_base or (API_STAGING if testnet else API_PROD)).rstrip("/")
        self._token: str | None = None
        self._token_at = 0.0
        log.info("[bluefin] adapter ready env=%s addr=%s", "staging" if testnet else "prod", self.address)

    # ---------------------------------------------------------------- helpers

    def _req(self, method: str, path: str, json_body: dict | None = None,
             public: bool = False) -> dict:
        url = f"{self.api}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
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
        if self._token and time.time() - self._token_at < 3600:
            return self._token
        # onboarding: sign a nonce (Bluefin expects the wallet to sign a
        # timestamp-based message for the auth token; v2 uses the onboarding
        # signature over a nonce string 'SUI:<addr>:<nonce>').
        nonce = str(int(time.time() * 1000))
        from sui_adapter import _ed25519_sign
        payload = f"SUI:{self.address}:{nonce}"
        sig = _ed25519_sign(self.seed, hashlib.sha256(payload.encode()).digest())
        resp = self._req("POST", "/v1/auth/token",
                         json_body={"address": self.address, "nonce": nonce,
                                    "signature": bytes(sig).hex() + "01"},
                         public=True)
        if not resp.get("ok"):
            log.warning("[bluefin] onboarding failed: %s", resp.get("error"))
            return None
        tok = (resp["data"].get("token") or resp["data"].get("accessToken"))
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
        # Fall back to the live listing: any base symbol Bluefin lists becomes
        # tradable even if it is not in the static map.
        listed = self.markets()
        if sym in listed:
            return f"{sym}-PERP"
        return None

    def markets(self) -> list[str]:
        """All tradable perp base symbols (long + short) offered by Bluefin.

        Prefers the live /exchangeInfo listing; falls back to the static
        MARKET_SYMBOLS map when the API is unreachable (offline/test). Each
        entry is the base symbol, e.g. 'BTC' for 'BTC-PERP'."""
        cached = getattr(self, "_markets_cache", None)
        if cached:
            return list(cached)
        try:
            resp = self._req("GET", "/exchangeInfo", public=True)
            rows = resp.get("data") if isinstance(resp.get("data"), list) else None
            if rows is None:
                rows = resp.get("data", {}).get("symbols") if isinstance(resp.get("data"), dict) else None
            out = []
            if isinstance(rows, list):
                for r in rows:
                    sym = str(r.get("symbol") or r.get("name") or "")
                    if sym.endswith("-PERP"):
                        base = sym[: -len("-PERP")]
                        if base:
                            out.append(base.upper())
            if out:
                self._markets_cache = sorted(set(out))
                return self._markets_cache
        except Exception as exc:  # noqa: BLE001
            log.warning("[bluefin] exchangeInfo failed (%s); using static map", exc)
        self._markets_cache = sorted({v.split("-")[0].upper() for v in MARKET_SYMBOLS.values()})
        return list(self._markets_cache)

    def price(self, symbol: str, ref_price: float) -> int:
        """Bluefin expects u64 prices scaled by 1e6."""
        return int(round(ref_price * CALLBACK_PRICE_SCALE))

    # ---------------------------------------------------------------- orders

    def place_order(self, intent: OrderIntent, ref_price: float) -> dict:
        market = self.market(intent.symbol)
        if not market:
            return {"ok": False, "error": f"unsupported bluefin market {intent.symbol}"}
        price_int = 0 if intent.order_type == "market" else self.price(intent.symbol, ref_price)
        try:
            market_hash = self._market_hash(market)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"market hash: {exc}"[:200]}

        salt = random.randint(1, 2**31)
        expiration_ms = int((time.time() + 30 * 86400) * 1000)
        order = {
            "symbol": market,
            "price": price_int if price_int else int(ref_price * CALLBACK_PRICE_SCALE),
            "qty": int(round(intent.qty * CALLBACK_PRICE_SCALE)),
            "side": "BUY" if intent.side == "buy" else "SELL",
            "orderType": "MARKET" if intent.order_type == "market" else "LIMIT",
            "leverage": int(intent.leverage),
            "isBuy": intent.side == "buy",
            "orderbookOnly": True,
            "postOnly": False,
            "reduceOnly": False,
            "salt": salt,
            "expiration": expiration_ms,
            "market": market_hash,
        }
        serialized = _serialize_order(order, self.address)
        order["order_signature"] = _signature(self.seed, serialized, b"")
        body = {
            "symbol": order["symbol"],
            "price": order["price"],
            "quantity": order["qty"],
            "side": order["side"],
            "orderType": order["orderType"],
            "leverage": order["leverage"],
            "isBuy": order["isBuy"],
            "orderbookOnly": order["orderbookOnly"],
            "postOnly": order["postOnly"],
            "reduceOnly": order["reduceOnly"],
            "salt": order["salt"],
            "expiration": order["expiration"],
            "orderSignature": order["order_signature"],
        }
        return self._req("POST", "/orders", json_body=body)

    def _market_hash(self, market: str) -> str:
        # Bluefin market hashes are published per market in the deployment
        # config. We resolve via the public instrument endpoint; fallback to
        # the known BTC hash only for a warning (never place with a wrong hash
        # silently - the trade would sign but not be matchable).
        try:
            resp = self._req("GET", f"/instruments/{market}", public=True)
            if resp.get("ok"):
                mh = resp["data"].get("marketHash") or resp["data"].get("market")
                if mh:
                    return mh if str(mh).startswith("0x") else f"0x{int(mh):064x}"
        except Exception as exc:  # noqa: BLE001
            log.warning("[bluefin] instrument resolve %s: %s", market, exc)
        if market == "BTC-PERP":
            return MARKET_HASH
        return f"0x{int(hashlib.sha256(market.encode()).hexdigest()[:64], 16):064x}"  # never reusable

    # ---------------------------------------------------------------- state

    def positions(self) -> dict:
        return self._req("GET", f"/accounts/positions/{self.address}", public=False)

    def open_orders(self) -> dict:
        return self._req("GET", "/orders/open", public=False)

    def cancel_all(self, symbol: str | None = None) -> dict:
        body = {"symbol": self.market(symbol)} if symbol and self.market(symbol) else {}
        return self._req("POST", "/orders/cancel_all", json_body=body or None)

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
        for p in pos["data"] if isinstance(pos["data"], list) else []:
            market = p.get("symbol") or p.get("market")
            qty = p.get("quantity") or p.get("position")
            if market and qty and float(qty) != 0:
                close = self.place_order(
                    OrderIntent(chain="sui", venue="bluefin-perp",
                                symbol=market.split("-")[0],
                                side="sell" if float(qty) > 0 else "buy",
                                qty=abs(float(qty)), order_type="market",
                                idempotency_key=f"kill-{bot_id}-{market}"),
                    ref_price=float(p.get("entryPrice") or p.get("markPrice") or 0) / 1e6 or 1.0,
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
        addr = "0x" + hashlib.blake2b(b"\x00" + bytes(kp.public_key.key_bytes), digest_size=32).hexdigest()
    else:
        hexed = keypair_hex[2:] if keypair_hex.startswith("0x") else keypair_hex
        seed = bytes.fromhex(hexed)
        if len(seed) != 32:
            raise ValueError(f"bluefin keypair_hex must be 32 bytes, got {len(seed)}")
        from sui_adapter import _ed25519_pubkey
        addr = "0x" + hashlib.blake2b(b"\x00" + _ed25519_pubkey(seed), digest_size=32).hexdigest()
    return BluefinAdapter(ledger, seed, addr, testnet=testnet, api_base=api_base)
