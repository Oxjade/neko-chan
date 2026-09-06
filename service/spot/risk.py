"""Spot risk guard: simpler than perps by design.

Long-only, no leverage, no liquidation. The risks that remain:
  - spending more USDC than the wallet holds
  - slippage on thin Sui books (router mitigates, we still cap)
  - illiquid tokens (min-order-size + notional floor)
"""

from dataclasses import dataclass


@dataclass
class SpotRiskProfile:
    max_notional_usd: float = 500.0        # per order
    min_notional_usd: float = 5.0          # venue minimum + dust guard
    max_slippage_bps: int = 300            # hard ceiling even if intent asks more
    max_open_spot_positions: int = 5


class SpotRiskGuard:
    def __init__(self, profile: SpotRiskProfile | None = None):
        self.profile = profile or SpotRiskProfile()

    def check(self, order, wallet_usdc: float,
              open_spot_symbols: set[str]) -> list[str]:
        """Returns violations; empty list = cleared to execute."""
        v: list[str] = []
        errs = order.validate(ref_price=order.limit_price or 1.0)
        v += errs
        if order.order_type == "limit" and order.limit_price:
            notional = order.notional(order.limit_price)
        else:
            # market orders are priced at execution; the caller passes a
            # reference price through qty checks - the wallet check below is
            # the binding constraint for buys.
            notional = 0.0
        if notional and notional > self.profile.max_notional_usd:
            v.append(f"spot notional ${notional:,.0f} > cap ${self.profile.max_notional_usd:,.0f}")
        if notional and notional < self.profile.min_notional_usd:
            v.append(f"spot notional ${notional:,.2f} < minimum ${self.profile.min_notional_usd:,.2f}")
        if order.slippage_bps > self.profile.max_slippage_bps:
            v.append(f"slippage {order.slippage_bps}bps > ceiling {self.profile.max_slippage_bps}bps")
        if order.side == "buy" and wallet_usdc < order.notional(order.limit_price or 0):
            v.append(f"wallet USDC ${wallet_usdc:,.2f} < order ${order.notional(order.limit_price or 0):,.2f}")
        if order.side == "buy" and order.symbol in open_spot_symbols:
            v.append(f"already holding {order.symbol} - one spot position per token")
        if len(open_spot_symbols) >= self.profile.max_open_spot_positions:
            v.append(f"max {self.profile.max_open_spot_positions} open spot positions")
        return v
