"""Thin Sui GraphQL read client (JSON-RPC is deprecated on public fullnodes).

Kept tiny and dependency-free so streamer/metrics/validator all share one read
surface that's trivially mockable in tests (pass `post=`). Broadcast+sign lives in
the executor (which reuses sui_adapter's Ed25519/BCS primitives), not here.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import requests

from . import constants as K


def _digest_to_hex(digest: str) -> str:
    """Sui object/transaction digests are base58 in JSON-RPC (Blockvision) and
    base64 in some GraphQL responses. The signer's BCS object-ref needs the raw
    32 bytes as hex, so detect the encoding instead of assuming one."""
    if not digest:
        return ""
    d = digest[2:] if digest.startswith("0x") else digest
    try:
        int(d, 16)
        return d
    except ValueError:
        pass
    try:
        import base58 as _b58
        raw = _b58.b58decode(d)
        if len(raw) == 32:
            return raw.hex()
    except Exception:
        pass
    return base64.b64decode(digest).hex()


class GqlError(Exception):
    pass


class Chain:
    def __init__(self, network: str = "mainnet", post=None, timeout: int = 25):
        self.network = network
        self.url = K.SUI_GQL_MAINNET if network == "mainnet" else K.SUI_GQL_TESTNET
        self._post = post or (lambda url, body: requests.post(url, json=body, timeout=timeout))
        self.timeout = timeout

    def query(self, q: str, variables: dict | None = None) -> dict:
        body: dict[str, Any] = {"query": q}
        if variables:
            body["variables"] = variables
        resp = self._post(self.url, body)
        # accept a dict (tests) or a requests-like with .json()
        data = resp if isinstance(resp, dict) else resp.json()
        if isinstance(data, dict) and data.get("errors"):
            raise GqlError(str(data["errors"][:2]))
        return (data or {}).get("data", {})

    # ---------------- objects ----------------

    def object(self, address: str) -> dict | None:
        """{address, version, digest, type, owner{...}, json} or None if absent."""
        q = (
            '{ object(address: "' + address + '") { '
            'address version digest '
            'owner { ... on AddressOwner { address { address } } ... on Shared { initialSharedVersion } } '
            'asMoveObject { contents { type { repr } json } } '
            '} }'
        )
        o = (self.query(q) or {}).get("object")
        if not o:
            return None
        mo = o.get("asMoveObject") or {}
        cont = mo.get("contents") or {}
        typ = (cont.get("type") or {}).get("repr", "")
        own = o.get("owner") or {}
        owner_addr = ((own.get("address") or {}).get("address") or "")
        init_shared = own.get("initialSharedVersion")
        try:
            init_shared = int(init_shared) if init_shared else 0
        except (TypeError, ValueError):
            init_shared = 0
        try:
            parsed = json.loads(cont.get("json") or "{}") if isinstance(cont.get("json"), str) else (cont.get("json") or {})
        except (ValueError, TypeError):
            parsed = {}
        return {"address": o["address"], "version": int(o.get("version", 0)),
                "digest": o.get("digest", ""), "type": typ, "owner": owner_addr,
                "shared_version": init_shared, "json": parsed}

    def objects_by_type(self, type_repr: str, first: int = 20) -> list[dict]:
        """Live objects whose Move type matches (ObjectFilter.type confirmed)."""
        q = (
            '{ objects(filter: { type: "' + type_repr + '" }, first: ' + str(first) + ') '
            '{ nodes { address version } pageInfo { hasNextPage endCursor } } }'
        )
        try:
            conn = (self.query(q) or {}).get("objects") or {}
        except GqlError:
            return []
        return [{"address": n["address"], "version": int(n.get("version", 0))}
                for n in conn.get("nodes", [])]

    # ---------------- events ----------------

    def events(self, type_repr: str, first: int = 50, after: str = "") -> tuple[list[dict], str, bool]:
        """Return (nodes, endCursor, hasNextPage). nodes carry parsed contents+sender+ts.

        Ordering: Sui GraphQL events connection is oldest-first; callers keep a
        persisted endCursor to resume. Dedup by (checkpoint, eventSeq) anyway."""
        tail = (', after: "' + after + '"') if after else ""
        q = (
            '{ events(filter: { type: "' + type_repr + '" }, first: ' + str(first) + tail + ') { '
            'nodes { timestamp sequenceNumber sender { address } transaction { digest } '
            'contents { type { repr } json } } '
            'pageInfo { hasNextPage endCursor } } }'
        )
        conn = (self.query(q) or {}).get("events") or {}
        nodes = []
        for n in conn.get("nodes", []):
            cont = n.get("contents") or {}
            try:
                parsed = json.loads(cont["json"]) if isinstance(cont.get("json"), str) else (cont.get("json") or {})
            except (ValueError, TypeError):
                parsed = {}
            nodes.append({"timestamp": n.get("timestamp"),
                          "seq": int(n.get("sequenceNumber") or 0),
                          "sender": (n.get("sender") or {}).get("address", ""),
                          "digest": (n.get("transaction") or {}).get("digest", ""),
                          "type": (cont.get("type") or {}).get("repr", ""),
                          "json": parsed})
        pi = conn.get("pageInfo") or {}
        return nodes, (pi.get("endCursor") or ""), bool(pi.get("hasNextPage"))

    # ---------------- coins / gas ----------------

    def coins(self, owner: str, coin_type: str = K.SUI_COIN_TYPE) -> list[dict]:
        """Owned coins of a type: [{objectId, version, digest, balance_mist}].

        Mirrors sui_adapter._gql_coins: query under `address { objects }`, the
        true balance lives in contents.json.balance (per-object balance field
        is 0 on current GraphQL); digests normalized to hex."""
        if not coin_type.startswith("0x2::coin::Coin<"):
            coin_type = f"0x2::coin::Coin<{coin_type}>"
        is_mainnet_sui = (
            self.network == "mainnet"
            and coin_type == f"0x2::coin::Coin<{K.SUI_COIN_TYPE}>"
        )
        # A non-empty GraphQL SUI page can still be partial and omit the only
        # coin large enough for a buy. Prefer the full JSON-RPC inventory for
        # native SUI; GraphQL below remains the outage fallback.
        if is_mainnet_sui:
            rpc = self._rpc_coins(owner, K.SUI_COIN_TYPE)
            if rpc:
                return rpc
        q = (
            '{ address(address: "' + owner + '") { objects(first: 50, filter: {type: "'
            + coin_type + '"}) { nodes { address version digest contents { json } } } } }'
        )
        try:
            nodes = (((self.query(q) or {}).get("address") or {}).get("objects") or {}).get("nodes") or []
        except GqlError:
            nodes = []
        out = []
        for n in nodes:
            try:
                bal = int(((n.get("contents") or {}).get("json") or {}).get("balance") or 0)
            except (ValueError, TypeError):
                bal = 0
            out.append({"objectId": n.get("address", ""), "version": int(n.get("version", 0)),
                        "digest": _digest_to_hex(n.get("digest", "")), "balance_mist": bal})
        if out and any(c["balance_mist"] > 0 for c in out):
            return out
        # Non-native coin inventory still uses GraphQL first. Native SUI has
        # already attempted RPC above and only gets here when that source is
        # unavailable.
        raw = coin_type.split("<", 1)[1].rsplit(">", 1)[0] if "<" in coin_type else coin_type
        rpc = self._rpc_coins(owner, raw)
        return rpc if rpc else out

    def _rpc_coins(self, owner: str, raw_coin: str) -> list[dict]:
        # Blockvision intermittently returns HTTP 200 + empty data[] even when
        # coins exist (observed on mainnet 2026-09-16). Retry so a flake never
        # surfaces as a false "insufficient balance" in the buy path.
        for attempt in range(3):
            try:
                r = requests.post("https://sui-mainnet-endpoint.blockvision.org:443", json={
                    "jsonrpc": "2.0", "id": 1, "method": "suix_getCoins",
                    "params": [owner, raw_coin, None, 50]}, timeout=8)
                data = (r.json().get("result") or {}).get("data") or []
                if data:
                    return [{"objectId": c.get("coinObjectId") or c.get("objectId"),
                             "version": int(c.get("version", 0)),
                             "digest": _digest_to_hex(str(c.get("digest") or "")),
                             "balance_mist": int(c.get("balance") or 0)} for c in data]
            except Exception:
                pass
            import time as _t
            _t.sleep(0.4 * (attempt + 1))
        return []

    def balance(self, owner: str, coin_type: str = K.SUI_COIN_TYPE) -> int:
        q = '{ address(address: "' + owner + '") { balance(coinType: "' + coin_type + '") { totalBalance } } }'
        try:
            a = (self.query(q) or {}).get("address") or {}
            return int(((a.get("balance") or {}).get("totalBalance")) or 0)
        except (GqlError, ValueError):
            return 0

    def gas_price(self) -> int:
        try:
            cfg = (self.query('{ serviceConfig { referenceGasPrice } }') or {}).get("serviceConfig") or {}
            return int(cfg.get("referenceGasPrice") or 1000)
        except (GqlError, ValueError):
            return 1000
