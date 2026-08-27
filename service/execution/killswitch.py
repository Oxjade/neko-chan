"""Kill-switch orchestration: flat positions + cancel open orders on every chain.

Adapters register a `flat_and_cancel()` hook; the killswitch calls them and
records the outcome. If any adapter fails, the switch stays ENGAGED and the
failure is logged - a kill-switch never silently succeeds.
"""

import logging
import threading

log = logging.getLogger("execution")


class KillSwitch:
    def __init__(self, risk_guard, ledger):
        self.risk_guard = risk_guard
        self.ledger = ledger
        self._hooks: dict[str, callable] = {}  # chain -> flat_and_cancel()
        self._lock = threading.RLock()

    def register_hook(self, chain: str, hook: callable) -> None:
        with self._lock:
            self._hooks[chain] = hook

    def engage(self, bot_id: int, reason: str) -> dict:
        """Flat + cancel everywhere. Returns per-chain results."""
        self.risk_guard.engage_killswitch(bot_id)
        results = {}
        with self._lock:
            chains = list(self._hooks.keys())
        if not chains:
            # nothing was flattened - a kill-switch must never silently succeed
            return {"bot_id": bot_id, "reason": reason, "results": {},
                    "fully_flattened": False, "error": "no chain adapters registered"}
        for chain in chains:
            hook = self._hooks.get(chain)
            if not hook:
                results[chain] = {"ok": False, "error": "no hook registered"}
                continue
            try:
                results[chain] = hook(bot_id) or {"ok": True}
            except Exception as exc:  # noqa: BLE001
                log.error("killswitch %s hook failed for bot %s: %s", chain, bot_id, exc)
                results[chain] = {"ok": False, "error": str(exc)[:200]}
        # any failure -> stay engaged (never silently partial)
        failed = [c for c, r in results.items() if not r.get("ok")]
        if failed:
            log.error("killswitch partial failure for bot %s on chains: %s", bot_id, failed)
        return {"bot_id": bot_id, "reason": reason, "results": results,
                "fully_flattened": not failed}

    def release(self, bot_id: int) -> None:
        self.risk_guard.release_killswitch(bot_id)