"""Production wiring: build the full execution stack from env config.

Gate by REAL_TRADING_ENABLED (default 0). When enabled AND per-chain keys
are configured, the gateway wires adapters → router → killswitch → sync
fetchers → deposit checkers, all fed from the same ledger.

Usage:
    gateway = ExecGateway.build()
    if gateway.ready:
        gateway.route(bot_id, intent, ref_price)
        gateway.killswitch(bot_id, "reason")
"""

import json
import logging
import os
from pathlib import Path

import requests

from exec_vault import ExecVault
from hooks import build_adapters, register_chain_hooks
from killswitch import KillSwitch
from ledger import ExecLedger
from deposit_watch import DepositWatch
from risk_guard import RiskGuard, BotRiskProfile
from router import VenueRouter, build_router
from sync_engine import SyncEngine

log = logging.getLogger("execution")

_GATE_ENV = "REAL_TRADING_ENABLED"  # set to "1" to activate real execution
_KEY_ENV = "TG_EXEC_MASTER_KEY"  # Fernet master key for trading keys
_LEDGER_PATH = "EXEC_LEDGER_PATH"  # path to the execution SQLite DB


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _load_execution_cfg() -> dict:
    """Build cfg dict for build_adapters() from env vars.
    
    Returns {} if no keys are configured (safe default).
    Per-chain env vars:
      EXEC_HL_AGENT_KEY, EXEC_HL_MASTER_ADDRESS, EXEC_HL_TESTNET
      EXEC_SOL_KEYPAIR_HEX, EXEC_SOL_RPC_URL, EXEC_SOL_TESTNET
      EXEC_SUI_KEYPAIR_HEX, EXEC_SUI_RPC_URL, EXEC_SUI_TESTNET
      EXEC_SUI_DEEPBOOK_PACKAGE, EXEC_SUI_POOL_ID, EXEC_SUI_BALANCE_MANAGER
    """
    hl_key = _env("EXEC_HL_AGENT_KEY")
    hl_master = _env("EXEC_HL_MASTER_ADDRESS")
    sol_key = _env("EXEC_SOL_KEYPAIR_HEX")
    sui_key = _env("EXEC_SUI_KEYPAIR_HEX")
    if not (hl_key or sol_key or sui_key):
        return {}
    
    vault = ExecVault()
    cfg = {}
    
    if hl_key and hl_master:
        cfg["hyperliquid"] = {
            "key_enc": vault.encrypt(hl_key),
            "master_address": hl_master,
            "testnet": _env("EXEC_HL_TESTNET", "1") != "0",
        }
    
    if sol_key:
        cfg["solana"] = {
            "key_enc": vault.encrypt(sol_key),
            "rpc_url": _env("EXEC_SOL_RPC_URL", ""),
            "testnet": _env("EXEC_SOL_TESTNET", "1") != "0",
        }
    
    if sui_key:
        extra = {}
        for env_name, attr in (
            ("EXEC_SUI_DEEPBOOK_PACKAGE", "deepbook_package"),
            ("EXEC_SUI_POOL_ID", "pool_id"),
            ("EXEC_SUI_BALANCE_MANAGER", "balance_manager"),
        ):
            v = _env(env_name)
            if v:
                extra[attr] = v
        cfg["sui"] = {
            "key_enc": vault.encrypt(sui_key),
            "rpc_url": _env("EXEC_SUI_RPC_URL", ""),
            "testnet": _env("EXEC_SUI_TESTNET", "1") != "0",
            **extra,
        }
    
    return cfg


# ---------------------------------------------------------------- sync fetchers


def _hl_fetcher(adapter):
    def fetch(wallet):
        state = adapter.get_account_state()
        if not state.get("ok"):
            raise RuntimeError(state.get("error", "hl fetch failed"))
        return {"balances": state["balances"], "positions": state["positions"], "orders": []}
    return fetch


def _sol_fetcher(adapter):
    def fetch(wallet):
        return {
            "balances": {"USDC": adapter.get_balance("USDC"), "native": adapter.get_balance("SOL")},
            "positions": adapter.get_positions(),
            "orders": [],
        }
    return fetch


def _sui_fetcher(adapter):
    def fetch(wallet):
        return {
            "balances": {"USDC": adapter.get_balance("USDC"), "native": adapter.get_balance("SUI")},
            "positions": adapter.get_positions(),
            "orders": [],
        }
    return fetch


# ---------------------------------------------------------------- deposit checkers


def _sol_deposit_checker(adapter):
    from sol_adapter import USDC_MINT, SOL_MINT

    def check(wallet):
        pubkey = str(wallet.get("address") or wallet.get("pubkey") or "")
        if not pubkey:
            return []
        rpc_url = adapter.rpc_url
        try:
            resp = requests.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [pubkey, {"limit": 15}],
            }, timeout=20)
            signatures = (resp.json().get("result") or [])
        except Exception as exc:
            log.warning("sol deposit sig scan failed: %s", exc)
            return []
        events = []
        for sig_info in signatures:
            sig = sig_info.get("signature")
            if not sig:
                continue
            try:
                tx_resp = requests.post(rpc_url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getParsedTransaction",
                    "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                }, timeout=20)
                tx = tx_resp.json().get("result")
                if not tx:
                    continue
                meta = tx.get("meta") or {}
                pre = meta.get("preTokenBalances") or []
                post = meta.get("postTokenBalances") or []
                pre_by_mint = {p.get("mint", ""): p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0 for p in pre}
                post_by_mint = {p.get("mint", ""): p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0 for p in post}
                for mint in (USDC_MINT, SOL_MINT):
                    prev_amt = pre_by_mint.get(mint, 0)
                    post_amt = post_by_mint.get(mint, 0)
                    if post_amt > prev_amt and post_amt - prev_amt > 1.0:
                        events.append({"asset": "USDC" if mint == USDC_MINT else "SOL",
                                        "amount": post_amt - prev_amt, "tx_hash": sig})
            except Exception as exc:
                log.warning("sol deposit tx parse failed for %s: %s", sig, exc)
                continue
        return events
    return check


def _sui_deposit_checker(adapter):
    def check(wallet):
        addr = str(wallet.get("address") or "")
        if not addr:
            return []
        try:
            prev_usdc = adapter.get_balance("USDC")
            prev_sui = adapter.get_balance("SUI")
        except Exception:
            return []
        events = []
        try:
            res = adapter._rpc("suix_queryTransactionBlocks", [
                addr, {"options": {"showBalanceChanges": True}},
            ])
            for block in (res.get("data") or [])[:10]:
                bc = block.get("balanceChanges") or []
                for change in bc:
                    owner = (change.get("owner") or {}).get("AddressOwner") or ""
                    if owner != addr:
                        continue
                    delta = float(change.get("amount", "0"))
                    ctype = change.get("coinType", "")
                    if delta > 1_000_000 and "usdc" in ctype.lower():
                        events.append({"asset": "USDC", "amount": delta / 1e6,
                                        "tx_hash": block.get("digest", "")})
                    elif delta > 500_000_000 and "sui" in ctype.lower():
                        events.append({"asset": "SUI", "amount": delta / 1e9,
                                        "tx_hash": block.get("digest", "")})
        except Exception as exc:
            log.warning("sui deposit scan failed: %s", exc)
        return events
    return check


# ---------------------------------------------------------------- gateway


class ExecGateway:
    """Build the full execution stack from env config.

    ready = False when REAL_TRADING_ENABLED is not set or no adapters configured.
    """
    
    def __init__(self, ledger: ExecLedger, router: VenueRouter | None = None,
                 adapters: dict | None = None, killswitch: KillSwitch | None = None,
                 sync_engine: SyncEngine | None = None, deposit_watch: DepositWatch | None = None,
                 cfg: dict | None = None, vault: ExecVault | None = None):
        self.ledger = ledger
        self.router = router
        self.adapters = adapters or {}
        self.killswitch = killswitch
        self.sync_engine = sync_engine
        self.deposit_watch = deposit_watch
        self._cfg = cfg or {}
        self._vault = vault
        self.ready = bool(router and self.adapters)

    @classmethod
    def build(cls, cfg: dict | None = None, ledger_path: str | None = None) -> "ExecGateway":
        if _env(_GATE_ENV) != "1":
            return cls(ExecLedger(ledger_path or ":memory:"))

        cfg = cfg if cfg is not None else _load_execution_cfg()
        if not cfg:
            log.warning("exec gateway: REAL_TRADING_ENABLED=1 but no keys configured")
            return cls(ExecLedger(ledger_path or ":memory:"))

        vault = ExecVault()
        ledger_path = ledger_path or _env(_LEDGER_PATH, "exec_ledger.db")
        ledger = ExecLedger(ledger_path)

        adapters = build_adapters(ledger, vault, cfg)
        if not adapters:
            log.warning("exec gateway: REAL_TRADING_ENABLED=1 but no adapters built")
            return cls(ledger)

        rg = RiskGuard(profile=BotRiskProfile(
            max_notional_usd=_env_float("EXEC_MAX_NOTIONAL", 500.0),
            max_exposure_pct=_env_float("EXEC_MAX_EXPOSURE_PCT", 30.0),
            max_leverage=_env_float("EXEC_MAX_LEVERAGE", 5.0),
            require_stop=_env("EXEC_REQUIRE_STOP", "1") != "0",
            min_stop_pct=_env_float("EXEC_MIN_STOP_PCT", 2.0),
            max_stop_pct=_env_float("EXEC_MAX_STOP_PCT", 8.0),
            daily_loss_halt_pct=_env_float("EXEC_DAILY_LOSS_HALT_PCT", 3.0),
            max_open_positions=_env_int("EXEC_MAX_OPEN_POSITIONS", 5),
        ))
        ks = KillSwitch(rg, ledger)
        register_chain_hooks(ks, adapters)

        router = VenueRouter(ledger, rg, ks)
        for chain, adapter in adapters.items():
            router.register_adapter(chain, adapter)

        sync = SyncEngine(ledger)
        chain_fetchers = {
            "hyperliquid": _hl_fetcher(adapters.get("hyperliquid")),
            "solana": _sol_fetcher(adapters.get("solana")),
            "sui": _sui_fetcher(adapters.get("sui")),
        }
        for chain, fetcher in chain_fetchers.items():
            if fetcher:
                sync.register_fetcher(chain, fetcher)

        dw = DepositWatch(ledger)
        if "solana" in adapters:
            dw.register_checker("solana", _sol_deposit_checker(adapters["solana"]))
        if "sui" in adapters:
            dw.register_checker("sui", _sui_deposit_checker(adapters["sui"]))

        return cls(ledger, router, adapters, ks, sync, dw, cfg=cfg, vault=vault)

    def route(self, bot_id: int, intent, ref_price: float) -> dict:
        if not self.router:
            return {"ok": False, "error": "real execution not configured"}
        return self.router.submit(bot_id, intent, ref_price)

    def route_and_sync(self, bot_id: int, intent, ref_price: float) -> dict:
        if not self.router:
            return {"ok": False, "error": "real execution not configured"}
        return self.router.submit_and_sync(bot_id, intent, ref_price)

    def provision_wallet(self, bot_id: int, chain: str) -> int | None:
        """Ensure a wallet row exists for (bot_id, chain) in the exec ledger.
        
        Returns wallet_id or None if chain is not configured."""
        c = self._cfg.get(chain)
        if not c or not c.get("key_enc"):
            return None
        adapter = self.adapters.get(chain)
        if not adapter:
            return None
        address = getattr(adapter, "master_address", None) or getattr(adapter, "pubkey", None) or getattr(adapter, "address", None)
        if not address:
            return None
        pubkey = str(getattr(adapter, "pubkey", "") or getattr(adapter, "address", "") or "")
        key_hash = ExecVault.key_hash(self._vault.decrypt(c["key_enc"])) if self._vault else ""
        return self.ledger.upsert_wallet(bot_id, chain, str(address), pubkey, c["key_enc"], key_hash)

    def provision_all_wallets(self, bot_id: int) -> dict[str, int]:
        return {chain: wid for chain in self.adapters
                if (wid := self.provision_wallet(bot_id, chain)) is not None}

    def engage_killswitch(self, bot_id: int, reason: str) -> dict:
        if not self.killswitch:
            return {"ok": False, "error": "killswitch not configured"}
        return self.killswitch.engage(bot_id, reason)

    def release_killswitch(self, bot_id: int) -> None:
        if self.killswitch:
            self.killswitch.release(bot_id)

    def sync(self, bot_id: int, chain: str) -> dict:
        if not self.sync_engine:
            return {"ok": False, "error": "sync engine not configured"}
        return self.sync_engine.sync(bot_id, chain)

    def scan_deposits(self, bot_id: int, chain: str) -> list[dict]:
        if not self.deposit_watch:
            return []
        return self.deposit_watch.scan(bot_id, chain)