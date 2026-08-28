"""Funding-rate carry strategy: collect structural perp funding while delta-neutral.

Strategy (market-neutral carry, sourced from Hyperliquid's hourly funding):
  - funds flow long<->short every hour based on the perp premium over spot.
  - When funding is POSITIVE (longs pay shorts): take the SHORT perp leg and
    delta-hedge with a long spot leg of equal notional. The price legs cancel;
    profit = funding received - round-trip fees - slippage.
  - When funding is NEGATIVE (shorts pay longs): take the LONG perp leg and
    hedge with a short spot leg. Profit = |funding| - costs.

Key difference vs directional trading: we do NOT predict price. The carry is a
structural payment that persists while leverage demand is asymmetric. This is the
same mechanism the 2026 literature calls cash-and-carry / basis trade and is the
most durable crypto edge available on Hyperliquid (see skills/funding-carry/SKILL.md).

This module is the analysis + signal layer. Execution (placing the perp + spot
legs through the VenueRouter with the ledger's idempotency and fee model) is left
to the router pipeline; this layer decides WHICH symbols to carry, at WHAT size,
and validates the carry is positive after all costs.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Optional

# --------------------------------------------------------------------------- #
# Configuration (spec-frozen; see skills/funding-carry/SKILL.md section 2)
# --------------------------------------------------------------------------- #

HOURS_PER_YEAR = 24 * 365

# Minimum annualized carry (after costs) to bother carrying, decimal APY.
MIN_ANNUAL_CARRY = 0.05          # 5% APY floor
# Only act when |annualized funding| exceeds this. Below it fees eat the edge.
FUNDING_APY_FLOOR = 0.04         # 4% APY

# Cost model (per leg round-trip, decimal of notional).
TAKER_FEE_BPS = 2.5              # Hyperliquid taker perp fee, bps
SLIPPAGE_BPS = 5.0               # 0.05% baseline slippage per leg
LEG_COST_ONE_WAY = (TAKER_FEE_BPS + SLIPPAGE_BPS) / 10000.0

# Symbol universe scanned for carry. These are the HL perp names.
UNIVERSE = ["BTC", "ETH", "SOL", "SUI", "HYPE", "SEI", "NEAR", "ATOM"]

# Max notional exposure per symbol as a fraction of equity carrying.
MAX_SYMBOL_CARRY_PCT = 0.10      # 10% of equity per carry symbol

# Rebalance every hour (foreign funding resettles once/hour).
REBALANCE_HOURS = 1
# Holding period for a carry position (days). The 4-leg round-trip cost is paid
# ONCE per hold; funding accrues daily over the hold. A longer hold amortizes
# the fixed round-trip cost better but ties up capital longer.
HOLD_DAYS = 30


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Data: live funding + mark
# --------------------------------------------------------------------------- #

def _hl_info(payload: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_funding_and_mark(symbols=UNIVERSE) -> dict:
    """Return {symbol: {'funding_per_hr', 'funding_apy', 'mark', 'premium'}}.

    Uses metaAndAssetCtxs (bulk) so all symbols come from one call.
    """
    meta = _hl_info({"type": "metaAndAssetCtxs"})
    universe = meta[0]["universe"]
    ctxs = meta[1]
    name_by_index = {i: u["name"] for i, u in enumerate(universe)}
    out = {}
    for i, ctx in enumerate(ctxs):
        name = name_by_index.get(i)
        if name not in symbols:
            continue
        funding = float(ctx.get("funding", 0.0))
        out[name] = {
            "funding_per_hr": funding,
            "funding_apy": funding * HOURS_PER_YEAR,
            "mark": float(ctx.get("markPx", 0.0)),
            "premium": float(ctx.get("premium", 0.0)),
            "oracle": float(ctx.get("oraclePx", 0.0)) or float(ctx.get("markPx", 0.0)),
            "dai": ctx.get("dayNtlVlm", 0),
        }
    # also pull spot mids for the hedge leg reference
    try:
        mids = _hl_info({"type": "allMids"})
        for name in out:
            m = mids.get(name)
            if isinstance(m, str):
                out[name]["spot_mid"] = float(m)
            else:
                out[name]["spot_mid"] = out[name]["mark"]
    except Exception:
        for name in out:
            out[name]["spot_mid"] = out[name]["mark"]
    return out


# --------------------------------------------------------------------------- #
# Carry analysis
# --------------------------------------------------------------------------- #

def carry_decision(row: dict, equity: float) -> dict:
    """Compute one symbol's carry signal.

    Returns a dict that can be fed straight into the funding_carry skill's
    decision record, or None if not attractive enough.
    """
    funding_apy = row["funding_apy"]
    if abs(funding_apy) < FUNDING_APY_FLOOR:
        return None

    # Which side do we take to COLLECT the funding?
    #   funding>0 -> longs pay shorts -> we are SHORT perp (hedged long spot)
    #   funding<0 -> shorts pay longs -> we are LONG perp (hedged short spot)
    collect_side = "short_perp" if funding_apy > 0 else "long_perp"

    # Cost per one-way leg (fee + slippage); a full carry has 2 perp legs
    # (open+close) + 2 spot legs (open+close) = 4 one-way legs. The round-trip
    # cost is paid ONCE per holding period, and funding accrues daily over the
    # hold. Annualized net APY over one hold cycle:
    #     net_apy = |funding_apy| - roundtrip_cost * (365 / HOLD_DAYS)
    per_leg = LEG_COST_ONE_WAY
    total_roundtrip_cost = 4 * per_leg      # 2 perp + 2 spot, one full cycle
    annualized_roundtrip_drag = total_roundtrip_cost * (365.0 / HOLD_DAYS)

    # Notional sized to a carry-weight cap of equity.
    notional = min(equity * MAX_SYMBOL_CARRY_PCT, equity)
    notional = max(notional, 0.0)

    # Gross carry per year if held on notional.
    net_apy = abs(funding_apy) - annualized_roundtrip_drag

    # One-cycle (HOLD_DAYS) net as a fraction of notional.
    cycle_net = abs(funding_apy) * (HOLD_DAYS / 365.0) - total_roundtrip_cost

    eligible = net_apy >= MIN_ANNUAL_CARRY
    return {
        "symbol": row.get("_symbol"),
        "funding_per_hr": row["funding_per_hr"],
        "funding_apy": funding_apy,
        "collect_side": collect_side,
        "mark": row["mark"],
        "spot_mid": row.get("spot_mid", row["mark"]),
        "premium": row.get("premium", 0.0),
        "per_leg_cost": per_leg,
        "total_roundtrip_cost_frac": total_roundtrip_cost,
        "hold_days": HOLD_DAYS,
        "annualized_roundtrip_drag_apy": annualized_roundtrip_drag,
        "notional_alloc": notional,
        "net_annualized_apy": net_apy,
        "cycle_net_frac": cycle_net,
        "eligible": eligible,
        "ts": utcnow(),
    }


def scan_carry(equity: float, symbols=UNIVERSE) -> list[dict]:
    """Rank the universe by eligible net carry, best (highest |net APY|) first."""
    rows = fetch_funding_and_mark(symbols)
    for sym, r in rows.items():
        r["_symbol"] = sym
    decisions = []
    for sym in symbols:
        if sym not in rows:
            continue
        d = carry_decision(rows[sym], equity)
        if d:
            decisions.append(d)
    decisions.sort(key=lambda d: d["net_annualized_apy"], reverse=True)
    return decisions


def summarize(decisions: list[dict]) -> str:
    """Human-readable one-line-per-symbol summary for logs/prompt context."""
    lines = ["Fundings (annualized, collect-side, net APY after costs):"]
    rows = fetch_funding_and_mark()
    for sym in UNIVERSE:
        if sym not in rows:
            continue
        r = rows[sym]
        apy = r["funding_apy"]
        side = "SHORT" if apy > 0 else "LONG"
        lines.append(
            f"  {sym}: {apy*100:+.1f}% APY ({side} perp carry, fees~{4*LEG_COST_ONE_WAY*100:.2f}%/rt)"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    eq = float(sys.argv[1]) if len(sys.argv) > 1 else 100000.0
    print(summarize(scan_carry(eq)))
