"""Bundle wallet manager (§3.6). Legs are generated with the SAME code path the
onboarding `⚙️ Generate Wallet` uses (`exec_vault.generate_key_material("sui")`),
Fernet-encrypted into the owner's bot DB row, one slot per key, never reused
across bots/legs. No pasted third-party keys, no fake-distribution wallets.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "execution"))

from exec_vault import ExecVault, generate_key_material  # noqa: E402

from .db import MAX_BUNDLE_WALLETS, DegenLedger  # noqa: E402

log = logging.getLogger(__name__)


class BundleManager:
    def __init__(self, ledger: DegenLedger, vault: ExecVault | None = None):
        self.ledger = ledger
        self._vault = vault          # lazily built (needs TG_EXEC_MASTER_KEY only
                                     # when keys are actually encrypted/decrypted)

    @property
    def vault(self) -> ExecVault:
        if self._vault is None:
            self._vault = ExecVault()
        return self._vault

    def generate(self, bot_id: int, count: int = 1, label_prefix: str = "leg") -> list[dict]:
        """Fresh Sui wallets in free slots. Returns [{slot,address,fingerprint}]."""
        made = []
        for _ in range(max(0, min(count, MAX_BUNDLE_WALLETS - len(self.ledger.bundle_wallets(bot_id))))):
            address, key = generate_key_material("sui")          # same as onboarding
            enc = self.vault.encrypt(key)
            kh = ExecVault.key_hash(key)
            slot = len(self.ledger.bundle_wallets(bot_id)) + 1
            try:
                s = self.ledger.add_bundle_wallet(bot_id, f"{label_prefix}{slot}",
                                                  address, enc.encode() if isinstance(enc, str) else enc, kh)
            except ValueError:
                break
            made.append({"slot": s, "address": address, "fingerprint": ExecVault.fingerprint(key)})
        return made

    def sign_for(self, bot_id: int, wallet_addr: str) -> str | None:
        """Recover plaintext key for signing (called only inside the broadcast
        window; never logged). Returns None if the bot doesn't own it."""
        w = self.ledger.wallet_by_address(bot_id, wallet_addr)
        if not w:
            return None
        try:
            return self.vault.decrypt(bytes(w["key_enc"]) if isinstance(w["key_enc"], (bytes, bytearray)) else w["key_enc"])
        except Exception:
            log.error("bundle key decrypt failed for slot")
            return None

    def sweep(self, bot_id: int, wallet_addr: str, main_addr: str, adapter_factory) -> dict:
        """Delete leg: sweep remaining SUI to main first (gas included)."""
        w = self.ledger.wallet_by_address(bot_id, wallet_addr)
        if not w:
            return {"ok": False, "error": "not owned"}
        adapter = adapter_factory(wallet_addr)
        if adapter is None:
            return {"ok": False, "error": "no adapter"}
        try:
            bal = adapter.get_balance("SUI")
            res = adapter.transfer_asset(main_addr, bal * 0.985, asset="SUI")
            if not res.get("ok"):
                return {"ok": False, "error": res.get("error", "sweep failed")}
            self.ledger.set_bundle_wallet(bot_id, w["slot"], state="deleted")
            return {"ok": True, "digest": res.get("digest")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def funding_plan(self, bot_id: int, total_sui: float) -> list[dict]:
        """Split total into equal legs across active bundle wallets + main.
        Each leg tx is signed by ITS OWN key only (never mixed)."""
        ws = self.ledger.bundle_wallets(bot_id)
        n = max(1, len(ws))
        per = round(total_sui / (n + 1), 6)
        legs = [{"wallet": "", "amount_sui": per, "leg": "main"}]  # '' = main wallet
        for w in ws:
            legs.append({"wallet": w["address"], "amount_sui": per, "leg": w["slot"]})
        return legs

    def top_up_from_main(self, bot_id: int, main_adapter, legs: list[dict],
                         min_mist: int = 200_000_000) -> dict:
        """Ensure legs have gas: SplitCoins + TransferObjects PTB (§3.6)."""
        ws = {w["address"]: w for w in self.ledger.bundle_wallets(bot_id)}
        low = [a for a in ws if _gas(ws[a]) < min_mist]
        if not low:
            return {"ok": True, "topped": 0}
        from . import ptb
        main_addr = main_adapter.address
        coins = self.ledger.ch_coins if hasattr(self.ledger, "ch_coins") else None
        # NOTE: full funding PTB needs the chain (coin refs) — executor builds it.
        return {"ok": False, "error": "funding tx built by executor (chain required)",
                "legs": low, "per_mist": min_mist * 2}


def _gas(w) -> int:
    try:
        return int(w.get("gas_mist") or 0)
    except (TypeError, ValueError):
        return 0
