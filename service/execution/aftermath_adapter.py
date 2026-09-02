"""Aftermath Perps (Sui) real-trading adapter: perp CLOB via the CCXT API.

Aftermath Perpetuals is a fully on-chain perpetual futures exchange on Sui.
Unlike Bluefin (off-chain order book), Aftermath executes all orders, cancellations,
and liquidations transparently on-chain via Sui Programmable Transaction Blocks.

This adapter uses the CCXT API layer (build -> sign -> submit) for writes and
the native REST API for reads. The key flow:

  Order placement (CCXT build/sign/submit):
    1. POST /api/ccxt/build/createOrders -> {transactionBytes, signingDigest}
    2. Sign signingDigest with Ed25519 (Sui wallet key)
    3. POST /api/ccxt/submit/createOrders -> {signatures: [base64 UserSignature]}

  Reads (native REST):
    - GET /api/ccxt/markets             -> market list
    - POST /api/ccxt/positions           -> open positions
    - POST /api/ccxt/accounts            -> account capabilities
    - POST /api/perpetuals/accounts/owned -> account caps

  Authentication:
    - CCXT writes: the Sui wallet key signs the transaction signing digest
    - Native reads: no auth for public data; reusable terms signature for
      authenticated reads (stop orders, etc.)
    - Account creation: POST /api/ccxt/build/createAccount -> sign -> submit
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from typing import Any

import requests

from ledger import ExecLedger
from order_model import OrderIntent

log = logging.getLogger("execution")

# Aftermath API bases by network.
#   mainnet (production): https://aftermath.finance
#   testnet:              https://testnet.aftermath.finance
#   devnet:               NOT AVAILABLE (Aftermath runs mainnet + testnet only)
API_MAINNET = "https://aftermath.finance/api"
API_TESTNET = "https://testnet.aftermath.finance/api"

# Default USDC coin type on Sui mainnet (Aftermath mainnet settleId).
USDC_MAINNET_COIN_TYPE = "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
# Aftermath TESTNET settleId USDC (what the perp account settles in and what
# deposits must be). This is NOT the same as the generic testnet faucet USDC
# (0xa1ec7fc0...::usdc::USDC) - sending the wrong type leaves the account
# unfundable. Verified from Market.settleId on testnet, 2026-09-01.
USDC_TESTNET_COIN_TYPE = "0xcdd397f2cffb7f5d439f56fc01afe5585c5f06e3bcd2ee3a21753c566de313d9::usdc::USDC"

# RPC timeout for API calls.
RPC_TIMEOUT = 20

# Aftermath perp market symbols (base -> symbol mapping, populated from /api/ccxt/markets).
# Static fallback used when the API is unreachable. MAINNET list (verified from
# /api/perpetuals/all-markets, 2026-09-01): BTC/ETH/SOL/XAUT 20x, all others 5-10x.
MARKET_SYMBOLS: dict[str, str] = {
    "BTC": "BTC/USD:USDC", "ETH": "ETH/USD:USDC", "SOL": "SOL/USD:USDC",
    "SUI": "SUI/USD:USDC", "HYPE": "HYPE/USD:USDC", "XRP": "XRP/USD:USDC",
    "UNI": "UNI/USD:USDC", "XMR": "XMR/USD:USDC", "ZEC": "ZEC/USD:USDC",
    "MON": "MON/USD:USDC", "XAUT": "XAUT/USD:USDC", "XAG": "XAG/USD:USDC",
    "WTI": "WTI/USD:USDC", "US500": "US500/USD:USDC", "GOOGL": "GOOGL/USD:USDC",
    "NVDA": "NVDA/USD:USDC", "TSLA": "TSLA/USD:USDC", "INTC": "INTC/USD:USDC",
    "MU": "MU/USD:USDC", "MRVL": "MRVL/USD:USDC", "SNDK": "SNDK/USD:USDC",
    "AMC": "AMC/USD:USDC", "DRAM": "DRAM/USD:USDC", "LLY": "LLY/USD:USDC",
    "IOVA": "IOVA/USD:USDC", "SPCX": "SPCX/USD:USDC", "PUMP": "PUMP/USD:USDC",
    "CHIP": "CHIP/USD:USDC", "LIT": "LIT/USD:USDC",
}

# Aftermath max leverage per market, VERIFIED from /api/perpetuals/all-markets
# marginRatioInitial (maxLev = 1/IMR), 2026-09-01.
MARKET_MAX_LEVERAGE: dict[str, int] = {
    "BTC": 20, "ETH": 20, "SOL": 20, "XAUT": 20,
    "SUI": 10, "HYPE": 10, "XRP": 10, "UNI": 10, "XMR": 10, "ZEC": 10,
    "MON": 10, "XAG": 10, "US500": 10, "GOOGL": 10, "NVDA": 10, "TSLA": 10,
    "INTC": 10, "MU": 10, "MRVL": 10, "PUMP": 10, "SPCX": 10, "LIT": 10,
    "WTI": 5, "AMC": 5, "DRAM": 5, "LLY": 5, "IOVA": 5, "SNDK": 5, "CHIP": 5,
}


def _sign_ccxt_digest(seed32: bytes, pub32: bytes, signing_digest_b64: str) -> str:
    """Sign a CCXT signing digest with the Sui wallet key.

    The signingDigest from CCXT build is a base64-encoded 32-byte blake2b-256
    hash of the intent message. We sign it with Ed25519 and return a base64
    UserSignature: 0x00 (ED25519 flag) || sig(64) || pubkey(32).
    """
    from sui_adapter import _ed25519_sign
    digest = base64.b64decode(signing_digest_b64)
    sig = _ed25519_sign(seed32, digest)
    serialized = b"\x00" + sig + pub32
    return base64.b64encode(serialized).decode()


def _sign_terms_message(seed32: bytes, pub32: bytes) -> str:
    """Sign the Aftermath Terms and Conditions message for authenticated reads.

    Returns (bytes_b64, signature_b64) where bytes is the base64 of the UTF-8
    terms message and signature is the Ed25519 UserSignature over it.
    """
    from sui_adapter import _ed25519_sign
    terms = "Aftermath Terms and Conditions"
    msg = terms.encode("utf-8")
    # PersonalMessage signing: intent bytes + BCS byteVector
    vec = _bcs_byte_vector(msg)
    intent_msg = b"\x03\x00\x00" + vec
    digest = hashlib.blake2b(intent_msg, digest_size=32).digest()
    sig = _ed25519_sign(seed32, digest)
    serialized = b"\x00" + sig + pub32
    bytes_b64 = base64.b64encode(msg).decode()
    sig_b64 = base64.b64encode(serialized).decode()
    return bytes_b64, sig_b64


def _bcs_uleb128(n: int) -> bytes:
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
    return _bcs_uleb128(len(data)) + data


class AftermathAdapter:
    """Aftermath Perps (Sui) adapter using CCXT build/sign/submit + native reads."""

    def __init__(self, ledger: ExecLedger, seed32: bytes, pubkey32: bytes, address: str,
                 api_base: str | None = None, testnet: bool = False,
                 network: str = ""):
        self.ledger = ledger
        self.seed = seed32
        self.pubkey = pubkey32
        self.address = address if address.startswith("0x") else f"0x{address}"
        # Explicit network name wins (mainnet|testnet); legacy bool kept for
        # callers that only know testnet vs not. Aftermath has NO devnet.
        self.network = (network or ("testnet" if testnet else "mainnet")).strip().lower()
        if self.network not in ("mainnet", "testnet"):
            log.warning("[aftermath] unknown network %r, defaulting to mainnet", self.network)
            self.network = "mainnet"
        default_base = API_TESTNET if self.network == "testnet" else API_MAINNET
        self.api_base = (api_base or default_base).rstrip("/")
        self.usdc_coin_type = (USDC_TESTNET_COIN_TYPE if self.network == "testnet"
                               else USDC_MAINNET_COIN_TYPE)
        self._markets_cache: dict[str, dict] = {}
        self._account_cap: dict | None = None
        self._account_number: int | None = None
        self._terms_auth: tuple[str, str] | None = None
        self._last_account_check = 0.0
        log.info("[aftermath] adapter ready net=%s addr=%s", self.network, self.address)

    # ---------------------------------------------------------------- helpers

    def _req(self, method: str, path: str, json_body: dict | None = None,
             public: bool = True) -> dict:
        url = f"{self.api_base}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            r = requests.request(method, url, headers=headers, json=json_body, timeout=RPC_TIMEOUT)
            if r.status_code >= 400:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}", "raw": r.text}
            data = r.json() if r.content else {}
            return {"ok": True, "data": data, "raw": data}
        except Exception as exc:
            return {"ok": False, "error": f"request failed: {exc}"[:300]}

    def _req_post(self, path: str, body: dict | None = None, public: bool = True) -> dict:
        return self._req("POST", path, json_body=body, public=public)

    def _terms_auth_block(self) -> dict | None:
        if self._terms_auth is None:
            self._terms_auth = _sign_terms_message(self.seed, self.pubkey)
        bytes_b64, sig_b64 = self._terms_auth
        return {"walletAddress": self.address, "bytes": bytes_b64, "signature": sig_b64}

    # ---------------------------------------------------------------- account

    def _resolve_settle_id(self) -> str:
        """Settlement currency id for CCXT createAccount.

        Read from the market catalog (Market.settleId) - the honest source.
        Falls back to the configured USDC coin type when the catalog is
        unreachable (they match on Aftermath: settle = USDC)."""
        try:
            resp = self._req("GET", "/ccxt/markets", public=True)
            if resp.get("ok") and isinstance(resp.get("data"), list):
                for m in resp["data"]:
                    if isinstance(m, dict) and m.get("swap") and m.get("settleId"):
                        return str(m["settleId"])
        except Exception as exc:
            log.warning("[aftermath] settleId resolve failed (%s); using USDC type", exc)
        return self.usdc_coin_type

    def _ensure_account(self) -> dict | None:
        """Find or create an Aftermath perpetuals account for this wallet.

        Returns the account cap dict: {accountId, objectId, accountObjectId, ...} or None.
        """
        now = time.time()
        if self._account_cap and now - self._last_account_check < 60:
            return self._account_cap

        # Check existing accounts (CCXT accounts endpoint).
        resp = self._req_post("/ccxt/accounts", {"address": self.address})
        accounts = []
        if resp.get("ok") and isinstance(resp.get("data"), list):
            for a in resp["data"]:
                if isinstance(a, dict) and a.get("type") == "capability":
                    accounts.append(a)
        if accounts:
            cap = accounts[0]
            self._account_cap = cap
            # The accountNumber is on the paired "account" entry.
            for a in resp["data"]:
                if isinstance(a, dict) and a.get("type") == "account" and a.get("accountNumber") is not None:
                    self._account_number = int(a["accountNumber"])
                    break
            self._last_account_check = now
            log.info("[aftermath] using existing account cap=%s num=%s",
                     cap.get("id", "?")[:12], self._account_number)
            return cap

        # No account found — create one. Build tx, sign, submit.
        # CCXT createAccount schema (both mainnet and testnet):
        #   {settleId, metadata} where settleId is the settlement currency id
        #   (read from Market.settleId - USDC on both networks) and metadata
        #   requires {sender}. This is NOT {walletAddress, collateralCoinType}
        #   (that is the native REST shape; the CCXT builder rejects it).
        log.info("[aftermath] no account found — creating one for %s", self.address[:12])
        settle_id = self._resolve_settle_id()
        create_body = {
            "settleId": settle_id,
            "metadata": {"sender": self.address, "gasFromAddressBalance": True},
        }
        resp = self._req_post("/ccxt/build/createAccount", create_body)
        if not resp.get("ok"):
            log.warning("[aftermath] create account build failed: %s", resp.get("error"))
            return None
        build = resp["data"]
        sig = _sign_ccxt_digest(self.seed, self.pubkey, build["signingDigest"])
        submit_body = {"transactionBytes": build["transactionBytes"], "signatures": [sig]}
        resp = self._req_post("/ccxt/submit/createAccount", submit_body)
        if not resp.get("ok"):
            log.warning("[aftermath] create account submit failed: %s", resp.get("error"))
            return None
        # Re-fetch accounts after creation.
        time.sleep(2)
        resp = self._req_post("/ccxt/accounts", {"address": self.address})
        if resp.get("ok") and isinstance(resp.get("data"), list):
            for a in resp["data"]:
                if isinstance(a, dict) and a.get("type") == "capability":
                    self._account_cap = a
                    self._last_account_check = now
                    log.info("[aftermath] account created cap=%s", a.get("id", "?")[:12])
                    return a
        return None

    def account_number(self) -> int | None:
        cap = self._ensure_account()
        if cap is None:
            return None
        if self._account_number is not None:
            return self._account_number
        # Fallback: resolve from CCXT accounts.
        resp = self._req_post("/ccxt/accounts", {"address": self.address})
        if resp.get("ok") and isinstance(resp.get("data"), list):
            for a in resp["data"]:
                if isinstance(a, dict) and a.get("type") == "account":
                    acc_num = a.get("accountNumber")
                    if acc_num is not None:
                        self._account_number = int(acc_num)
                        return self._account_number
        return None

    def account_cap_id(self) -> str | None:
        cap = self._ensure_account()
        if cap:
            return cap.get("id")
        return None

    def accounts(self) -> list[dict]:
        """Raw CCXT accounts for this wallet (collateral per account)."""
        resp = self._req_post("/ccxt/accounts", {"address": self.address})
        if resp.get("ok") and isinstance(resp.get("data"), list):
            return resp["data"]
        return []

    def collateral(self) -> float:
        """Free collateral currently inside the Aftermath account (USDC).

        Read from the native accounts/owned endpoint (collateral is the raw
        u64 as a '123n' string). Returns 0.0 when the account has none."""
        resp = self._req_post("/perpetuals/accounts/owned", {"walletAddress": self.address})
        if not resp.get("ok"):
            return 0.0
        caps = (resp.get("data") or {}).get("accountCaps") or []
        for c in caps:
            raw = str(c.get("collateral") or "0n")
            try:
                return float(raw.rstrip("n")) / 1e6
            except Exception:
                continue
        return 0.0

    def deposit(self, amount: float) -> dict:
        """Deposit collateral (USDC) from the wallet into the Aftermath account.

        The wallet's on-chain USDC is separate from the perp account - funds
        must be deposited before trading. Native REST flow: ask the API to
        build the deposit txKind (cap/account/registry inputs), then append
        the USDC coin as input 3 (the deposit MoveCall args are
        (account, cap, registry, coin) = inputs 1,0,2,3), then wrap+sign+submit."""
        acc_num = self.account_number()
        if acc_num is None:
            return {"ok": False, "error": "no aftermatch account number"}
        coin = self._find_usdc_coin()
        if coin is None:
            return {"ok": False, "error": "no USDC coin object found on wallet"}
        import base58 as _b58
        from sui_adapter import _uleb128 as _uleb

        def _uleb_read(buf: bytes, pos: int) -> tuple[int, int]:
            n = 0
            shift = 0
            while True:
                x = buf[pos]
                pos += 1
                n |= (x & 0x7F) << shift
                shift += 7
                if not x & 0x80:
                    return n, pos

        # 1. API-built base txKind (cap, account, registry = inputs 0,1,2)
        body = {
            "walletAddress": self.address,
            "accountId": f"{acc_num}n",
            "collateralCoinType": self.usdc_coin_type,
            "depositCoinArg": {"Input": 3},
        }
        resp = self._req_post("/perpetuals/account/transactions/deposit-collateral", body)
        if not resp.get("ok"):
            return {"ok": False, "error": f"deposit build failed: {resp.get('error')}"}
        tk0 = base64.b64decode((resp.get("data") or {}).get("txKind") or "")
        if not tk0:
            return {"ok": False, "error": "no txKind in deposit response"}

        # 2. parse input section length, append the coin as input 3
        i = 1
        n_inp0, i = _uleb_read(tk0, i)
        for _k in range(n_inp0):
            tag = tk0[i]; i += 1
            if tag == 1:
                otag = tk0[i]; i += 1
                if otag == 0:
                    i += 32 + 8
                    _dl, i = _uleb_read(tk0, i); i += _dl
                elif otag == 1:
                    i += 32 + 8 + 1
            elif tag == 0:
                ln, i = _uleb_read(tk0, i); i += ln
        cmd_start = i
        call_arg = (b"\x01\x00" + bytes.fromhex(coin["objectId"][2:])
                    + int(coin["version"]).to_bytes(8, "little")
                    + _uleb(len(coin["digest_bytes"])) + coin["digest_bytes"])
        new_inputs = _uleb(n_inp0 + 1) + tk0[2:cmd_start] + call_arg
        tx_kind_b64 = base64.b64encode(b"\x00" + new_inputs + tk0[cmd_start:]).decode()
        return self._submit_native_tx(tx_kind_b64)

    def _find_usdc_coin(self) -> dict | None:
        """Locate the wallet's USDC coin object (id, version, digest bytes).

        The coin must be of the SETTLE ID type (the USDC Aftermath settles in),
        which is what the deposit MoveCall's Coin<> type argument expects."""
        settle_id = self._resolve_settle_id()
        import requests as _r
        gql_url = "https://graphql.testnet.sui.io/graphql" if self.network == "testnet" \
            else "https://graphql.mainnet.sui.io/graphql"
        q = ('{ address(address: "' + self.address + '") {'
             '  objects(first: 20, filter: {type: "0x2::coin::Coin<'
             + settle_id + '>"}) { nodes {'
             '    address version digest contents { json } } } } }')
        try:
            r = _r.post(gql_url, json={"query": q}, timeout=20)
            data = r.json().get("data") or {}
            nodes = (((data.get("address") or {}).get("objects") or {}).get("nodes") or [])
            for o in nodes:
                try:
                    bal = int(((o.get("contents") or {}).get("json") or {}).get("balance") or 0)
                except Exception:
                    continue
                if bal > 0:
                    import base58 as _b58
                    return {
                        "objectId": o.get("address"),
                        "version": int(o.get("version", 0)),
                        "digest": o.get("digest", ""),
                        "digest_bytes": _b58.b58decode(o.get("digest", "")),
                    }
        except Exception as exc:
            log.warning("[aftermath] usdc coin lookup failed: %s", exc)
        return None

    def _wallet_usdc_balance(self) -> float:
        """Wallet's on-chain USDC of the settle type (usable for deposits)."""
        coin = self._find_usdc_coin()
        if coin is None:
            return 0.0
        import requests as _r
        gql_url = "https://graphql.testnet.sui.io/graphql" if self.network == "testnet" \
            else "https://graphql.mainnet.sui.io/graphql"
        q = ('{ object(address: "' + coin["objectId"] + '") { '
             '  asMoveObject { contents { json } } } }')
        try:
            r = _r.post(gql_url, json={"query": q}, timeout=20)
            data = r.json().get("data") or {}
            obj = (data.get("object") or {}).get("asMoveObject") or {}
            bal = ((obj.get("contents") or {}).get("json") or {}).get("balance") or 0
            return float(bal) / 1e6
        except Exception as exc:
            log.warning("[aftermath] wallet usdc balance failed: %s", exc)
            return 0.0

    def _submit_native_tx(self, tx_kind_b64: str) -> dict:
        """Wrap a base64 TransactionKind in TransactionData::V1, sign, submit.

        txKind from the native REST is a BCS TransactionKind. We build
        TransactionData::V1 { kind, sender, gas_data, expiration } using the
        same BCS primitives the Sui adapter uses, then broadcast via the Sui
        GraphQL executeTransaction."""
        import hashlib as _hl
        import base58 as _b58
        from sui_adapter import (_bcs_addr, _bcs_u64, _ed25519_sign,
                                 _bcs_object_ref, _bcs_vec, _uleb128)

        tx_kind = base64.b64decode(tx_kind_b64)
        now_ms = int(time.time() * 1000)
        # 1. gas coin + price + budget via Sui GraphQL (same as SUIAdapter)
        import requests as _r
        gql_url = "https://graphql.testnet.sui.io/graphql" if self.network == "testnet" \
            else "https://graphql.mainnet.sui.io/graphql"
        q_coin = ('{ address(address: "' + self.address + '") {'
                  '  objects(first: 20, filter: {type: "0x2::coin::Coin<0x2::sui::SUI>"}) { nodes {'
                  '    address version digest'
                  '    contents { json } } } } }')
        r = _r.post(gql_url, json={"query": q_coin}, timeout=20)
        data = r.json().get("data") or {}
        addr_node = data.get("address") or {}
        objs = ((addr_node.get("objects") or {}).get("nodes") or [])
        gas_coin = None
        for o in objs:
            try:
                bal = int(((o.get("contents") or {}).get("json") or {}).get("balance") or 0)
            except Exception:
                continue
            if bal >= 50_000_000:  # 0.05 SUI min for gas
                gas_coin = {"objectId": o.get("address"), "version": int(o.get("version", 0)),
                            "digest": o.get("digest", "")}
                break
        if gas_coin is None:
            return {"ok": False, "error": "no SUI gas coin >= 0.05 SUI on testnet wallet"}
        # Sui digests are base58 - convert to hex for the BCS object ref.
        digest_hex = _b58.b58decode(gas_coin["digest"]).hex()
        gas_coin["digest"] = "0x" + digest_hex
        # gas price
        q_gp = "{ serviceConfig { referenceGasPrice } }"
        r2 = _r.post(gql_url, json={"query": q_gp}, timeout=20)
        gas_price = int(((r2.json().get("data") or {}).get("serviceConfig") or {}).get("referenceGasPrice") or 1000)
        # budget: a single deposit PTB costs ~0.001-0.01 SUI; use 0.02 SUI to stay
        # well under the 0.1-0.186 SUI gas coins available on testnet.
        budget = 20_000_000

        # 2. TransactionData::V1 = 0x00 || kind || sender || gas_data || 0x00
        gas_data = (
            _bcs_vec([_bcs_object_ref(gas_coin["objectId"], gas_coin["version"], gas_coin["digest"])])
            + _bcs_addr(self.address) + _bcs_u64(gas_price) + _bcs_u64(budget)
        )
        tx_bytes = b"\x00" + tx_kind + _bcs_addr(self.address) + gas_data + b"\x00"

        # 3. sign intent hash and submit via GraphQL
        intent_msg = _hl.blake2b(b"\x00\x00\x00" + tx_bytes, digest_size=32).digest()
        sig = _ed25519_sign(self.seed, intent_msg)
        signature = base64.b64encode(b"\x00" + sig + self.pubkey).decode()
        tx_b64 = base64.b64encode(tx_bytes).decode()
        q_sub = ('mutation { executeTransaction(transactionDataBcs: "' + tx_b64 +
                 '", signatures: ["' + signature + '"]) { '
                 'effects { digest status executionError { message abortCode } } } }')
        r3 = _r.post(gql_url, json={"query": q_sub}, timeout=60)
        d3 = r3.json()
        if d3.get("errors"):
            return {"ok": False, "error": f"submit failed: {d3['errors'][0].get('message','')[:200]}"}
        exec_res = (d3.get("data") or {}).get("executeTransaction") or {}
        effects = exec_res.get("effects") or {}
        status = str(effects.get("status") or "")
        if status != "SUCCESS":
            err = str(effects.get("executionError") or effects.get("status") or "unknown")
            return {"ok": False, "error": f"tx failed: {err}"[:300]}
        return {"ok": True, "tx_hash": effects.get("digest", ""), "digest": effects.get("digest", "")}

    # ---------------------------------------------------------------- market

    def market(self, symbol: str) -> dict | None:
        """Resolve base symbol -> market info dict {id, symbol, base, ...}."""
        sym = (symbol or "").upper()
        # Check cache first.
        cached = self._markets_cache.get(sym)
        if cached:
            return cached
        # Fetch from API.
        markets = self.markets()
        for m in markets:
            base = (m.get("base") or "").upper()
            if base == sym:
                self._markets_cache[sym] = m
                return m
        return None

    def market_ch_id(self, symbol: str) -> str | None:
        """Market object ID (chId) for a base symbol."""
        m = self.market(symbol)
        if m:
            return m.get("id")
        return None

    def markets(self) -> list[dict]:
        """All traded perpetual markets from GET /api/ccxt/markets."""
        cached = getattr(self, "_markets_full", None)
        if cached:
            return cached
        try:
            resp = self._req("GET", "/ccxt/markets", public=True)
            if resp.get("ok") and isinstance(resp.get("data"), list):
                out = []
                for m in resp["data"]:
                    if isinstance(m, dict) and m.get("swap") and m.get("contract"):
                        out.append(m)
                if out:
                    self._markets_full = out
                    return out
        except Exception as exc:
            log.warning("[aftermath] markets fetch failed (%s); using static map", exc)
        self._markets_full = []
        return []

    def ticker(self, symbol: str) -> dict | None:
        """Latest ticker for a symbol (mark price, bid, ask, etc.)."""
        ch_id = self.market_ch_id(symbol)
        if not ch_id:
            return None
        resp = self._req_post("/ccxt/ticker", {"chId": ch_id})
        if resp.get("ok") and isinstance(resp.get("data"), dict):
            return resp["data"]
        return None

    def market_price(self, symbol: str) -> float | None:
        """Mid price from the ticker for a symbol."""
        t = self.ticker(symbol)
        if t:
            bid = t.get("bid")
            ask = t.get("ask")
            mark = t.get("markPrice") or t.get("indexPrice") or t.get("last")
            if bid and ask:
                return (float(bid) + float(ask)) / 2.0
            if mark:
                return float(mark)
        return None

    # ---------------------------------------------------------------- orders

    def place_order(self, intent: OrderIntent, ref_price: float) -> dict:
        """Place a market or limit order via CCXT build/sign/submit.

        AUTO-DEPOSIT: if the Aftermath account holds less collateral than the
        order needs and the wallet holds USDC, deposit the shortfall first so
        the order can actually be covered (the wallet USDC is not usable until
        it is inside the account)."""
        acc_id = self.account_cap_id()
        if not acc_id:
            return {"ok": False, "error": "no aftermatch account cap available"}
        ch_id = self.market_ch_id(intent.symbol)
        if not ch_id:
            return {"ok": False, "error": f"unsupported aftermatch market {intent.symbol}"}

        # AUTO-DEPOSIT: ensure the account can cover this order.
        needed = intent.notional(ref_price) / max(intent.leverage, 1)
        have = self.collateral()
        if have < needed:
            wallet_usdc = self._wallet_usdc_balance()
            if wallet_usdc > 0:
                to_deposit = min(wallet_usdc, needed * 1.5)
                log.info("[aftermath] collateral %.2f < needed %.2f - auto-depositing %.2f USDC",
                         have, needed, to_deposit)
                dep = self.deposit(to_deposit)
                if not dep.get("ok"):
                    return {"ok": False, "error": f"auto-deposit failed: {dep.get('error')}"}
                time.sleep(2)
                have = self.collateral()
            if have < needed:
                return {"ok": False, "error": (
                    f"insufficient collateral: account {have:.2f} < needed {needed:.2f} USDC "
                    f"(fund the wallet with the Aftermath testnet USDC type)")}

        side = "sell" if intent.side == "sell" else "buy"
        order_type = "market" if intent.order_type == "market" else "limit"
        amount = intent.qty
        # LIMIT PRICE: use the intent's limit_price (the 2bps inside-market
        # maker entry computed by the agent) when present, NOT the raw ref_price
        # the router passes separately. ref_price is only a fallback.
        price = (intent.limit_price or ref_price) if order_type == "limit" else None

        orders = [{
            "chId": ch_id,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
        }]

        body = {
            "orders": orders,
            "accountId": acc_id,
            "deallocateFreeCollateral": False,
            "metadata": {"sender": self.address, "gasFromAddressBalance": True},
        }
        if intent.leverage > 1:
            body["leverage"] = intent.leverage

        resp = self._req_post("/ccxt/build/createOrders", body)
        if not resp.get("ok"):
            return {"ok": False, "error": f"build failed: {resp.get('error')}"}

        build = resp["data"]
        sig = _sign_ccxt_digest(self.seed, self.pubkey, build["signingDigest"])
        submit_body = {"transactionBytes": build["transactionBytes"], "signatures": [sig]}

        resp = self._req_post("/ccxt/submit/createOrders", submit_body)
        if not resp.get("ok"):
            return {"ok": False, "error": f"submit failed: {resp.get('error')}"}

        # Parse response: CCXT submit returns Order[].
        orders_resp = resp.get("data")
        if isinstance(orders_resp, list) and orders_resp:
            first = orders_resp[0]
            return {
                "ok": True,
                "venue_order_id": str(first.get("id", "")),
                "order_id": str(first.get("id", "")),
                "price": float(first.get("price") or ref_price),
                "filled_qty": float(first.get("filled", 0) or 0),
                "fee": float(first.get("fee", {}).get("cost", 0) if isinstance(first.get("fee"), dict) else 0),
                "status": first.get("status", "submitted"),
                "raw": orders_resp,
            }

        return {"ok": True, "venue_order_id": "", "price": ref_price, "filled_qty": 0, "fee": 0.0}

    def cancel_all(self, symbol: str | None = None) -> dict:
        """Cancel all open orders (optionally for a specific market)."""
        acc_id = self.account_cap_id()
        if not acc_id:
            return {"ok": False, "error": "no aftermatch account cap available"}
        ch_id = self.market_ch_id(symbol) if symbol else None

        # Fetch open orders to know which IDs to cancel.
        open_orders = self.open_orders()
        if not open_orders.get("ok"):
            return {"ok": True, "cancelled": 0}  # no orders to cancel is fine

        order_ids = []
        for o in open_orders.get("data", []):
            if isinstance(o, dict):
                oid = o.get("id")
                if oid:
                    if ch_id:
                        o_ch = o.get("symbol") or o.get("info", {}).get("chId") or ""
                        if ch_id not in o_ch:
                            continue
                    order_ids.append(str(oid))

        if not order_ids:
            return {"ok": True, "cancelled": 0}

        body = {
            "accountId": acc_id,
            "chId": ch_id or "",
            "orderIds": order_ids,
            "deallocateFreeCollateral": False,
            "metadata": {"sender": self.address, "gasFromAddressBalance": True},
        }

        resp = self._req_post("/ccxt/build/cancelOrders", body)
        if not resp.get("ok"):
            return {"ok": False, "error": f"cancel build failed: {resp.get('error')}"}

        build = resp["data"]
        sig = _sign_ccxt_digest(self.seed, self.pubkey, build["signingDigest"])
        submit_body = {"transactionBytes": build["transactionBytes"], "signatures": [sig]}
        resp = self._req_post("/ccxt/submit/cancelOrders", submit_body)
        return {
            "ok": bool(resp.get("ok")),
            "cancelled": len(order_ids),
            "error": resp.get("error") if not resp.get("ok") else None,
        }

    # ---------------------------------------------------------------- state

    def positions(self) -> dict:
        """Open positions from CCXT positions endpoint."""
        acc_num = self.account_number()
        if acc_num is None:
            return {"ok": False, "error": "no aftermatch account number"}
        resp = self._req_post("/ccxt/positions", {"accountNumber": acc_num})
        if not resp.get("ok"):
            return resp
        return {"ok": True, "data": resp.get("data", [])}

    def open_orders(self) -> dict:
        """Open orders from CCXT myPendingOrders endpoint."""
        acc_num = self.account_number()
        if acc_num is None:
            return {"ok": False, "error": "no aftermatch account number"}
        resp = self._req_post("/ccxt/myPendingOrders", {"accountNumber": acc_num})
        return {"ok": bool(resp.get("ok")), "data": resp.get("data", []) if resp.get("ok") else []}

    def balance(self) -> dict:
        """Account balance (collateral) from CCXT balance endpoint."""
        acc_num = self.account_number()
        if acc_num is None:
            return {"ok": False, "error": "no aftermatch account number"}
        resp = self._req_post("/ccxt/balance", {"account": str(acc_num)})
        return resp

    # ---------------------------------------------------------------- killswitch hook

    def flat_and_cancel(self, bot_id: int) -> dict:
        """Aftermath perp killswitch: cancel all orders + close positions (market)."""
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
            if not isinstance(p, dict):
                continue
            market_sym = p.get("symbol") or ""
            qty = abs(float(p.get("contracts") or p.get("baseAssetAmount") or 0))
            side = str(p.get("side", "long"))
            if market_sym and qty > 0:
                base = market_sym.split("/")[0].split(":")[0]
                close = self.place_order(
                    OrderIntent(chain="sui", venue="aftermath-perp",
                                symbol=base,
                                side="sell" if side == "long" else "buy",
                                qty=qty, order_type="market",
                                idempotency_key=f"kill-{bot_id}-{base}"),
                    ref_price=float(p.get("entryPrice") or p.get("liquidationPrice") or 0) or 1.0,
                )
                result["closed"].append({market_sym: close.get("ok")})
                if not close.get("ok"):
                    result["errors"].append(f"close {market_sym}: {close.get('error')}")
                    result["ok"] = False
        return result


def build_aftermath(ledger: ExecLedger, keypair_hex: str,
                    api_base: str | None = None, testnet: bool = False,
                    network: str = "") -> AftermathAdapter:
    """Factory: derive adapter from a Sui seed (raw hex or bech32 keystring).

    testnet/network select the Aftermath API base (mainnet vs testnet) and the
    matching USDC coin type. Aftermath has no devnet."""
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
            raise ValueError(f"aftermath keypair_hex must be 32 bytes, got {len(seed)}")
        from sui_adapter import _ed25519_pubkey
        pub = _ed25519_pubkey(seed)
        addr = "0x" + hashlib.blake2b(b"\x00" + pub, digest_size=32).hexdigest()
    return AftermathAdapter(ledger, seed, pub, addr,
                            api_base=api_base, testnet=testnet, network=network)