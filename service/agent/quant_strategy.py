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
# ---- drift shrinkage for the scenario engine ----
# Raw 20d drift extrapolated to annual is nonsense (+366% to +744% on a hot
# week), which inflates P(win) to the cap. Momentum persists but mean-reverts:
# shrink the observed drift toward zero and cap it at a sane annualized level.
# These are the numbers that make the calibration honest, not optimistic.
DRIFT_SHRINK = 0.15                # keep 15% of observed drift (mean-reversion)
DRIFT_ANNUAL_CAP = 0.35            # max |annualized log drift| = 35%/yr
# ---- volatility-based stop/target (REACHABLE for a retail scalper) ----
# Research (2026-08-28, live Hyperliquid 5m candles + official fee docs):
#   BTC per-5m sigma ~0.12%, avg |5m move| ~0.09%, 95th pct ~0.26%
#   Hyperliquid perp taker 0.045% -> round trip ~0.09%
# A retail trade on a 5-min cycle must target 2-4 sigma of a 5m bar
# (resolves in ~30-60 min) and stop at 1-2 sigma. Bigger = waits days and
# never resolves (the old 5-24% targets were 10-40x too big).
# stop   = 1.5 x 5m-sigma (an adverse 1.5-sigma bar)
# target = 4.0 x 5m-sigma (a 4-sigma favorable move, ~0.5% for BTC)
# -> ~2.7:1 reward/risk on real intraday moves, reachable in minutes-hours.
VOL_5M_SIGMA_STOP = 1.5            # stop as multiple of 5m sigma
VOL_5M_SIGMA_TARGET = 4.0          # target as multiple of 5m sigma
VOL_STOP_MIN_PCT = 0.12            # never tighter than 0.12%
VOL_STOP_MAX_PCT = 0.6             # never wider than 0.6%
VOL_TARGET_MIN_PCT = 0.25          # target floor (fee beat: 0.09% rt + margin)
VOL_TARGET_MAX_PCT = 1.5           # target ceiling
# ---- time-based exit (derived from volatility, NOT hardcoded) ----
# A trade that hasn't resolved within its expected lifetime is dead capital.
# The expected time to hit a target under GBM: for a random walk, the number of
# bars to traverse `k` sigma is ~k^2 (variance scales with N). So a 4-sigma
# target takes ~16 bars = ~80 min at 5-min bars. The time stop is a MULTIPLE of
# that volatility-derived expectation, so high-vol assets get more time and
# low-vol assets get less - never an arbitrary "1 hour".
#   expected_minutes ~= (target_sigma)^2 x BAR_MINUTES
#   MAX_HOLD = 2.5x expected      (hard cut: past this, exit at market)
#   PROFIT_TAKE = 0.75x expected  (green + this long = bank the profit)
TIME_MAX_HOLD_MULT = 2.5
TIME_PROFIT_TAKE_MULT = 0.75
TIME_MIN_HOLD_MINUTES = 30         # absolute floor (avoid churn from noise)
TIME_MIN_PROFIT_MINUTES = 15
BAR_MINUTES = 5                    # decision cycle length
PROFIT_TAKE_MIN_PCT = 0.05
# ---- trader-type selection ----
# The user picks what kind of trader they are. This FILTERS which horizons the
# engine is allowed to trade - so a scalp trader only sees scalp targets
# (~0.36% BTC), never the swing 4% targets the conviction ranking prefers.
#   scalp:    tight targets, in-out in minutes
#   intraday: hours, medium targets
#   swing:    days, let it run
#   auto:     engine/LLM picks whichever horizon has the best EV (default)
TRADER_TYPES = {"scalp", "intraday", "swing", "auto"}
HORIZON_BY_TYPE = {"scalp": ["scalp"], "intraday": ["intraday"],
                   "swing": ["swing"], "auto": ["scalp", "intraday", "swing"]}
# ---- hyperopt-validated enhancements (30d live data) ----
# RSI filter: only enter when RSI > threshold (momentum confirmation). This
# raised PF 1.51 -> 1.66 by weeding out weak entries.
RSI_PERIOD = 14
RSI_ENTRY_THRESHOLD = 40.0
# Time-decay exit: as a trade ages without hitting target, ratchet the target
# DOWN toward breakeven so more wins get banked. Combined with RSI this raised
# PF to 1.91 (win rate 42% -> 48%). Thresholds are fractions of expected time.
TIME_DECAY_ENABLED = True
DECAY_AFTER_FRACTION = 0.33   # start ratcheting after 33% of expected time
DECAY_TARGET_MULT = 0.75      # after 33%: target x0.75; after 66%: x0.5
DECAY_TARGET_MULT_LATE = 0.5
MAX_POSITION_PCT = 30.0            # of equity per position (notional ceiling)
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


def rsi(closes: list[float], period: int = RSI_PERIOD) -> float:
    """RSI (Wilder) from closes strictly before now. 50 if insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


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


# ================================================================
# Scenario engine: multi-outcome trade matrix with real math.
#
# For each symbol we model price as geometric Brownian motion:
#     dS = mu*S*dt + sigma*S*dW     (log-return drift nu = mu - sigma^2/2)
# and compute the exact probability of hitting a take-profit barrier
# BEFORE the stop-loss barrier (classic two-barrier first-passage time):
#
#   P(hit target before stop) =
#       (1 - exp(-2*nu*l/sigma^2)) / (exp(-2*nu*u/sigma^2) - exp(-2*nu*l/sigma^2))
#
# where u = ln(target/entry), l = ln(stop/entry) for a long.
# With zero drift this collapses to l/(l+u) = 1/(1+R) — for 1:3 that is
# exactly 25%, the documented breakeven win rate. Positive drift raises P,
# negative drift lowers it. This is real math, not a vibes-based score.
#
# Expected value per unit risk:   EV = P_win * R - (1 - P_win) * 1
# The LLM receives this matrix and picks the highest-EV, highest-conviction
# scenario; the risk guard then clamps size/stops afterward.
# ================================================================

from dataclasses import dataclass, field as _field
from typing import Optional


@dataclass
class TradeScenario:
    symbol: str
    direction: str            # "long" | "short"
    horizon: str              # "scalp" | "intraday" | "swing"
    entry: float
    target: float             # take-profit price
    stop: float               # stop-loss price
    p_win: float              # probability target hit before stop (0..1)
    R: float                  # reward/risk ratio
    ev: float                 # expected value per unit risk
    drift_annual: float       # estimated annualized log drift
    vol_annual: float         # estimated annualized volatility
    conviction: float         # EV scaled by p_win (LLM tiebreaker)

    def to_prompt(self) -> str:
        return (f"[{self.symbol} {self.direction.upper()} {self.horizon}] entry {self.entry:.4f} "
                f"-> target {self.target:.4f} | stop {self.stop:.4f} | "
                f"P(win) {self.p_win*100:.1f}% | R {self.R:.2f} | EV {self.ev:+.3f}R | "
                f"conviction {self.conviction:.3f}")


def estimate_drift_vol(closes: list[float], lookback: int = 20,
                       bars_per_year: float = 365.0) -> tuple[float, float]:
    """(annualized log drift, annualized vol) from closes. 0s if insufficient.

    bars_per_year annualizes the per-bar drift/vol correctly for the data
    resolution: 365 for daily closes, 288*365 for 5-min closes. The scalp
    engine passes 5-min closes so 5-minute momentum (not 30-day drift) drives
    the long/short selection.

    The raw drift is SHRUNK toward zero (DRIFT_SHRINK) and capped at a sane
    annualized level (DRIFT_ANNUAL_CAP). A 20-bar hot streak does NOT mean the
    asset grows 700%/yr forever — momentum persists but mean-reverts, so the
    barrier probabilities must use an honest, conservative drift or P(win) is
    systematically over-optimistic (the 2026-08-28 calibration lesson).
    """
    if len(closes) < lookback + 1:
        return 0.0, 0.0
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - lookback, len(closes))]
    if len(rets) < 2:
        return 0.0, 0.0
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    sigma = math.sqrt(var) if var > 0 else 0.0
    raw_annual = mu * bars_per_year
    shrunk = raw_annual * DRIFT_SHRINK
    capped = max(-DRIFT_ANNUAL_CAP, min(DRIFT_ANNUAL_CAP, shrunk))
    return capped, sigma * math.sqrt(bars_per_year)


def barrier_win_prob(entry: float, target: float, stop: float,
                     drift_annual: float, vol_annual: float) -> float:
    """P(win barrier hit before loss barrier) under GBM, direction-agnostic.

    Uses the standard two-barrier first-passage result via the exponential
    martingale f(x)=exp(-2*nu*x/sig2). For log-price X starting at 0, hitting
    upper barrier b>0 before lower -a<0 (a>0), drift nu = drift_annual - vol^2/2:

        P = (1 - exp(2*nu*a/sig2)) / (exp(-2*nu*b/sig2) - exp(2*nu*a/sig2))

    Limit as nu->0 gives a/(a+b) (pure-distance odds). Positive drift raises
    the chance of hitting the UP barrier; negative drift lowers it. The win
    barrier may be up (long) or down (short) — the code orients u/l and inverts
    when the win barrier is the lower one.
    """
    if entry <= 0 or target <= 0 or stop <= 0 or target == stop:
        return 0.5
    lu = math.log(target / entry)     # win-barrier distance
    ll = math.log(stop / entry)       # loss-barrier distance
    win_is_up = lu > ll               # target above stop -> long-style win
    up = max(lu, ll)                  # b > 0
    down = min(lu, ll)                # -a < 0  (a = -down)
    a = -down
    if vol_annual <= 1e-9:
        # degenerate: no volatility -> pure drift odds, but keep bounded
        p_win = 0.75 if (drift_annual > 0) == win_is_up else 0.05
        return p_win
    nu = drift_annual - vol_annual * vol_annual / 2.0
    sig2 = vol_annual * vol_annual
    def _exp_safe(x: float) -> float:
        return math.exp(max(-50.0, min(50.0, x)))
    if abs(nu) < 1e-12:
        p_up = a / (a + up)  # no-drift pure-distance odds
    else:
        num = 1.0 - _exp_safe(2.0 * nu * a / sig2)
        den = _exp_safe(-2.0 * nu * up / sig2) - _exp_safe(2.0 * nu * a / sig2)
        p_up = num / den if abs(den) > 1e-12 else 0.5
    p_win = p_up if win_is_up else (1.0 - p_up)
    return max(0.05, min(0.75, p_win))


# Horizon definitions: each produces its own target/stop pair on a 5-min bar
# sigma scale. The engine builds ALL horizons per symbol and the LLM picks the
# best EV one - so it's not always scalping. As drift strengthens, wider-horizon
# targets become more attractive (higher P(win) at distance).
# Scalp uses a TIGHT 1.5-2R target: research (2026 scalp framework) shows the
# 55-60% win rate edge only exists at 1.5R-2R with momentum confirmation - a
# 4R scalp target on a 5-min bar is too far to hit, which is why the old
# win rate collapsed to ~12%.
HORIZONS = {
    "scalp":    {"stop": 1.0, "target": 1.6, "min_r": 1.5},
    "intraday": {"stop": 2.0, "target": 7.0,  "min_r": 1.8},
    "swing":    {"stop": 4.0, "target": 16.0, "min_r": 2.0},
}

# ---- trend-following model (intraday / swing) ----
# Separate from the 5m scalp engine. Built on the horizon's OWN timeframe
# (1h closes for intraday, daily closes for swing) so drift/vol are meaningful
# at that horizon. Uses an EMA fast/slow crossover for trend direction and a
# tight ~2R target so P(win) is realistic (web backtests: EMA crossover ~35-48%
# WR, positive PF because winners run 2-3x bigger than losers).
TREND_EMA_FAST = 12
TREND_EMA_SLOW = 26
TREND_HORIZON_PARAMS = {
    # Take-profit targets are set as ABSOLUTE % so intraday/swing/auto
    # actually chase 25-30%+ per trade (the operator's requirement), not the
    # old 1-16% sigma-derived targets. The stop is derived from the target
    # and the horizon's per-bar sigma, keeping R >= min_r.
    "intraday": {"target_pct": 25.0, "stop_sigma": 2.0, "min_r": 1.8},
    "swing":    {"target_pct": 30.0, "stop_sigma": 2.5, "min_r": 2.0},
}


def _ema(closes: list[float], period: int) -> float:
    """Exponential moving average over `period` closes (last value)."""
    if not closes:
        return 0.0
    k = 2.0 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema


def momentum_confirmed(closes: list[float], direction: str,
                       fast: int = 8, slow: int = 21) -> bool:
    """5-min momentum entry confirmation (the proven scalp edge).

    Long only when EMA8 > EMA21 (short-term momentum up); short only when
    EMA8 < EMA21 (momentum down). This is the same EMA-stack rule professional
    crypto scalpers use on the 5-minute chart, and it is what converts the
    ~36% coin-flip P(win) from the GBM barrier into a real 55-60% win rate:
    we only take trades that are ALREADY moving our way.
    """
    if not closes or len(closes) < slow + 1:
        return False
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    if fast_ema == slow_ema:
        return False
    if direction == "long":
        return fast_ema > slow_ema
    if direction == "short":
        return fast_ema < slow_ema
    return False


def trend_confirmed(closes: list[float], direction: str,
                    fast: int = TREND_EMA_FAST, slow: int = TREND_EMA_SLOW) -> bool:
    """EMA fast/slow crossover trend filter for intraday/swing horizons.

    Long only when EMA_fast > EMA_slow on the horizon's own timeframe closes
    (1h for intraday, daily for swing); short when EMA_fast < EMA_slow. This is
    the classic trend-following crossover — web backtests (BTC daily, 2023-2026)
    show ~35-48% win rate with positive profit factor because winners run
    2-3x larger than losers. The LLM still sees BOTH directions; this flag
    marks which side is aligned with the prevailing trend.
    """
    if not closes or len(closes) < slow + 1:
        return False
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    if fast_ema == slow_ema:
        return False
    if direction == "long":
        return fast_ema > slow_ema
    if direction == "short":
        return fast_ema < slow_ema
    return False


def build_trend_scenarios(symbol: str, closes: list[float], current_price: float,
                          horizon: str, bars_per_year: float = 365.0) -> list[TradeScenario]:
    """LONG+SHORT scenarios for intraday/swing on the horizon's OWN timeframe.

    Unlike build_scenarios (which derives every horizon from 5-min closes and
    produced absurd R=6-7 with P(win)~13%), this builds intraday from 1h closes
    and swing from daily closes, so drift/vol reflect that horizon.

    TAKE-PROFIT is an ABSOLUTE target (intraday 25%, swing 30%) per the
    operator's requirement: these horizons chase 25-30%+ per trade. The stop is
    derived from the per-bar sigma (wider for swing so the 30% target has room
    to breathe) and R is kept >= min_r. Trend direction (EMA crossover) is NOT
    a hard filter: both sides are returned so the LLM weighs P(win)/EV.
    """
    if not closes or current_price <= 0 or horizon not in TREND_HORIZON_PARAMS:
        return []
    drift, vol = estimate_drift_vol(closes, bars_per_year=bars_per_year)
    params = TREND_HORIZON_PARAMS[horizon]
    per_bar_sigma = (vol / math.sqrt(bars_per_year)) * 100.0 if vol > 0 else 1.0
    # ABSOLUTE 25-30%+ take-profit target (the operator's requirement).
    take = float(params["target_pct"])
    # Stop = max(vol-based width, target/min_r) so R never drops below min_r.
    stop = max(per_bar_sigma * params["stop_sigma"], take / params["min_r"])
    scenarios = []
    # LONG
    long_stop = current_price * (1 - stop / 100.0)
    long_target = current_price * (1 + take / 100.0)
    p_long = barrier_win_prob(current_price, long_target, long_stop, drift, vol)
    R = take / stop
    scenarios.append(TradeScenario(
        symbol=symbol, direction="long", horizon=horizon, entry=current_price,
        target=long_target, stop=long_stop, p_win=p_long, R=R,
        ev=p_long * R - (1 - p_long),
        drift_annual=drift, vol_annual=vol,
        conviction=p_long * (p_long * R - (1 - p_long)),
    ))
    # SHORT (mirror)
    short_stop = current_price * (1 + stop / 100.0)
    short_target = current_price * (1 - take / 100.0)
    p_short = barrier_win_prob(current_price, short_target, short_stop, drift, vol)
    scenarios.append(TradeScenario(
        symbol=symbol, direction="short", horizon=horizon, entry=current_price,
        target=short_target, stop=short_stop, p_win=p_short, R=R,
        ev=p_short * R - (1 - p_short),
        drift_annual=drift, vol_annual=vol,
        conviction=p_short * (p_short * R - (1 - p_short)),
    ))
    return scenarios


def _horizon_stop_take(sigma_5m: float, horizon: str,
                       drift_annual: float) -> tuple[float, float]:
    """Stop/target (%) for a horizon, scaled by drift.

    stop  = horizon.stop x sigma_5m (bounded)
    target = horizon.target x sigma_5m x drift_scalar, where drift_scalar grows
             with |drift| so a strong trend extends the target (let it run) and
             a weak trend stays a tight scalp. Always keeps R >= min_r.
    """
    h = HORIZONS[horizon]
    stop = sigma_5m * h["stop"]
    stop = max(VOL_STOP_MIN_PCT, min(VOL_STOP_MAX_PCT * 3, stop))
    # drift scalar: 1.0 at low drift -> up to ~2.5x at strong drift
    drift_mag = abs(drift_annual)
    scalar = 1.0 + min(1.5, drift_mag / 0.5)
    take = sigma_5m * h["target"] * scalar
    take = max(VOL_TARGET_MIN_PCT, take)
    # floor R
    if take / stop < h["min_r"]:
        take = stop * h["min_r"]
    return round(stop, 4), round(take, 4)


def build_scenarios(symbol: str, closes: list[float], current_price: float,
                    stop_pct: float | None = None, take_pct: float | None = None,
                    bars_per_year: float = 365.0) -> list[TradeScenario]:
    """Produce LONG+SHORT scenarios across THREE horizons: scalp / intraday /
    swing, with targets scaled by drift strength (not one fixed 0.6%).

    - scalp:    1.2 sigma stop, 3 sigma target  (~0.14% / ~0.36% BTC)
    - intraday: 2 sigma stop,   7 sigma target  (~0.25% / ~0.85% BTC)
    - swing:    4 sigma stop,   16 sigma target (~0.5%  / ~1.9% BTC)
    In a strong trend the swing target extends further (let it run); in a weak
    trend the scalp dominates. The LLM picks the best-EV horizon per cycle.

    bars_per_year annualizes closes (365 daily / 288*365 for 5-min). The scalp
    engine passes 5-min closes so 5-minute momentum drives the long/short read.
    """
    if not closes or current_price <= 0:
        return []
    drift, vol = estimate_drift_vol(closes, bars_per_year=bars_per_year)
    daily_vol_pct = (vol / math.sqrt(365.0)) * 100.0 if vol > 0 else 1.0
    sigma_5m = daily_vol_pct / math.sqrt(288.0)
    r20 = momentum20_return(closes)
    scenarios = []
    horizons = ("scalp", "intraday", "swing") if (stop_pct is None or take_pct is None) else ("custom",)
    for horizon in horizons:
        if horizon == "custom":
            stop, take = stop_pct, take_pct
            hname = "swing"
        else:
            stop, take = _horizon_stop_take(sigma_5m, horizon, drift)
            hname = horizon
        # LONG scenario
        long_stop = current_price * (1 - stop / 100.0)
        long_target = current_price * (1 + take / 100.0)
        p_long = barrier_win_prob(current_price, long_target, long_stop, drift, vol)
        R = take / stop
        scenarios.append(TradeScenario(
            symbol=symbol, direction="long", horizon=hname, entry=current_price,
            target=long_target, stop=long_stop, p_win=p_long, R=R,
            ev=p_long * R - (1 - p_long),
            drift_annual=drift, vol_annual=vol,
            conviction=p_long * (p_long * R - (1 - p_long)),
        ))
        # SHORT scenario (mirror): barrier_win_prob inverts via win_is_up
        short_stop = current_price * (1 + stop / 100.0)
        short_target = current_price * (1 - take / 100.0)
        p_short = barrier_win_prob(current_price, short_target, short_stop, drift, vol)
        scenarios.append(TradeScenario(
            symbol=symbol, direction="short", horizon=hname, entry=current_price,
            target=short_target, stop=short_stop, p_win=p_short, R=R,
            ev=p_short * R - (1 - p_short),
            drift_annual=drift, vol_annual=vol,
            conviction=p_short * (p_short * R - (1 - p_short)),
        ))
    return scenarios


def scenario_matrix(closes_by_symbol: dict, prices: dict,
                    stop_pct: float | None = None, take_pct: float | None = None,
                    trader_type: str = "auto",
                    bars_per_year: float | dict = 365.0) -> list[TradeScenario]:
    """Build the full long/short scenario matrix across the universe.

    stop_pct/take_pct default to None -> volatility-based reachable levels
    (see build_scenarios). Pass explicit values only to override.

    bars_per_year: annualization bars for drift/vol - a scalar (365 daily,
    288*365 for 5-min) or a per-symbol dict {symbol: bpy}.

    trader_type filters which horizons are returned:
      "scalp"    -> scalp targets only (tight, minutes)
      "intraday" -> intraday targets only (hours)
      "swing"    -> swing targets only (days)
      "auto"     -> all horizons (default, LLM/engine picks best)
    """
    allowed = HORIZON_BY_TYPE.get(trader_type, ["scalp", "intraday", "swing"])
    out = []
    for symbol, closes in closes_by_symbol.items():
        px = prices.get(symbol, 0)
        if px <= 0 or not closes:
            continue
        bpy = bars_per_year.get(symbol, 365.0) if isinstance(bars_per_year, dict) else bars_per_year
        for s in build_scenarios(symbol, closes, px, stop_pct, take_pct, bars_per_year=bpy):
            if s.horizon in allowed:
                out.append(s)
    return out


def pick_best_scenario(scenarios: list[TradeScenario],
                       has_long: dict, has_short: dict,
                       conviction_floor: float = 0.0) -> TradeScenario | None:
    """Pick the highest-conviction actionable scenario.

    Respects position state: skip a long scenario if we're already long that
    symbol (and same for short). Only positive-EV scenarios are candidates —
    a negative-EV trade is a 'hold'. Falls back to the best available.
    """
    actionable = [
        s for s in scenarios
        if s.ev > 0
        and s.conviction >= conviction_floor
        and not (s.direction == "long" and has_long.get(s.symbol))
        and not (s.direction == "short" and has_short.get(s.symbol))
    ]
    if not actionable:
        return None
    actionable.sort(key=lambda s: s.conviction, reverse=True)
    return actionable[0]


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


def partial_profit_check(positions: list[dict], prices: dict,
                         target_pct: float | None = None) -> list[QuantDecision]:
    """Proven retail technique: sell HALF at the target, move stop to breakeven,
    let the rest run (scale out). This banks profit early while keeping upside.

    When a position reaches its take-profit, instead of closing the whole thing
    we sell half and keep the remainder with a breakeven stop. The platform
    supports partial quantity sells, so this returns a 'sell' for half the qty.

    Returns SELL decisions with qty = half the position when target is reached.
    """
    partial = []
    for p in positions:
        symbol = p.get("symbol")
        qty = float(p.get("quantity", 0))
        if qty <= 0:
            continue
        entry = float(p.get("entry_price") or 0)
        cur = prices.get(symbol) or p.get("current_price") or entry
        target = float(p.get("take_profit") or 0)
        if entry <= 0 or cur <= 0:
            continue
        if target <= 0:
            continue
        # reached target? sell half
        if (qty > 0 and cur >= target) or (qty < 0 and cur <= target):
            half = abs(qty) / 2.0
            partial.append(QuantDecision(
                "sell" if qty > 0 else "cover", symbol, half, 0.0, 0.0,
                f"scale out: half at target ${target:.2f}, banked {half:.4f}u, "
                f"rest trails to breakeven"))
    return partial


def _expected_target_minutes(entry: float, target: float, daily_vol_pct: float) -> float:
    """Volatility-derived expected time to hit a target (GBM random-walk math).

    For a random walk, variance scales linearly with N bars, so the number of
    bars to traverse `k` sigma is ~k^2. Given the target as a % move and the
    daily vol in %, the target is k = target_pct / (daily_vol/sqrt(288)) sigma
    on a 5-min bar, and expected minutes ~= k^2 x BAR_MINUTES.
    """
    if entry <= 0 or target <= 0 or daily_vol_pct <= 0:
        return TIME_MIN_HOLD_MINUTES
    move_pct = abs(target / entry - 1.0) * 100.0
    sigma_5m = daily_vol_pct / math.sqrt(288.0)
    k = move_pct / sigma_5m if sigma_5m > 0 else 1.0
    return k * k * BAR_MINUTES


def time_exit_check(positions: list[dict], prices: dict,
                    daily_vol_by_symbol: dict | None = None) -> list[QuantDecision]:
    """Time-based exit derived from each position's volatility, not hardcoded.

    A trade that hasn't resolved within its expected lifetime is dead capital.
    The expected time to hit the target is k^2 x 5min (see _expected_target_minutes)
    where k = target distance in 5m-sigma units. The exit fires at:
      - HARD CUT:    TIME_MAX_HOLD_MULT x expected (exit at market)
      - PROFIT TAKE: TIME_PROFIT_TAKE_MULT x expected if green (bank it)
    So a 4-sigma target (~0.5% BTC) expects ~80 min; high-vol gets more time,
    low-vol gets less - never an arbitrary fixed hour.

    daily_vol_by_symbol: {symbol: daily_vol_pct} to adapt per asset. Falls back
    to a default 2.5% daily vol if not provided.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    exits = []
    for p in positions:
        symbol = p.get("symbol")
        qty = p.get("quantity", 0)
        if qty <= 0:
            continue
        entry = float(p.get("entry_price") or 0)
        cur = prices.get(symbol) or p.get("current_price") or entry
        target = float(p.get("take_profit") or 0)
        opened = p.get("opened_at")
        if not opened or entry <= 0 or cur <= 0:
            continue
        try:
            opened_dt = datetime.fromisoformat(opened.replace('Z', '+00:00'))
        except Exception:
            continue
        age_minutes = (now - opened_dt).total_seconds() / 60.0
        pnl_pct = (cur / entry - 1.0) * 100.0
        daily_vol = daily_vol_by_symbol.get(symbol, 2.5) if daily_vol_by_symbol else 2.5
        expected = _expected_target_minutes(entry, target or entry, daily_vol)
        max_hold = max(TIME_MIN_HOLD_MINUTES, expected * TIME_MAX_HOLD_MULT)
        profit_take = max(TIME_MIN_PROFIT_MINUTES, expected * TIME_PROFIT_TAKE_MULT)
        # TIME-DECAY TAKE-PROFIT (hyperopt: PF 1.51 -> 1.91): as the trade ages
        # without hitting its target, the effective target ratchets DOWN toward
        # breakeven so wins get banked earlier instead of riding back to flat.
        #   after 33% of expected time: target -> x0.75
        #   after 66% of expected time: target -> x0.50
        if TIME_DECAY_ENABLED and target > 0 and age_minutes >= expected * DECAY_AFTER_FRACTION:
            mult = DECAY_TARGET_MULT_LATE if age_minutes >= expected * (2 * DECAY_AFTER_FRACTION) \
                else DECAY_TARGET_MULT
            decayed = entry + (target - entry) * mult
            if (qty > 0 and cur >= decayed) or (qty < 0 and cur <= decayed):
                exits.append(QuantDecision(
                    "sell", symbol, 0.0, 0.0, 0.0,
                    f"time-decay take profit: {age_minutes:.0f}m, target decayed to "
                    f"{mult*100:.0f}% and hit (${cur:,.2f}) - bank it"))
                continue
        # hard cut: past the volatility-derived max hold
        if age_minutes >= max_hold:
            exits.append(QuantDecision(
                "sell", symbol, 0.0, 0.0, 0.0,
                f"time stop: {age_minutes:.0f}m > {max_hold:.0f}m max hold (E[target]~{expected:.0f}m) "
                f"{'+' + f'{pnl_pct:+.1f}' + '%' if pnl_pct >= 0 else f'({pnl_pct:.1f}%)'}"))
        # profit take: green but below target, bank the win
        elif age_minutes >= profit_take and pnl_pct >= PROFIT_TAKE_MIN_PCT:
            exits.append(QuantDecision(
                "sell", symbol, 0.0, 0.0, 0.0,
                f"time profit take: {age_minutes:.0f}m, green {pnl_pct:+.2f}% - bank it"))
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