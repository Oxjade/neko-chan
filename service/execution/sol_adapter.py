"""Solana real-trading adapter: Jupiter Perps, Jupiter Limit (Trigger V2), xStocks via Jupiter Swap.

Endpoint contracts (Jupiter official docs: https://developers.jup.ag/docs):

- Perps REST API is officially a work-in-progress (https://developers.jup.ag/docs/perps).
  This adapter implements the jup.ag v1 UI contract:
      GET  https://jup.ag/api/perp/positions?wallet=<ADDRESS>         -> open positions
      POST https://jup.ag/api/perp/orders   (order intent)            -> {transaction: b64} unsigned
  (two-tx request/keeper model: the unsigned tx is signed locally and broadcast to the RPC)
- Limit orders (OCO = take-profit/stop-loss): Trigger V2 (https://developers.jup.ag/docs/trigger):
      POST https://api.jup.ag/trigger/v2/auth/challenge | /auth/verify  -> JWT (wallet-signed)
      GET  https://api.jup.ag/trigger/v2/vault                          -> vault pubkey
      POST https://api.jup.ag/trigger/v2/deposit/craft                  -> {transaction, requestId}
      POST https://api.jup.ag/trigger/v2/orders/price                   -> {id, txSignature}
      POST https://api.jup.ag/trigger/v2/orders/price/cancel/{id} + /confirm-cancel/{id}
- Spot swaps (xStocks): legacy swap v1 contract (quote, build unsigned tx, sign + broadcast):
      GET  https://api.jup.ag/swap/v1/quote?inputMint=..&outputMint=..&amount=..
      POST https://api.jup.ag/swap/v1/swap {quoteResponse, userPublicKey} -> {swapTransaction: b64}
  xStocks mints (AAPLx etc.) resolve via GET https://api.jup.ag/tokens/v2/search?query=<SYMBOL>.
- RPC: JSON-RPC on mainnet-beta (devnet when testnet=True); unsigned txs are signed with the
  trading keypair (solders) and broadcast via sendTransaction.

Every public method is failure-tolerant: network errors become {"ok": False, ...} or 0.0/[].
"""

import base64
import logging
from datetime import datetime, timedelta, timezone

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from ledger import ExecLedger
from order_model import OrderIntent

log = logging.getLogger("execution")

MAINNET_RPC = "https://api.mainnet-beta.solana.com"
DEVNET_RPC = "https://api.devnet.solana.com"
JUP_API = "https://api.jup.ag"
TRIGGER_BASE = f"{JUP_API}/trigger/v2"
SWAP_QUOTE_URL = f"{JUP_API}/swap/v1/quote"
SWAP_SWAP_URL = f"{JUP_API}/swap/v1/swap"
TOKENS_SEARCH_URL = f"{JUP_API}/tokens/v2/search"
PERPS_BASE = "https://jup.ag/api/perp"
PERPS_POSITIONS_URL = f"{PERPS_BASE}/positions"
PERPS_ORDERS_URL = f"{PERPS_BASE}/orders"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
SOL_DECIMALS = 9
XSTOCK_DECIMALS_FALLBACK = 8
TIMEOUT = 20
SLIPPAGE_BPS = 50


class SOLAdapter:
    def __init__(self, ledger: ExecLedger, keypair_hex: str,
                 rpc_url: str = "https://api.mainnet-beta.solana.com", testnet: bool = False):
        self.ledger = ledger
        raw = bytes.fromhex(keypair_hex)
        if len(raw) == 64:
            self.keypair = Keypair.from_bytes(raw)
        elif len(raw) == 32:
            self.keypair = Keypair.from_seed(raw)
        else:
            raise ValueError(f"keypair_hex must decode to 32 or 64 bytes, got {len(raw)}")
        self.pubkey = str(self.keypair.pubkey())
        self.testnet = testnet
        self.rpc_url = DEVNET_RPC if testnet else rpc_url
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})

    # ---------------- http helpers ----------------

    def _request(self, method: str, url: str, **kw):
        kw.setdefault("timeout", TIMEOUT)
        resp = self.session.request(method, url, **kw)
        if resp.status_code >= 500:
            resp = self.session.request(method, url, **kw)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    @staticmethod
    def _bearer(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    # ---------------- balances ----------------

    def get_balance(self, asset: str) -> float:
        try:
            if asset.upper() in ("SOL", "WSOL"):
                return self._native_balance()
            if asset.upper() == "USDC":
                return self._token_balance(USDC_MINT)
            return 0.0
        except Exception as exc:  # noqa: BLE001
            log.warning("get_balance(%s) failed: %s", asset, exc)
            return 0.0

    def _native_balance(self) -> float:
        rpc = self._request("POST", self.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [self.pubkey],
        })
        return float(rpc["result"]["value"]) / 10 ** SOL_DECIMALS

    def _token_balance(self, mint: str) -> float:
        rpc = self._request("POST", self.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [self.pubkey, {"mint": mint}],
        })
        value = (rpc.get("result") or {}).get("value") or []
        total = 0.0
        for item in value:
            try:
                info = item["account"]["data"]["parsed"]["info"]["tokenAmount"]
                total += float(info.get("uiAmount") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
        return total

    # ---------------- positions ----------------

    def get_positions(self) -> list[dict]:
        try:
            resp = self._request("GET", PERPS_POSITIONS_URL, params={"wallet": self.pubkey})
        except Exception as exc:  # noqa: BLE001
            log.warning("get_positions failed: %s", exc)
            return []
        rows = resp.get("positions") if isinstance(resp, dict) else resp
        if not isinstance(rows, list):
            return []
        return [p for p in (self._normalize_position(r) for r in rows) if p]

    @staticmethod
    def _normalize_position(raw: dict) -> dict | None:
        side = str(raw.get("side") or raw.get("positionSide") or "").lower()
        if side in ("long", "buy"):
            side = "buy"
        elif side in ("short", "sell"):
            side = "sell"
        else:
            return None
        try:
            qty = float(raw.get("qty") or raw.get("size") or 0.0)
        except (TypeError, ValueError):
            return None
        if qty <= 0:
            return None

        def f(key: str) -> float | None:
            try:
                return float(raw.get(key)) if raw.get(key) is not None else None
            except (TypeError, ValueError):
                return None

        return {
            "symbol": raw.get("symbol") or raw.get("mint") or "?",
            "side": side,
            "qty": qty,
            "entry": f("entryPrice") or f("entry") or 0.0,
            "leverage": f("leverage") or 1.0,
            "liq_price": f("liquidationPrice") or f("liqPrice"),
            "pnl": f("pnl") or f("unrealizedPnl") or 0.0,
        }

    # ---------------- place order ----------------

    def place_order(self, intent: OrderIntent, ref_price: float) -> dict:
        try:
            errors = intent.validate(ref_price)
            if errors:
                return {"ok": False, "error": "; ".join(errors)}
            if intent.venue == "jup-perp":
                return self._place_jup_perp(intent, ref_price)
            if intent.venue == "jup-limit":
                return self._place_jup_limit(intent, ref_price)
            if intent.venue == "xstocks-spot":
                return self._place_xstocks(intent, ref_price)
            return {"ok": False, "error": f"venue {intent.venue} not supported by SOLAdapter"}
        except Exception as exc:  # noqa: BLE001
            log.error("place_order failed: %s", exc)
            return {"ok": False, "error": str(exc)[:300]}

    def _place_jup_perp(self, intent: OrderIntent, ref_price: float) -> dict:
        resp = self._request("POST", PERPS_ORDERS_URL, json={
            "wallet": self.pubkey,
            "symbol": intent.symbol,
            "side": intent.side,
            "size": intent.qty,
            "orderType": intent.order_type if intent.order_type in ("market", "limit") else "market",
            "leverage": intent.leverage,
            "price": intent.limit_price,
            "tpPrice": intent.take_profit,
            "slPrice": intent.stop_loss,
            "reduceOnly": False,
            "idempotencyKey": intent.idempotency_key,
        })
        tx = resp.get("transaction") or resp.get("tx") or resp.get("swapTransaction")
        if not tx:
            return {"ok": False, "error": f"perp open returned no transaction: {resp}"[:300]}
        return self._broadcast_result(tx)

    def _place_jup_limit(self, intent: OrderIntent, ref_price: float) -> dict:
        info = self._resolve_mint(intent.symbol)
        if not info:
            return {"ok": False, "error": f"could not resolve mint for {intent.symbol}"}
        token = self._trigger_auth()
        if not token:
            return {"ok": False, "error": "trigger auth failed"}
        vault = self._trigger_vault(token)
        if not vault:
            return {"ok": False, "error": "trigger vault not available"}
        if intent.side == "buy":
            input_mint, output_mint = USDC_MINT, info["mint"]
            deposit_amount = int(intent.qty * ref_price * 10 ** USDC_DECIMALS)
            condition = "<"
        else:
            input_mint, output_mint = info["mint"], USDC_MINT
            deposit_amount = int(intent.qty * 10 ** info["decimals"])
            condition = ">"
        if deposit_amount <= 0:
            return {"ok": False, "error": "deposit amount too small"}
        if intent.order_type == "limit" and intent.limit_price:
            trigger_price = intent.limit_price
        else:
            trigger_price = ref_price * (0.997 if intent.side == "buy" else 1.003)
        sub_type = "oco" if (intent.take_profit and intent.stop_loss) else "single"
        craft = self._request("POST", f"{TRIGGER_BASE}/deposit/craft", json={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "userAddress": self.pubkey,
            "amount": deposit_amount,
            "orderType": "price",
            "orderSubType": sub_type,
        }, headers=self._bearer(token))
        deposit_tx = craft.get("transaction")
        request_id = craft.get("requestId")
        if not deposit_tx or not request_id:
            return {"ok": False, "error": "deposit craft incomplete"}
        deposit_signed = self._sign_tx(deposit_tx)
        try:
            self._broadcast(deposit_signed)
        except Exception as exc:  # noqa: BLE001
            log.warning("deposit broadcast failed (create may still land): %s", exc)
        create = self._request("POST", f"{TRIGGER_BASE}/orders/price", json={
            "vaultPubkey": vault,
            "depositRequestId": request_id,
            "depositSignedTx": deposit_signed,
            "orderType": "price",
            "orderSubType": sub_type,
            "triggerMint": info["mint"],
            "triggerCondition": condition,
            "triggerPriceUsd": round(trigger_price, 6),
            "tpPriceUsd": intent.take_profit,
            "slPriceUsd": intent.stop_loss,
            "slippageBps": SLIPPAGE_BPS,
            "expiresAt": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        }, headers=self._bearer(token))
        order_id = create.get("id")
        if not order_id:
            return {"ok": False, "error": f"trigger create returned no id: {create}"[:300]}
        return {"ok": True, "venue_order_id": str(order_id), "tx_hash": create.get("txSignature")}

    def _place_xstocks(self, intent: OrderIntent, ref_price: float) -> dict:
        info = self._resolve_mint(intent.symbol)
        if not info:
            return {"ok": False, "error": f"could not resolve xStocks token {intent.symbol}"}
        if intent.side == "buy":
            input_mint, output_mint = USDC_MINT, info["mint"]
            amount = int(intent.qty * ref_price * 10 ** USDC_DECIMALS)
        else:
            input_mint, output_mint = info["mint"], USDC_MINT
            amount = int(intent.qty * 10 ** info["decimals"])
        if amount <= 0:
            return {"ok": False, "error": "swap amount too small"}
        quote = self._request("GET", SWAP_QUOTE_URL, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
            "slippageBps": SLIPPAGE_BPS,
        })
        build = self._request("POST", SWAP_SWAP_URL, json={
            "quoteResponse": quote,
            "userPublicKey": self.pubkey,
            "wrapAndUnwrapSol": True,
        })
        tx = build.get("swapTransaction")
        if not tx:
            return {"ok": False, "error": "swap build returned no swapTransaction"}
        return self._broadcast_result(tx)

    def _resolve_mint(self, symbol: str) -> dict | None:
        if len(symbol) in (43, 44) and symbol.isalnum():
            return {"mint": symbol, "decimals": XSTOCK_DECIMALS_FALLBACK}
        try:
            resp = self._request("GET", TOKENS_SEARCH_URL, params={"query": symbol})
            for row in (resp or {}).get("results", []) or []:
                if row.get("symbol", "").upper() == symbol.upper() or row.get("address", "") == symbol:
                    return {
                        "mint": row["address"],
                        "decimals": int(row.get("decimals") or XSTOCK_DECIMALS_FALLBACK),
                    }
        except Exception as exc:  # noqa: BLE001
            log.warning("mint resolution failed for %s: %s", symbol, exc)
        return None

    # ---------------- cancel / flatten ----------------

    def cancel_all(self, bot_id: int) -> dict:
        result = {"ok": True, "limit_cancelled": [], "errors": []}
        token = self._trigger_auth()
        if not token:
            result["errors"].append("trigger auth failed - limit orders not cancelled")
        else:
            try:
                orders = self._trigger_active_orders(token)
            except Exception as exc:  # noqa: BLE001
                orders = []
                result["errors"].append(f"limit history: {exc}")
            for order in orders:
                try:
                    ok = self._trigger_cancel(order, token)
                    result["limit_cancelled"].append({"id": order.get("id"), "ok": ok})
                    if not ok:
                        result["errors"].append(f"cancel failed for order {order.get('id')}")
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(f"cancel {order.get('id')}: {exc}")
        if result["errors"]:
            result["ok"] = False
        return result

    def flat_and_cancel(self, bot_id: int) -> dict:
        result = {"ok": True, "flat": [], "cancel": {}}
        try:
            result["flat"] = self._perp_close_all()
            if [c for c in result["flat"] if not c.get("ok")]:
                result["ok"] = False
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["error"] = f"perp close-all: {exc}"[:300]
        try:
            result["cancel"] = self.cancel_all(bot_id)
            if not result["cancel"].get("ok"):
                result["ok"] = False
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["error"] = f"cancel_all: {exc}"[:300]
        return result

    def _perp_close_all(self) -> list[dict]:
        return [self._perp_close_position(pos) for pos in self.get_positions()]

    def _perp_close_position(self, pos: dict) -> dict:
        resp = self._request("POST", PERPS_ORDERS_URL, json={
            "wallet": self.pubkey,
            "symbol": pos["symbol"],
            "side": "sell" if pos["side"] == "buy" else "buy",
            "size": pos["qty"],
            "orderType": "market",
            "reduceOnly": True,
            "leverage": pos.get("leverage") or 1.0,
        })
        tx = resp.get("transaction") or resp.get("tx") or resp.get("swapTransaction")
        if not tx:
            return {"symbol": pos["symbol"], "ok": False, "error": "no transaction returned"}
        res = self._broadcast_result(tx)
        res["symbol"] = pos["symbol"]
        return res

    def _trigger_active_orders(self, token: str) -> list[dict]:
        resp = self._request("GET", f"{TRIGGER_BASE}/orders/history",
                             params={"state": "active"}, headers=self._bearer(token))
        orders = resp.get("orders") if isinstance(resp, dict) else resp
        return orders if isinstance(orders, list) else []

    def _trigger_cancel(self, order: dict, token: str) -> bool:
        order_id = order.get("id")
        if not order_id:
            return False
        resp = self._request("POST", f"{TRIGGER_BASE}/orders/price/cancel/{order_id}",
                             headers=self._bearer(token))
        tx = resp.get("transaction")
        if tx:
            signed = self._sign_tx(tx)
            self._broadcast(signed)
            self._request("POST", f"{TRIGGER_BASE}/orders/price/confirm-cancel/{order_id}",
                          json={"signedTransaction": signed}, headers=self._bearer(token))
        return bool(resp.get("cancelRequestId") or resp.get("status") or tx)

    # ---------------- trigger auth ----------------

    def _trigger_auth(self) -> str | None:
        try:
            challenge = self._request("POST", f"{TRIGGER_BASE}/auth/challenge",
                                      json={"wallet": self.pubkey})
            message = challenge.get("message") or challenge.get("challenge")
            if not message:
                log.warning("trigger challenge missing message")
                return None
            try:
                msg_bytes = base64.b64decode(message, validate=True)
            except Exception:  # noqa: BLE001
                msg_bytes = message.encode()
            sig = self.keypair.sign_message(msg_bytes)
            verify = self._request("POST", f"{TRIGGER_BASE}/auth/verify", json={
                "wallet": self.pubkey,
                "signed_message": base64.b64encode(bytes(sig)).decode(),
            })
            token = verify.get("token") or verify.get("accessToken")
            if not token:
                log.warning("trigger verify returned no token")
                return None
            return str(token)
        except Exception as exc:  # noqa: BLE001
            log.warning("trigger auth failed: %s", exc)
            return None

    def _trigger_vault(self, token: str) -> str | None:
        try:
            resp = self._request("GET", f"{TRIGGER_BASE}/vault", headers=self._bearer(token))
            return resp.get("vaultPubkey") or resp.get("vault")
        except Exception as exc:  # noqa: BLE001
            log.warning("trigger vault lookup failed: %s", exc)
        try:
            resp = self._request("POST", f"{TRIGGER_BASE}/vault/register",
                                 json={"wallet": self.pubkey}, headers=self._bearer(token))
            return resp.get("vaultPubkey") or resp.get("vault")
        except Exception as exc:  # noqa: BLE001
            log.warning("trigger vault register failed: %s", exc)
        return None

    # ---------------- sign + broadcast ----------------

    def _sign_tx(self, tx_b64: str) -> str:
        raw = base64.b64decode(tx_b64)
        msg = VersionedTransaction.from_bytes(raw).message
        sig = self.keypair.sign_message(bytes(msg))
        signed = VersionedTransaction.populate(msg, [sig])
        return base64.b64encode(bytes(signed)).decode()

    def _broadcast(self, signed_b64: str) -> str:
        rpc = self._request("POST", self.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}],
        })
        if rpc.get("error"):
            raise RuntimeError(f"sendTransaction error: {rpc['error']}")
        return str(rpc.get("result") or "")

    def _sign_and_broadcast(self, tx_b64: str) -> str:
        return self._broadcast(self._sign_tx(tx_b64))

    def _broadcast_result(self, tx_b64: str) -> dict:
        try:
            sig = self._sign_and_broadcast(tx_b64)
            return {"ok": True, "tx_hash": sig}
        except Exception as exc:  # noqa: BLE001
            log.error("broadcast failed: %s", exc)
            return {"ok": False, "error": f"broadcast failed: {exc}"[:300]}