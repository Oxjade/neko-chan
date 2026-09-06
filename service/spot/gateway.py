"""SpotGateway: routes SpotOrders through the Aftermath spot adapter with
spot-specific risk checks, the shared exec ledger, and the platform fee.

Deliberately parallel to execution/gateway.py (perps) rather than merged:
spot orders never touch margin, liquidation, or funding code paths.

Wiring status: adapter API calls are implemented; the on-chain broadcast
step (sign the returned serialized tx + send via the Sui fullnode/GraphQL,
same as the perps _broadcast_tx) and the router swap-tx composition are the
next two implementation steps before this goes live.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("spot")

from execution.ledger import ExecLedger, PLATFORM_FEE_BPS
from execution.router import VenueRouter  # fee sweep reuse (USDC post-fill)
from .adapter import AftermathSpotAdapter, USDC_MAINNET_COIN_TYPE
from .order_model import SpotOrder, USDC_MAINNET_COIN_TYPE as _Q  # noqa: F401
from .risk import SpotRiskGuard, SpotRiskProfile


class SpotGateway:
    def __init__(self, ledger: ExecLedger, adapter: AftermathSpotAdapter,
                 risk: SpotRiskGuard | None = None,
                 fee_recipient: str | None = None):
        self.ledger = ledger
        self.adapter = adapter
        self.risk = risk or SpotRiskGuard()
        # Aftermath native integrator fee on spot LIMIT orders - the platform's
        # 0.5% charged on-chain at fill time. Router swaps have no integrator
        # slot; the existing USDC sweep covers those after the fill.
        self.fee_recipient = fee_recipient or os.environ.get("AFTERMATH_FEE_ADDR", "")
        self.fee_bps = PLATFORM_FEE_BPS

    def wallet_usdc(self) -> float:
        """Wallet USDC via the reliable balance() read (same source as perps)."""
        from execution.sui_adapter import SUIAdapter
        # balance reads are keyless; a throwaway adapter with any key works.
        # Long-term: lift _gql_balance into a keyless helper.
        return 0.0  # wired in the broadcast step

    def submit(self, order: SpotOrder, ref_price: float) -> dict:
        """Validate + route one spot order. Returns {ok, error|digest}."""
        violations = self.risk.check(order, self.wallet_usdc(), open_spot_symbols=set())
        if violations:
            return {"ok": False, "error": "; ".join(violations)}

        try:
            if order.order_type == "market":
                return self._execute_market(order, ref_price)
            return self._place_limit(order)
        except Exception as exc:  # noqa: BLE001
            log.exception("spot order failed")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    def _execute_market(self, order: SpotOrder, ref_price: float) -> dict:
        """Router swap: USDC -> token (buy) or token -> USDC (sell)."""
        if order.side == "buy":
            coin_in, coin_out = USDC_MAINNET_COIN_TYPE, self._coin_type(order.symbol)
            amount = order.notional(ref_price)  # USDC spent
        else:
            coin_in, coin_out = self._coin_type(order.symbol), USDC_MAINNET_COIN_TYPE
            amount = order.qty
        self.adapter.quote_route(coin_in, coin_out, amount_in=amount,
                                 slippage_bps=order.slippage_bps)
        # tx composition + broadcast: gateway wiring step (see module docstring)
        raise NotImplementedError("router swap broadcast - next implementation step")

    def _place_limit(self, order: SpotOrder) -> dict:
        """Resting spot limit order with the platform fee attached natively."""
        if order.side == "buy":
            allocate_coin, allocate_amount = USDC_MAINNET_COIN_TYPE, order.notional(order.limit_price)
        else:
            allocate_coin, allocate_amount = self._coin_type(order.symbol), order.qty
        rate = order.limit_price  # outputToInput = buyCoin per allocateCoin
        tx = self.adapter.create_limit_order_tx(
            allocate_coin_type=allocate_coin,
            allocate_amount=allocate_amount,
            buy_coin_type=self._coin_type(order.symbol) if order.side == "buy" else USDC_MAINNET_COIN_TYPE,
            rate=rate,
            expiry_ms=order.expiry_ms,
            fee_recipient=self.fee_recipient or None,
            fee_bps=self.fee_bps if self.fee_recipient else None,
            stop_rate=order.stop_loss_price,
        )
        # sign + broadcast: gateway wiring step
        raise NotImplementedError("limit-order broadcast - next implementation step")

    def _coin_type(self, symbol: str) -> str:
        """Resolve base symbol -> on-chain coin type (cached verified list)."""
        coins = self.adapter.supported_coins()
        # symbol lookup against /api/coins/verified metadata; cached mapping
        raise NotImplementedError("symbol->coinType resolution - next step")
