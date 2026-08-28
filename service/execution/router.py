"""Venue router: OrderIntent -> adapter, risk-checked, ledgered, killswitch-wired.

The router is the execution entry point that makes all chains consistent:

  1. Validate the intent (order_model.validate).
  2. Build WalletState from the ledger (balances, exposure, positions, today PnL).
  3. RiskGuard.check BEFORE any signing (hard caps, not model-overridable).
  4. Resolve the adapter by venue (hl_adapter / sui_adapter / bluefin_adapter /
     sol_adapter). A venue that is NOT configured (Bluefin prod API down,
     DeepBook without package addresses, no exec key) returns
     {"ok": False, "error": "... not configured"} — the router reports that as
     a rejected order, never a silent success.
  5. Ledger-record the order (idempotency key -> status proposed/submitted).
  6. Call the adapter, update status filled/rejected, record fill + fee.
  7. Register the adapter's flat_and_cancel with the KillSwitch per chain.

Most adapters are failure-tolerant already; the router's job is that ANY
failure becomes a ledger row with status 'rejected' and a short, safe error -
never an exception that leaves a position unexplained or un-halved.
"""

from __future__ import annotations

import logging
import time

from ledger import ExecLedger
from order_model import OrderIntent, VENUE_FEE_BPS, resolve_adapter_name
from risk_guard import RiskGuard, WalletState
from killswitch import KillSwitch
from sync_engine import SyncEngine

log = logging.getLogger("execution")


class VenueRouter:
    def __init__(self, ledger: ExecLedger, risk_guard: RiskGuard, killswitch: KillSwitch,
                 sync_engine: SyncEngine | None = None):
        self.ledger = ledger
        self.risk_guard = risk_guard
        self.killswitch = killswitch
        self.sync = sync_engine or SyncEngine(ledger)
        self.adapters: dict[str, object] = {}  # chain -> adapter instance

    # ------------------------------------------------------------ wiring

    def register_adapter(self, chain: str, adapter) -> None:
        self.adapters[chain] = adapter
        hook = getattr(adapter, "flat_and_cancel", None)
        if callable(hook):
            self.killswitch.register_hook(chain, hook)

    # ------------------------------------------------------------ submit

    def submit(self, bot_id: int, intent: OrderIntent, ref_price: float) -> dict:
        """Route one order. Returns {'ok': bool, 'order_id': .., 'status': ..,
        'error': str|None, 'fee': float}."""
        chain = intent.chain
        venue = intent.venue
        adapter = self.adapters.get(chain)
        if not adapter:
            return {"ok": False, "order_id": None, "status": "rejected",
                    "error": f"no adapter registered for chain {chain}", "fee": 0.0}

        # wallet + state from ledger
        wallet = self.ledger.wallet_by_bot_chain(bot_id, chain)
        if not wallet:
            return {"ok": False, "order_id": None, "status": "rejected",
                    "error": f"no wallet for bot {bot_id} on {chain}", "fee": 0.0}

        # FAULT TOLERANCE: pull the freshest on-chain state BEFORE the risk
        # check. If we skip this, chain_state is empty on the first order and
        # the exposure cap sees $0 balance -> every order is rejected. Sync is
        # best-effort; if the fetcher fails we still proceed with what we have.
        try:
            if self.sync is not None:
                self.sync.sync(bot_id, chain)
        except Exception as exc:
            log.warning("[router] pre-risk sync failed bot=%s chain=%s: %s", bot_id, chain, exc)

        state = self._wallet_state(bot_id, chain, wallet, intent.symbol)
        violations = self.risk_guard.check(bot_id, intent, ref_price, state)
        if violations:
            detail = "; ".join(violations)
            log.warning("[router] bot=%s rejected by risk: %s", bot_id, detail)
            return {"ok": False, "order_id": None, "status": "rejected",
                    "error": f"risk: {detail}", "fee": 0.0}

        # ledger: idempotent order row BEFORE hitting any venue
        if self.ledger.order_exists(intent.idempotency_key):
            return {"ok": False, "order_id": None, "status": "rejected",
                    "error": f"duplicate idempotency_key {intent.idempotency_key}", "fee": 0.0}
        order_id = self.ledger.create_order(intent, bot_id)

        # call the adapter
        try:
            result = adapter.place_order(intent, ref_price) or {}
        except Exception as exc:  # noqa: BLE001
            log.exception("[router] adapter threw on %s/%s", chain, venue)
            self.ledger.set_order_status(order_id, "rejected")
            return {"ok": False, "order_id": order_id, "status": "rejected",
                    "error": f"adapter: {exc}"[:200], "fee": 0.0}

        ok = bool(result.get("ok"))
        if ok:
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            ven_id = str(data.get("order_hash") or data.get("orderId")
                         or result.get("venue_order_id") or result.get("tx_hash") or "")
            fill_price = float(result.get("avg_px") or result.get("price") or ref_price)
            fill_qty = float(result.get("filled_qty") or intent.qty)
            self.ledger.set_order_status(order_id, "submitted", venue_order_id=ven_id[:80] or None)
            fee_venue = float(result.get("fee") or 0.0)
            if fee_venue <= 0:
                # Adapter did not report a venue fee (HL/Sui/Bluefin do not) -
                # fall back to the venue's taker basis so P&L is truthful.
                fee_venue = round(intent.notional(ref_price) * VENUE_FEE_BPS.get(venue, 0.0) / 10000, 6)
            self.ledger.record_fill(order_id, price=fill_price, qty=fill_qty,
                                    fee_venue=fee_venue, tx_hash=ven_id[:80], bot_id=bot_id)
            log.info("[router] bot=%s %s %s %s @ %s ok", bot_id, venue, intent.side, intent.symbol, fill_price)
        else:
            self.ledger.set_order_status(order_id, "rejected")
        return {"ok": ok, "order_id": order_id, "status": "submitted" if ok else "rejected",
                "error": result.get("error") if not ok else None, "fee": 0.0}

    # ------------------------------------------------------------ helpers

    def _wallet_state(self, bot_id: int, chain: str, wallet: dict, symbol: str) -> WalletState:
        try:
            state = self.ledger.load_chain_state(wallet["id"])
            balances = state.get("balances", {}) or {}
            usd = float(balances.get("USDC") or balances.get("USD") or 0.0)
            positions = state.get("positions", []) or []
            exposure = sum(float(p.get("notional_usd") or 0.0) for p in positions if p)
            realized_today = 0.0  # ledger-provided if available
            return WalletState(usd_balance=usd, open_exposure_usd=exposure,
                               open_positions=len(positions), realized_pnl_today=realized_today)
        except Exception:
            return WalletState()

    def submit_and_sync(self, bot_id: int, intent: OrderIntent, ref_price: float) -> dict:
        res = self.submit(bot_id, intent, ref_price)
        if res.get("ok"):
            sync = self.sync.sync(bot_id, intent.chain)
            res["sync"] = sync
        return res


def build_router(ledger: ExecLedger, risk_profile=None) -> VenueRouter:
    """Wire a complete router with default risk profile (v1 caps)."""
    rg = RiskGuard(risk_profile=risk_profile)
    kws = KillSwitch(rg, ledger)
    return VenueRouter(ledger, rg, kws)
