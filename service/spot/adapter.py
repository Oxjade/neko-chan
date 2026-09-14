"""Aftermath SPOT adapter: router swaps + resting spot limit orders.

API surface (Aftermath REST, /api prefix, docs 2026-09):
  Router (instant swaps):
    GET  /api/router/supported-coins          -> flat list of tradable coin types (~92k, cache it)
    POST /api/router/trade-route              -> {coinInType, coinOutType, coinInAmount|coinOutAmount, slippage}
    POST /api/router/transactions/trade       -> serialized PTB (preferred over add-trade for standalone swaps)

  Spot limit orders (resting, on-chain, venue-native stop-loss field):
    POST /api/limit-orders/transactions/create-order
         {walletAddress, allocateCoinType, allocateCoinAmount, buyCoinType,
          expiryDurationMs, outputToInputExchangeRate, isSponsoredTx,
          integratorFee?: {feeRecipient, feeBps},
          outputToInputStopLossExchangeRate?}
         -> bare JSON string (serialized tx); sign + broadcast via Sui
    POST /api/limit-orders/active   {walletAddress, bytes, signature}
    POST /api/limit-orders/past     {walletAddress}
    POST /api/limit-orders/cancel   {walletAddress, bytes, signature, orderObjectIds}
    POST /api/limit-orders/min-order-size-usd  (bodyless)

Auth for signed actions: sign the EXACT bytes of b"Aftermath Terms and
Conditions" with the wallet Ed25519 key (reusable terms signature) and send
{walletAddress, bytes (base64), signature (base64)}.

Gas: spot txs are Sui PTBs paid from the wallet's SUI (same wallet as perps).
Dynamic Gas sponsorship exists (/api/dynamic-gas) if a user has USDC but no
SUI - not wired initially.

Fee (platform): integratorFee is native on limit orders (feeBps capped by the
account's builder-code config). Router swaps have no integrator field - the
platform fee is collected by the existing post-fill USDC sweep.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger("spot")

API_MAINNET = "https://aftermath.finance/api"
TERMS_BYTES = b"Aftermath Terms and Conditions"

# Cache supported-coins hard: the response is ~7.8 MB (92k entries). Refresh
# at most once per day; the router universe rarely changes.
_COINS_CACHE: dict[str, tuple[float, set[str]]] = {}
COINS_CACHE_TTL = 24 * 3600


class AftermathSpotAdapter:
    """One adapter instance per bot wallet (same Sui key as perps)."""

    def __init__(self, keypair_bech32: str, network: str = "mainnet",
                 api_base: str | None = None):
        self.network = (network or "mainnet").strip().lower()
        self.api = (api_base or API_MAINNET).rstrip("/")
        self._keypair_bech32 = keypair_bech32
        self._address: str | None = None  # derived lazily via sui_adapter helper

    # ------------------------------------------------------------- keys
    @property
    def address(self) -> str:
        if self._address is None:
            from execution.sui_adapter import SUIAdapter  # shared key plumbing
            from execution.exec_vault import ExecVault
            # Reuse the same ED25519 derivation as the perps stack: address =
            # blake2b-256(0x00 || pubkey). We resolve the address through a
            # throwaway adapter built from the same bech32 key.
            raise NotImplementedError(
                "address derivation wired in gateway wiring step (needs the "
                "same SUIAdapter key import the perps adapter uses)")
        return self._address

    # ------------------------------------------------------------- signing
    def _sign_terms(self) -> tuple[str, str]:
        """Reusable terms signature: base64(bytes) + base64(Ed25519 sig)."""
        from execution.sui_adapter import _ed25519_sign
        seed = self._seed_from_bech32()
        sig = _ed25519_sign(seed, TERMS_BYTES)
        return (base64.b64encode(TERMS_BYTES).decode(),
                base64.b64encode(bytes(sig)).decode())

    def _seed_from_bech32(self) -> bytes:
        """Decode suiprivkey1... -> 32-byte seed (flag byte stripped)."""
        import base58  # pysui dependency, already installed for perps
        from execution.sui_adapter import BECH32_CONST  # noqa: F401 (avail check)
        raw = base58.b58decode(self._keypair_bech32)
        # bech32 payload: 0x00 flag || 32-byte seed (Sui private-key format)
        return raw[1:33]

    # ------------------------------------------------------------- router
    def supported_coins(self) -> set[str]:
        """Coin types tradeable through the router (cached 24h)."""
        now = time.time()
        hit = _COINS_CACHE.get(self.network)
        if hit and now - hit[0] < COINS_CACHE_TTL:
            return hit[1]
        r = requests.get(f"{self.api}/router/supported-coins", timeout=30)
        r.raise_for_status()
        coins = set(r.json() or [])
        _COINS_CACHE[self.network] = (now, coins)
        return coins

    def quote_route(self, coin_in_type: str, coin_out_type: str,
                    amount_in_atoms: Optional[int] = None,
                    amount_out_atoms: Optional[int] = None,
                    slippage_bps: int = 100,
                    protocols_whitelist: Optional[list] = None,
                    external_fee: Optional[dict] = None) -> dict:
        """Best route quote. EXACTLY one of the atom amounts; coin args are FULL
        Move type strings (64-hex padded) — symbols are rejected ("Coin not
        found"). Wire format verified live 2026-09-14 against
        /api/router/trade-route: amounts are plain BigInt atom strings, slippage
        is a 0..1 fraction. routes[].paths[] expose protocolName/poolId so
        callers can SEE the venue (Cetus included — suipump-graduated tokens
        route through their graduation pool)."""
        body: dict = {"coinInType": coin_in_type, "coinOutType": coin_out_type}
        if amount_in_atoms is not None:
            body["coinInAmount"] = str(int(amount_in_atoms))
        elif amount_out_atoms is not None:
            body["coinOutAmount"] = str(int(amount_out_atoms))
        else:
            raise ValueError("quote_route needs exactly one of amount_in/amount_out")
        if slippage_bps:
            body["slippage"] = slippage_bps / 10000.0
        if protocols_whitelist:
            body["protocolWhitelist"] = protocols_whitelist
        if external_fee:
            # {recipient, feePercentage} — Aftermath pays it out of the route's
            # output INSIDE the swap tx (atomic integrator fee).
            body["externalFee"] = external_fee
        r = requests.post(f"{self.api}/router/trade-route", json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def route_venues(quote: dict) -> list[tuple]:
        """[(protocolName, poolId, coinOut atoms)] per leg of a route quote."""
        out = []
        for rt in (quote or {}).get("routes") or []:
            for pth in rt.get("paths") or []:
                meta = pth.get("poolMetadata") or {}
                co = int((pth.get("coinOut") or {}).get("amount", "0").rstrip("n") or 0)
                out.append(((meta.get("tbData") or {}).get("protocol")
                            or pth.get("protocolName") or "?",
                            pth.get("poolId") or "", co))
        return out

    def swap_tx_b64(self, complete_route: dict, wallet_address: str,
                    slippage_bps: int = 100) -> str:
        """POST /router/v1/transactions/trade -> base64 TransactionKind for the
        quoted route. Server checks the wallet's input-coin balance, so this
        only succeeds with the REAL funded signer. Sign+broadcast of the
        returned kind is the remaining P1 wrap step (docs §3.5)."""
        body = {"completeRoute": complete_route, "walletAddress": wallet_address,
                "slippage": slippage_bps / 10000.0, "isSponsoredTx": False}
        r = requests.post(f"{self.api}/router/v1/transactions/trade",
                          json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def swap_tx(self, route_complete: str) -> str:
        """Serialized PTB for a quoted route (execute on-chain, self-custody)."""
        # transactions/trade lacks a documented schema (docs 2026-09) - the
        # documented flow is transactions/add-trade composing into an existing
        # PTB. Standalone swap: build the tx via the SDK-equivalent composition
        # in the gateway wiring step; kept as an explicit TODO until tested.
        raise NotImplementedError("swap tx composition - wire in gateway step")

    # ------------------------------------------------------------- limits
    def create_limit_order_tx(self, allocate_coin_type: str, allocate_amount: float,
                              buy_coin_type: str, rate: float,
                              expiry_ms: int = 24 * 3600 * 1000,
                              fee_recipient: str | None = None,
                              fee_bps: int | None = None,
                              stop_rate: float | None = None,
                              decimals: int = 6) -> str:
        """Serialized tx placing a resting spot limit order. Sign + broadcast."""
        body: dict = {
            "walletAddress": self.address,
            "allocateCoinType": allocate_coin_type,
            "allocateCoinAmount": f"{int(round(allocate_amount * 10**decimals))}n",
            "buyCoinType": buy_coin_type,
            "expiryDurationMs": expiry_ms,
            "outputToInputExchangeRate": rate,
            "isSponsoredTx": False,
        }
        if fee_recipient and fee_bps:
            body["integratorFee"] = {"feeRecipient": fee_recipient, "feeBps": fee_bps}
        if stop_rate:
            body["outputToInputStopLossExchangeRate"] = stop_rate
        r = requests.post(f"{self.api}/limit-orders/transactions/create-order",
                          json=body, timeout=30)
        r.raise_for_status()
        return r.json()  # bare serialized transaction string

    def active_limit_orders(self) -> list[dict]:
        b64_bytes, b64_sig = self._sign_terms()
        r = requests.post(f"{self.api}/limit-orders/active",
                          json={"walletAddress": self.address,
                                "bytes": b64_bytes, "signature": b64_sig},
                          timeout=30)
        r.raise_for_status()
        return r.json() or []

    def past_limit_orders(self) -> list[dict]:
        r = requests.post(f"{self.api}/limit-orders/past",
                          json={"walletAddress": self.address}, timeout=30)
        r.raise_for_status()
        return r.json() or []

    def cancel_limit_orders(self, order_ids: list[str]) -> bool:
        b64_bytes, b64_sig = self._sign_terms()
        r = requests.post(f"{self.api}/limit-orders/cancel",
                          json={"walletAddress": self.address,
                                "bytes": b64_bytes, "signature": b64_sig,
                                "orderObjectIds": order_ids},
                          timeout=30)
        r.raise_for_status()
        return bool(r.json())

    @staticmethod
    def min_order_size_usd() -> float:
        r = requests.post(f"{API_MAINNET}/limit-orders/min-order-size-usd",
                          json={}, timeout=15)
        r.raise_for_status()
        return float(r.json())
