"""Spot order model: long-only intents on Sui tokens.

Deliberately separate from execution/order_model.py (perps):
  - No leverage, no shorts, no funding: a spot intent is buy TOKEN with USDC,
    or sell TOKEN back to USDC.
  - Quote asset is always the wallet's USDC (canonical Circle USDC on mainnet).
  - Two execution styles:
      market  -> Router swap (best route across Sui DEXs, instant)
      limit   -> Aftermath spot limit-order book (resting, on-chain)
"""

from dataclasses import dataclass
from typing import Optional

QUOTE_ASSET = "USDC"

# Canonical Circle USDC on Sui mainnet (same coin type the perps stack uses).
USDC_MAINNET_COIN_TYPE = (
    "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
)

# Venue id registered in execution/order_model.VENUES by the gateway wiring
# (kept here so the spot package is self-describing).
VENUE_ID = "aftermath-spot"

# Router swap slippage default (bps). Router quotes carry price impact; the
# tolerance only guards execution drift between quote and on-chain execution.
DEFAULT_SLIPPAGE_BPS = 100  # 1.0% - spot books on Sui are thinner than perps

# Spot limit orders: expiry after which the resting order is cancelled by the
# venue. One trading day default for the bot's one-shot interday flow.
DEFAULT_LIMIT_EXPIRY_MS = 24 * 3600 * 1000


@dataclass
class SpotOrder:
    """One spot intent. `side` is always from the user's USDC perspective."""
    chain: str                    # "sui"
    venue: str                    # "aftermath-spot"
    symbol: str                   # base symbol, e.g. "DEEP" (quote = USDC)
    side: str                     # "buy" | "sell"
    qty: float                    # base-token amount
    order_type: str = "market"    # "market" (router swap) | "limit" (resting)
    limit_price: Optional[float] = None     # required for limit, in USDC
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS
    expiry_ms: int = DEFAULT_LIMIT_EXPIRY_MS
    idempotency_key: str = ""
    # stop-loss exchange rate for resting spot limits (optional; venue-native)
    stop_loss_price: Optional[float] = None

    def notional(self, ref_price: float) -> float:
        return self.qty * ref_price

    def validate(self, ref_price: float) -> list[str]:
        errors: list[str] = []
        if self.chain != "sui":
            errors.append(f"spot trading is Sui-only (got chain {self.chain})")
        if self.venue != VENUE_ID:
            errors.append(f"unknown spot venue {self.venue}")
        if self.side not in ("buy", "sell"):
            errors.append("spot side must be buy or sell (no shorts on spot)")
        if self.qty <= 0 or not float(self.qty) > 0:
            errors.append("qty must be positive")
        if ref_price <= 0:
            errors.append("reference price must be positive")
        if self.order_type not in ("market", "limit"):
            errors.append(f"unknown spot order_type {self.order_type}")
        if self.order_type == "limit" and not (self.limit_price and self.limit_price > 0):
            errors.append("limit orders require a positive limit_price")
        if not self.idempotency_key:
            errors.append("idempotency_key required")
        return errors
