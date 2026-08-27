"""Sync engine: pull on-chain snapshots per wallet, reconcile against the local
chain_state cache, log drift instead of trusting the cache."""

import logging
from datetime import datetime, timezone

log = logging.getLogger("execution")


class SyncEngine:
    def __init__(self, ledger, fetchers: dict | None = None):
        self.ledger = ledger
        self.fetchers = fetchers or {}  # chain -> fn(wallet) -> {balances, positions, orders}
        self.drift_events = []

    def register_fetcher(self, chain: str, fetcher: callable) -> None:
        self.fetchers[chain] = fetcher

    def sync(self, bot_id: int, chain: str) -> dict:
        wallet = self.ledger.wallet_by_bot_chain(bot_id, chain)
        if not wallet:
            return {"ok": False, "error": "wallet not found"}
        fetcher = self.fetchers.get(chain)
        if not fetcher:
            return {"ok": False, "error": f"no fetcher for {chain}"}
        try:
            snapshot = fetcher(wallet) or {}
        except Exception as exc:  # noqa: BLE001
            log.error("sync fetch failed chain=%s bot=%s: %s", chain, bot_id, exc)
            return {"ok": False, "error": str(exc)[:200]}

        cached = self.ledger.load_chain_state(wallet["id"])
        self.ledger.save_chain_state(
            wallet["id"],
            snapshot.get("balances", {}),
            snapshot.get("positions", []),
            snapshot.get("orders", []),
        )
        drift = None
        if cached:
            found = self._diff(cached, snapshot)
            if found:
                drift = found
                self.drift_events.append({
                    "bot_id": bot_id, "chain": chain,
                    "at": datetime.now(timezone.utc).isoformat(), "drift": found,
                })
                log.warning("chain drift bot=%s chain=%s: %s", bot_id, chain, found)
        return {"ok": True, "drift": drift, "synced_at": datetime.now(timezone.utc).isoformat()}

    def _diff(self, cached: dict, fresh: dict) -> list[str]:
        problems = []
        cb = cached.get("balances") or {}
        fb = fresh.get("balances") or {}
        for asset in set(cb) | set(fb):
            if abs(float(cb.get(asset, 0)) - float(fb.get(asset, 0))) > 1e-6:
                problems.append(f"balance {asset}: cached {cb.get(asset)} != on-chain {fb.get(asset)}")
        cp = sorted(str(p) for p in (cached.get("positions") or []))
        fp = sorted(str(p) for p in (fresh.get("positions") or []))
        if cp != fp:
            problems.append("positions differ from on-chain")
        return problems