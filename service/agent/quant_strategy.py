"""Deterministic quant strategy engine - the validated edge, not an LLM guess.

Implements the strategy specs from skills/momentum/SKILL.md and
skills/funding-carry/SKILL.md with the absolute sizing math from those docs:

  MOMENTUM20 (skills/momentum SKILL §2): 20d return > 2% -> LONG else FLAT.
  - validated 2021-2026 backtest (backtest_risk_controlled.py): BTC +664%,
    ETH +1316% vs buy&hold +167%/+234%, Sharpe ~1.06, MaxDD ~36%.
  - stop 8% / take 24% (1:3, platform worker enforces), 1 decision/day,
    max 30% equity per position, correlated assets treated as ONE book.
  - capital preservation: risk 1% of equity per trade, half-Kelly ceiling,
    vol-targeted sizing. Cash is a position (momentum crashes in bears).

  SHORT SIDE is NEVER taken directionally (proven loser in up-drift; the 2026-08-27
  live short test). Shorts only appear as the market-neutral FUNDING-CARRY leg
  (skills/funding-carry SKILL §1), surfaced for the execution gateway, never as a
  naked directional short.

All decisions are deterministic functions of market data (no look-ahead: the
20d return uses closes strictly before the decision instant). The LLM is NOT
asked for entries; it only explains a decision already made by this engine.
"""

from __future__ import annotations

import math
import time

MOMENTUM_LOOKBACK = 20
MOMENTUM_THRESHOLD = 0.02          # r20 > 2% -> LONG
STOP_PCT = 8.0                     # 1:3 reward/risk
TAKE_PCT = 24.0
MAX_POSITION_PCT = 30.0            # of equity per position
MAX_BOOK_PCT = 30.0                # correlated positions = one book (BTC+ETH)
RISK_PER_TRADE_PCT = 1.0           # risk 1% of equity per trade
KELLY_FRACTION = 0.5               # half-Kelly ceiling
TARGET_VOL_ANNUAL = 0.20
MAX_DAILY_TRADES = 1               # momentum is 1 decision/day
MAX_OPEN = 5
# equities are NOT momentum-tradeable (skill §1: no evidence on SPY/AAPL/QQQ)
CRYPTO_ONLY = True


def momentum20_return(closes: list[float], lookback: int = MOMENTUM_LOOKBACK) -> float:
    """20d return from closes strictly before now. 0.0 when insufficient data."""
    if len(closes) < lookback + 1:
        return 0.0
    base, now = closes[-lookback - 1], closes[-1]
    if base <= 0:
        return 0.0
    return now / base - 1.0


def realized_vol(closes: list[float], window: int = 20) -> float | None:
    """Annualized stdev of returns over the last `window` closes (prompt/context only)."""
    if len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - window, len(closes))]
    if not rets:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) * math.sqrt(365)  # daily closes -> annualized


def risk_sized_units(equity: float, entry: float, stop_pct: float = STOP_PCT,
                     take_pct: float = TAKE_PCT, realized_vol: float | None = None,
                     max_pos_pct: float = MAX_POSITION_PCT) -> tuple[float, str]:
    """Absolute sizing math (skills/momentum SKILL §3): size by risk, not conviction.

    units = (equity * risk_pct) / (entry * stop_pct), then:
      - half-Kelly ceiling: f* = p - (1-p)/R with p=0.35, R=take/stop
      - vol-target cap: shrink when realized vol > target
      - hard cap: notional <= max_pos_pct of equity
    """
    if entry <= 0 or stop_pct <= 0:
        return 0.0, "entry/stop<=0"
    risk_dollars = equity * RISK_PER_TRADE_PCT / 100.0
    stop_dist = entry * stop_pct / 100.0
    units = risk_dollars / stop_dist

    R = max(1.0, take_pct / stop_pct)
    p_prior = 0.35
    f_star = p_prior - (1 - p_prior) / R
    f_used = max(0.0, f_star * KELLY_FRACTION)
    kelly_units = (equity * f_used) / stop_dist if f_used > 0 else float("inf")

    vol_mult = 1.0
    if realized_vol is not None and realized_vol > TARGET_VOL_ANNUAL * 1.5:
        vol_mult = max(0.25, min(1.0, TARGET_VOL_ANNUAL / realized_vol))

    units = min(units, kelly_units) * vol_mult
    cap_units = (equity * max_pos_pct / 100.0) / entry
    units = min(units, cap_units)
    return units, (
        f"risk {RISK_PER_TRADE_PCT:.1f}% -> {units:.6f} units "
        f"(kelly {f_used:.2f}, vol x{vol_mult:.2f}, cap {max_pos_pct:.0f}%)"
    )


class QuantDecision:
    __slots__ = ("action", "symbol", "qty", "stop_pct", "take_pct", "reasoning")

    def __init__(self, action, symbol, qty, stop_pct, take_pct, reasoning):
        self.action = action
        self.symbol = symbol
        self.qty = qty
        self.stop_pct = stop_pct
        self.take_pct = take_pct
        self.reasoning = reasoning

    def to_dict(self) -> dict:
        return {"action": self.action, "symbol": self.symbol, "quantity": self.qty,
                "stop_loss_pct": self.stop_pct, "take_profit_pct": self.take_pct,
                "reasoning": self.reasoning}


def momentum_decision(symbol: str, market: str, closes: list[float],
                      equity: float, has_long: bool, has_short: bool,
                      current_price: float) -> QuantDecision:
    """One symbol's momentum20 decision. Cash is the default (capital preservation).

    - LONG when 20d return > 2% and not already long
    - FLAT (sell) when the position exists but momentum has decayed
    - never SHORT (directional shorts are unvalidated and lose in up-drift)
    """
    if market != "crypto":
        return QuantDecision("hold", symbol, 0.0, 0.0, 0.0,
                             f"momentum20 is crypto-only (no evidence on {market})")
    r20 = momentum20_return(closes)
    if has_short:
        return QuantDecision("hold", symbol, 0.0, 0.0, 0.0,
                             f"carry/short exists - keep separate from directional book")
    if r20 <= MOMENTUM_THRESHOLD:
        if has_long:
            return QuantDecision("sell", symbol, 0.0, 0.0, 0.0,
                                 f"momentum decayed (20d {r20*100:+.2f}% <= 2%) - flat")
        return QuantDecision("hold", symbol, 0.0, 0.0, 0.0,
                             f"no momentum (20d {r20*100:+.2f}%) - stay in cash")
    if has_long:
        return QuantDecision("hold", symbol, 0.0, 0.0, 0.0,
                             f"momentum intact (20d {r20*100:+.2f}%) - hold long")
    rv = realized_vol(closes)
    qty, why = risk_sized_units(equity, current_price, STOP_PCT, TAKE_PCT, rv)
    if qty <= 0:
        return QuantDecision("hold", symbol, 0.0, 0.0, 0.0,
                             f"momentum (20d {r20*100:+.2f}%) but sizing rejected ({why})")
    return QuantDecision("buy", symbol, qty, STOP_PCT, TAKE_PCT,
                         f"momentum20 LONG (20d {r20*100:+.2f}%) - {why}")


def scan_momentum_book(portfolio: dict, closes_by_symbol: dict, prices: dict) -> list[QuantDecision]:
    """Decide every crypto symbol once per cycle. Correlated long book capped."""
    equity = portfolio.get("cash", 0.0)
    positions = portfolio.get("positions", [])
    for p in positions:
        px = prices.get(p["symbol"]) or p.get("current_price") or p["entry_price"]
        if p["quantity"] >= 0:
            equity += p["quantity"] * px
        else:
            equity += abs(p["quantity"]) * (2 * p["entry_price"] - px)

    decisions: list[QuantDecision] = []
    for symbol, closes in closes_by_symbol.items():
        if not closes:
            continue
        has_long = any(p["symbol"] == symbol and p["quantity"] > 0 for p in positions)
        has_short = any(p["symbol"] == symbol and p["quantity"] < 0 for p in positions)
        px = prices.get(symbol, 0)
        if px <= 0:
            continue
        d = momentum_decision(symbol, "crypto", closes, equity, has_long, has_short, px)
        decisions.append(d)

    # enforce the correlated-book cap: if a NEW long would push total crypto
    # long notional > 30% of equity, demote it to hold (capital preservation).
    long_notional = sum(abs(p["quantity"]) * (prices.get(p["symbol"]) or p["entry_price"])
                        for p in positions if p["quantity"] > 0)
    for d in decisions:
        if d.action == "buy" and d.qty > 0:
            px = prices.get(d.symbol, 0)
            add_notional = d.qty * px
            if long_notional + add_notional > equity * MAX_BOOK_PCT / 100.0:
                d.action = "hold"
                d.qty = 0.0
                d.reasoning = f"book cap: crypto longs would exceed {MAX_BOOK_PCT:.0f}% of equity"
            else:
                long_notional += add_notional
    return decisions


def pick_decision(decisions: list[QuantDecision]) -> QuantDecision:
    """The one action to execute this cycle (momentum is 1 decision/day).

    Priority: a SELL/flat first (exit discipline wins over entry), then a fresh
    LONG on the strongest momentum symbol we don't already hold.
    """
    exits = [d for d in decisions if d.action == "sell"]
    if exits:
        return exits[0]
    entries = [d for d in decisions if d.action == "buy" and d.qty > 0]
    if not entries:
        return QuantDecision("hold", "", 0.0, 0.0, 0.0,
                             "momentum book: no entry, no exit - cash")
    import re
    pct_re = re.compile(r"20d ([+-][\d.]+)%")
    def momentum_of(d: QuantDecision) -> float:
        m = pct_re.search(d.reasoning)
        return float(m.group(1)) if m else 0.0
    entries.sort(key=momentum_of, reverse=True)
    return entries[0]