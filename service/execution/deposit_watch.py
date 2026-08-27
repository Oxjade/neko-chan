"""Deposit watch: detect incoming USDC + native-chain funding via RPC, flip
wallet state, emit a push event. V1 adapter protocol: each chain provides
`check_funding(wallet) -> list[{asset, amount, tx_hash}]`."""

import logging

log = logging.getLogger("execution")


class DepositWatch:
    def __init__(self, ledger, notifier=None):
        self.ledger = ledger
        self.notifier = notifier  # optional: fn(wallet_id, text) -> None
        self._checkers: dict[str, callable] = {}  # chain -> check_funding(wallet)

    def register_checker(self, chain: str, checker: callable) -> None:
        self._checkers[chain] = checker

    def scan(self, bot_id: int, chain: str) -> list[dict]:
        """Poll one wallet for new deposits. Returns confirmed deposits."""
        wallet = self.ledger.wallet_by_bot_chain(bot_id, chain)
        if not wallet:
            return []
        checker = self._checkers.get(chain)
        if not checker:
            return []
        found = []
        try:
            events = checker(wallet) or []
        except Exception as exc:  # noqa: BLE001
            log.error("deposit scan failed chain=%s bot=%s: %s", chain, bot_id, exc)
            return []
        for ev in events:
            asset = ev.get("asset")
            amount = float(ev.get("amount", 0) or 0)
            tx = ev.get("tx_hash", "")
            if not asset or amount <= 0 or not tx:
                continue
            deposit_id = self.ledger.record_deposit(wallet["id"], asset, amount, tx)
            self.ledger.confirm_deposit(deposit_id)
            self.ledger.set_wallet_status(wallet["id"], "active")
            found.append({"asset": asset, "amount": amount, "tx_hash": tx})
            if self.notifier:
                try:
                    self.notifier(wallet["id"],
                                  f"💰 Deposit: +${amount:,.2f} {asset} on {chain} — wallet ACTIVE ✅")
                except Exception as exc:  # noqa: BLE001
                    log.error("deposit push failed: %s", exc)
        return found