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
STOP_PCT = 8.0                     # 1R risk distance
TAKE_PCT = 24.0                    # 3R base (vol-adaptive, see adaptive_take_pct)
TAKE_MIN = 12.0                    # narrowest target (at 2× normal vol)
TAKE_MAX = 40.0                    # widest target (at 0.5× normal vol)
# ---- sentiment tail-risk adjuster (Fear & Greed, 0-100) ----
# Only acts at the extremes; in the middle it does nothing so the validated
# 20d momentum math runs untouched. Greed >= GREED_HOT tightens (stretched
# market reverses harder); fear <= FEAR_COLD widens (don't get shaken out of
# an oversold recovery). Extreme-greed adjustment caps at a 1.5R target so the
# payoff ratio never goes below the Kelly-positive line.
GREED_HOT = 90.0
FEAR_COLD = 15.0
GREED_STOP_CUT = 2.0               # stop 8% -> 6% in extreme greed
GREED_TARGET_CUT = 6.0             # take 24% -> 18% in extreme greed
FEAR_STOP_WIDEN = 2.0              # stop 8% -> 10% in extreme fear
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


def adaptive_take_pct(realized_vol: float | None, base_take: float = TAKE_PCT,
                      stop_pct: float = STOP_PCT) -> float:
    """Take-profit target scaled by realized volatility, never below the frozen spec.

    Math: the frozen momentum spec (skills/momentum SKILL §2) is a binding 1:3
    reward/risk — take 24% / stop 8%. That 24% is a FLOOR, not a starting point:
    tightening it raises the breakeven win rate and destroys the documented edge
    (the LLM decision layer enforces this). Volatility only ever WIDENS the target
    when the trend is calm and more likely to persist:

      multiplier = clamp(target_vol / realized_vol, 1.0, 1.5)   # never < 1.0
      take_pct   = base_take * multiplier                        # >= 24%

    Example (target_vol=20% annualized):
      realized 40% -> x1.0 -> take 24% (3R)   frozen spec, hot market
      realized 20% -> x1.0 -> take 24% (3R)   normal
      realized 10% -> x1.5 -> take 36% (4.5R) calm -> let it run
    """
    if not realized_vol or realized_vol <= 0:
        return base_take
    mult = max(1.0, min(1.5, TARGET_VOL_ANNUAL / realized_vol))
    take = base_take * mult
    return max(take, base_take)  # never below the frozen 3R spec


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


def sentiment_risk_adjust(stop_pct: float, take_pct: float,
                          fear_greed: float | None) -> tuple[float, float, str]:
    """Adjust stop/target ONLY at extreme sentiment (tail-risk control).

    Returns (stop_pct, take_pct, note). Middle sentiment (15 < fg < 90) leaves
    the validated math untouched. At the extremes:
      - Greed >= 90: stretch -> tighten stop & target (take profit before the
        snap-back). Target never below 1.5R (Kelly-positive floor).
      - Fear  <= 15: oversold -> widen stop (don't get shaken out), keep target.
    """
    if fear_greed is None:
        return stop_pct, take_pct, ""
    if fear_greed >= GREED_HOT:
        s = max(stop_pct - GREED_STOP_CUT, 4.0)
        t = max(take_pct - GREED_TARGET_CUT, s * 1.5)
        return s, t, f"extreme greed ({fear_greed:.0f}) -> tighter stop {s:.0f}%/target {t:.0f}%"
    if fear_greed <= FEAR_COLD:
        s = stop_pct + FEAR_STOP_WIDEN
        return s, take_pct, f"extreme fear ({fear_greed:.0f}) -> wider stop {s:.0f}%"
    return stop_pct, take_pct, ""


def momentum_decision(symbol: str, market: str, closes: list[float],
                      equity: float, has_long: bool, has_short: bool,
                      current_price: float, fear_greed: float | None = None) -> QuantDecision:
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
    take = adaptive_take_pct(rv)
    stop, take, senti_note = sentiment_risk_adjust(STOP_PCT, take, fear_greed)
    qty, why = risk_sized_units(equity, current_price, stop, take, rv)
    if qty <= 0:
        return QuantDecision("hold", symbol, 0.0, 0.0, 0.0,
                             f"momentum (20d {r20*100:+.2f}%) but sizing rejected ({why})")
    note = f"; {senti_note}" if senti_note else ""
    return QuantDecision("buy", symbol, qty, stop, take,
                         f"momentum20 LONG (20d {r20*100:+.2f}%) - {why}{note}")


def trailing_stop_pct(entry: float, current: float, stop_pct: float = STOP_PCT) -> float | None:
    """Trailing stop-loss distance (from the PEAK) as a winner advances.

    Math: once the position is up >= 1R (one full stop distance), activate a
    trailing stop that sits 0.5R below the running peak. As the peak rises the
    stop ratchets up with it, so a winning run never gives back more than half
    a full stop-distance of its peak gain. Returns the trailing distance in %
    (of the peak) to use, or None if not yet activated (keep the original stop).
    """
    if entry <= 0 or current <= entry:
        return None
    gain_pct = (current / entry - 1.0) * 100.0
    if gain_pct < stop_pct:  # not up a full R yet - keep original stop
        return None
    return stop_pct / 2.0  # trail 0.5R below the peak, ratcheting with it


def trail_check(positions: list[dict], prices: dict) -> list[QuantDecision]:
    """Manage open longs: ratchet the stop as winners run, exit on trail break.

    For each open long, track the highest price since entry (peak). Once up a
    full stop-distance, place a trail stop 0.5R below the peak. If price falls
    to the trail stop, emit a SELL to lock the profit. Never gives back more
    than half the peak gain. Peak is approximated from current price (the worker
    re-marks positions every 5 min, so peak is the max seen this session).
    """
    exits = []
    for p in positions:
        symbol = p.get("symbol")
        qty = p.get("quantity", 0)
        if qty <= 0:  # longs only; shorts/carry handled separately
            continue
        entry = float(p.get("entry_price") or 0)
        cur = prices.get(symbol) or p.get("current_price") or entry
        high = float(p.get("high_price") or p.get("trailing_high") or cur or entry)
        if cur <= 0 or entry <= 0:
            continue
        peak = max(high, cur)
        trail = trailing_stop_pct(entry, peak, STOP_PCT)
        if trail is None:
            continue  # not far enough in profit to trail yet
        # trail break = current price fell to the ratcheted stop (below peak)
        stop_price = peak * (1 - trail / 100.0)
        if cur <= stop_price:
            exits.append(QuantDecision(
                "sell", symbol, 0.0, 0.0, 0.0,
                f"trailing stop hit: +{(peak/entry-1)*100:.1f}% peak, "
                f"exited ~{(peak-cur)/peak*100:.1f}% off the high"))
    return exits


def scan_momentum_book(portfolio: dict, closes_by_symbol: dict, prices: dict,
                       fear_greed: float | None = None) -> list[QuantDecision]:
    """Decide every crypto symbol once per cycle. Correlated long book capped.

    fear_greed: Fear & Greed Index (0-100). Only acts at extremes (>=90 or
    <=15) as a tail-risk stop/target adjuster — middle range leaves the
    validated momentum math untouched.
    """
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
        d = momentum_decision(symbol, "crypto", closes, equity, has_long, has_short, px,
                              fear_greed=fear_greed)
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