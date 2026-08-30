"""Order model: one intent schema across all venues, validated purely."""

from dataclasses import dataclass, field
from typing import Optional

VENUES = {
    "hl-perp": "hyperliquid",
    "deepbook-spot": "sui",
    "deepbook-margin": "sui",
    "bluefin-perp": "sui",
    "jup-perp": "solana",
    "jup-limit": "solana",
    "xstocks-spot": "solana",
}

MAX_LEVERAGE_BY_VENUE = {
    "hl-perp": 50,
    "deepbook-margin": 10,
    "bluefin-perp": 100,
    "jup-perp": 100,
}

# Venue taker fee in basis points (1/10000) per venue, used by the router to
# report a truthful venue fee on every fill even when an adapter does not
# return one. Values are conservative taker rates (2026):
#   hl-perp / bluefin-perp  ~2.5 bps, jup-perp ~10 bps, deepbook spot ~0
#   (maker-oriented), xstocks/jup-limit ~10 bps.
VENUE_FEE_BPS = {
    "hl-perp": 2.5,
    "deepbook-spot": 0.0,
    "deepbook-margin": 2.5,
    "bluefin-perp": 2.5,
    "jup-perp": 10.0,
    "jup-limit": 10.0,
    "xstocks-spot": 10.0,
}


@dataclass
class OrderIntent:
    chain: str
    venue: str
    symbol: str
    side: str  # buy | sell
    qty: float
    order_type: str = "market"  # market | limit | stop | take_profit
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    leverage: float = 1.0
    idempotency_key: str = ""

    def notional(self, ref_price: float) -> float:
        return self.qty * ref_price

    def validate(self, ref_price: float) -> list[str]:
        errors = []
        if self.chain not in {"hyperliquid", "sui", "solana"}:
            errors.append(f"unknown chain {self.chain}")
        if self.venue not in VENUES:
            errors.append(f"unknown venue {self.venue}")
        elif VENUES[self.venue] != self.chain:
            errors.append(f"venue {self.venue} does not belong to chain {self.chain}")
        if self.side not in ("buy", "sell"):
            errors.append("side must be buy or sell")
        if self.qty <= 0 or not float(self.qty) > 0:
            errors.append("qty must be positive")
        if ref_price <= 0:
            errors.append("reference price must be positive")
        cap = MAX_LEVERAGE_BY_VENUE.get(self.venue, 1)
        if not (1 <= self.leverage <= cap):
            errors.append(f"leverage {self.leverage} out of range [1,{cap}] for {self.venue}")
        if self.order_type not in ("market", "limit", "stop", "take_profit"):
            errors.append(f"unknown order_type {self.order_type}")
        if self.order_type in ("limit", "stop", "take_profit") and not (self.limit_price and self.limit_price > 0):
            errors.append(f"{self.order_type} orders require a limit price")
        if not self.idempotency_key:
            errors.append("idempotency_key required")
        return errors

    def venue_cap(self) -> int:
        return MAX_LEVERAGE_BY_VENUE.get(self.venue, 1)


def resolve_adapter_name(venue: str) -> str:
    return {
        "hl-perp": "hl_adapter",
        "deepbook-spot": "sui_adapter",
        "deepbook-margin": "sui_adapter",
        "bluefin-perp": "sui_adapter",
        "jup-perp": "sol_adapter",
        "jup-limit": "sol_adapter",
        "xstocks-spot": "sol_adapter",
    }.get(venue, "unknown")