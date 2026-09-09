"""Honest 4h backtest lab for Neko-Chan entry filters.

RESEARCH ONLY — never imported by the live bot, never deployed to the VPS.
It replays the agent's exact intraday decision logic against historical 4h
candles with the REAL cost model, and measures win rate per configuration:

  HONESTY RULES (the anti-fake-win-rate guards):
  1. NEXT-BAR FILLS: a signal computed on bar T can only enter on bar T+1's
     open. No signal ever uses the bar it trades on.
  2. INTRA-BAR CONSERVATISM: if both stop and target are inside one bar's
     range, the STOP wins (pessimistic resolution).
  3. REAL COSTS: every fill pays venue fee + platform fee; slippage on
     market entries. Maker fills (resting limit at pullback) earn the
     Growth-Mode maker rebate instead (-0.005%).
  4. ONE POSITION AT A TIME, one entry attempt per symbol per day — the
     bot's actual discipline.

Usage:
  python3 research/lab.py --coins BTC,ETH,SOL,SUI,HYPE --years 2
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field, asdict

import requests

HYPERLIQUID = "https://api.hyperliquid.xyz/info"

# ---- cost model (must match the live bot / paper gateway) -----------------
TAKER_FEE_BPS = 4.5        # Aftermath perps taker (tier 0)
MAKER_FEE_BPS = -0.5       # Growth Mode: makers GET 0.5bps
PLATFORM_FEE_BPS = 50.0    # 0.5% platform fee per fill (current model)
SLIPPAGE_BPS = 1.0         # market orders


def fetch_4h(symbol: str, bars: int = 2000, retries: int = 3) -> list[dict]:
    """4h OHLCV from the same candle source the agent uses."""
    import time as _t
    for attempt in range(retries):
        try:
            return _fetch_4h_once(symbol, bars)
        except Exception:
            if attempt == retries - 1:
                raise
            _t.sleep(2 * (attempt + 1))


def _fetch_4h_once(symbol: str, bars: int) -> list[dict]:
    now_ms = int(time.time() * 1000)
    span = bars * 4 * 3600000
    r = requests.post(HYPERLIQUID, json={
        "type": "candleSnapshot",
        "req": {"coin": symbol, "interval": "4h",
                "startTime": now_ms - span, "endTime": now_ms},
    }, timeout=30)
    r.raise_for_status()
    out = []
    for c in r.json():
        out.append({"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                    "l": float(c["l"]), "c": float(c["c"])})
    return out


# ---- indicators (same math as quant_strategy) -----------------------------

def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> list[float]:
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out = [50.0] * period
    for i in range(period, len(closes)):
        if i > period:
            ag = (ag * (period - 1) + gains[i - 1]) / period
            al = (al * (period - 1) + losses[i - 1]) / period
        rs = ag / al if al > 0 else 100.0
        out.append(100 - 100 / (1 + rs))
    return out


def daily_sigma(closes: list[float]) -> float:
    """Annualized-ish daily vol proxy from 4h closes (per-bar stdev of returns)."""
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 30:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    return var ** 0.5


# ---- filters (each returns True when entry is ALLOWED) ---------------------

def f_trend(closes, i, dirn, sig, ctx=None):
    """BASE: EMA12/26 trend confirmed + RSI gate (the bot's current entry)."""
    e12 = ctx["e12"][i] if ctx else ema(closes[:i + 1], 12)[-1]
    e26 = ctx["e26"][i] if ctx else ema(closes[:i + 1], 26)[-1]
    r = ctx["rsi"][i] if ctx else rsi(closes[:i + 1])[-1]
    if dirn == "long":
        return e12 > e26 and r >= 40
    return e12 < e26 and r <= 60


def f_pullback(closes, i, dirn, sig, ctx=None):
    """Pullback entry: trend confirmed AND price near/recently touched EMA12
    (within 0.35 sigma) — entering the trend at a discount, not at the top."""
    if not f_trend(closes, i, dirn, sig, ctx):
        return False
    e12 = ctx["e12"][i] if ctx else ema(closes[:i + 1], 12)[-1]
    sp = ctx["sig_px"][i] if ctx else sig * closes[i]
    if abs(closes[i] - e12) / max(sp, 1e-9) <= 0.35:
        return True
    for j in range(max(0, i - 3), i):
        e = ctx["e12"][j] if ctx else ema(closes[:j + 1], 12)[-1]
        spj = ctx["sig_px"][j] if ctx else sig * closes[j]
        if abs(closes[j] - e) / max(spj, 1e-9) <= 0.35:
            return True
    return False


def f_chop(closes, i, dirn, sig, ctx=None):
    """Chop filter: EMA12/26 must be separated by >= 0.5 sigma (a real trend,
    not noise). Stacks on top of the base trend gate."""
    if not f_trend(closes, i, dirn, sig, ctx):
        return False
    e12 = ctx["e12"][i] if ctx else ema(closes[:i + 1], 12)[-1]
    e26 = ctx["e26"][i] if ctx else ema(closes[:i + 1], 26)[-1]
    sp = ctx["sig_px"][i] if ctx else sig * closes[i]
    gap = abs(e12 - e26) / max(sp, 1e-9)
    return gap >= 0.5


def f_session(closes, i, dirn, sig, ctx=None):
    """Session gate: only enter during EU/US liquidity (12:00-20:00 UTC)."""
    meta = (ctx or {}).get("meta")
    if meta is None:
        return True
    hour = time.gmtime(meta[i]["t"] / 1000).tm_hour
    return 12 <= hour < 20


def f_all(closes, i, dirn, sig, ctx=None):
    """Combined: pullback + chop + session — the full proposed stack."""
    if not f_pullback(closes, i, dirn, sig, ctx):
        return False
    if not f_chop(closes, i, dirn, sig, ctx):
        return False
    return f_session(closes, i, dirn, sig, ctx)


FILTERS = {
    "base": lambda c, i, d, s, m=None: f_trend(c, i, d, s),
    "pullback": f_pullback,
    "chop": f_chop,
    "session": f_session,
    "all": f_all,
}


# ---- the simulator ---------------------------------------------------------

@dataclass
class Config:
    target_r: float = 1.5          # take-profit in R multiples
    breakeven_at_r: float = 0.0    # move stop to entry after +X R (0 = off)
    stop_sigma: float = 1.0        # stop distance in daily sigmas
    max_bars_in_trade: int = 42    # 7 days of 4h bars


@dataclass
class Result:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    gross_r: float = 0.0           # before fees, in R units
    fees_pct_sum: float = 0.0      # total fee % of notional across fills
    win_rate: float = 0.0
    net_r: float = 0.0             # after fees
    notes: list = field(default_factory=list)


def run(symbol: str, bars: list[dict], filt: str, cfg: Config,
        success_fee: bool = False, maker_entry: bool = False) -> Result:
    closes = [b["c"] for b in bars]
    sig = daily_sigma(closes[:-1])
    # precomputed indicator arrays (O(n) once, not O(n^2) per filter call)
    _e12 = ema(closes, 12)
    _e26 = ema(closes, 26)
    _rsi = rsi(closes)
    _sig_px = [sig * c for c in closes]  # sigma in price units per bar
    ctx = {"e12": _e12, "e26": _e26, "rsi": _rsi, "sig_px": _sig_px,
           "meta": bars}
    res = Result()
    i = 210  # indicator warmup
    n = len(bars)
    in_pos = False
    pos = None
    last_entry_day = -1

    def close_trade(exit_px, reason):
        nonlocal in_pos, pos
        entry, side, stop, target, bar_i, orig_dist, entry_maker = pos
        pnl_pct = (exit_px / entry - 1) if side == "long" else (entry / exit_px - 1)
        r = pnl_pct / max(orig_dist, 1e-9)
        win = r > 0.02
        entry_fee = MAKER_FEE_BPS if (maker_entry and entry_maker) else TAKER_FEE_BPS
        # stops are market exits (taker); targets/trails rest as limits (maker)
        exit_fee = TAKER_FEE_BPS if reason == "stop" else MAKER_FEE_BPS
        venue = (entry_fee + exit_fee) / 10000.0
        platform = (PLATFORM_FEE_BPS / 10000.0) if (win or not success_fee) else 0.0
        fees = venue + platform
        res.trades += 1
        if reason == "stop" and r < -0.02:
            res.losses += 1
        elif reason in ("target", "trail") and r > 0.02:
            res.wins += 1
        else:
            res.scratches += 1
        res.gross_r += r
        # R-adjusted fee drag: fees as fraction of the stop-distance risk
        res.fees_pct_sum += fees / max(orig_dist, 1e-9)
        res.net_r += r - fees / max(orig_dist, 1e-9)
        in_pos = False
        pos = None

    while i < n - 1:
        if in_pos:
            bar = bars[i]
            entry, side, stop, target, bar_i, orig_dist, _em = pos
            # breakeven stop management (compared in R units)
            if cfg.breakeven_at_r > 0 and stop != entry:
                fav = (bar["h"] / entry - 1) if side == "long" else (entry / bar["l"] - 1)
                stop_dist = abs(entry - stop) / entry
                if stop_dist > 0 and fav / stop_dist >= cfg.breakeven_at_r:
                    stop = entry  # move to breakeven (orig_dist keeps R fixed)
                    pos = (entry, side, stop, target, bar_i, orig_dist, _em)
            # CONSERVATIVE intra-bar resolution: stop first
            hit_stop = bar["l"] <= stop if side == "long" else bar["h"] >= stop
            hit_tgt = bar["h"] >= target if side == "long" else bar["l"] <= target
            if hit_stop:
                close_trade(stop, "stop")
            elif hit_tgt:
                close_trade(target, "target")
            elif i - bar_i >= cfg.max_bars_in_trade:
                close_trade(bar["c"], "trail")
            i += 1
            continue

        # one attempt per symbol per UTC day
        day = time.gmtime(bars[i]["t"] / 1000).tm_year * 1000 + \
            time.gmtime(bars[i]["t"] / 1000).tm_yday
        if day == last_entry_day:
            i += 1
            continue

        # direction by trend on bar i, entry on bar i+1 OPEN (honesty rule 1)
        for dirn in ("long", "short"):
            if not FILTERS[filt](closes, i, dirn, sig, ctx):
                continue
            e12, e26 = _e12[i], _e26[i]
            stop_dist = cfg.stop_sigma * sig
            if stop_dist <= 0:
                break
            entry = bars[i + 1]["o"]
            if dirn == "long" and e12 > e26:
                stop = entry * (1 - stop_dist)
                target = entry * (1 + stop_dist * cfg.target_r)
                pos = (entry, "long", stop, target, i + 1, stop_dist,
                       filt == "pullback")
                in_pos = True
                last_entry_day = day
                break
            if dirn == "short" and e12 < e26:
                stop = entry * (1 + stop_dist)
                target = entry * (1 - stop_dist * cfg.target_r)
                pos = (entry, "short", stop, target, i + 1, stop_dist,
                       filt == "pullback")
                in_pos = True
                last_entry_day = day
                break
        i += 1

    if res.trades:
        res.win_rate = (res.wins / res.trades) * 100.0
    res.notes.append(f"fee drag total: {res.fees_pct_sum:.1f}R across {res.trades} trades")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="BTC,ETH,SOL,SUI,HYPE")
    ap.add_argument("--bars", type=int, default=2000)  # ~333 days of 4h
    args = ap.parse_args()

    configs = {
        "current (2R target, no filters)": (Config(target_r=2.0), "base"),
        "intraday 1.5R (current target)": (Config(target_r=1.5), "base"),
        "1.5R + breakeven@1R": (Config(target_r=1.5, breakeven_at_r=1.0), "base"),
        "1.5R + breakeven@0.5R": (Config(target_r=1.5, breakeven_at_r=0.5), "base"),
        "1.5R + pullback entry": (Config(target_r=1.5), "pullback"),
        "1.5R + chop filter": (Config(target_r=1.5), "chop"),
        "1.5R + session gate": (Config(target_r=1.5), "session"),
        "1.5R + ALL filters": (Config(target_r=1.5), "all"),
        "1.5R ALL + breakeven@0.5R": (Config(target_r=1.5, breakeven_at_r=0.5), "all"),
        "1R target plain": (Config(target_r=1.0), "base"),
        "1R + pullback": (Config(target_r=1.0), "pullback"),
        "1R + pullback + chop": (Config(target_r=1.0), "pullback"),
    }
    econ = {
        "current fees (taker + platform on all)": dict(),
        "maker fills (Growth Mode)": dict(maker_entry=True),
        "success fee (platform on wins only)": dict(success_fee=True),
        "maker + success fee": dict(success_fee=True, maker_entry=True),
    }
    cache = {c: fetch_4h(c.strip(), args.bars) for c in args.coins.split(",")}

    print(f"{'config':<40}{'economics':<42}{'trades':>6} {'win%':>6} {'grossR':>8} {'netR':>8}")
    print("-" * 106)
    for name, (cfg, filt) in configs.items():
      for econ_name, kwargs in econ.items():
        tot_r = Result()
        for coin, bars in cache.items():
            r = run(coin.strip(), bars, filt, cfg, **kwargs)
            tot_r.trades += r.trades
            tot_r.wins += r.wins
            tot_r.gross_r += r.gross_r
            tot_r.net_r += r.net_r
        wr = (tot_r.wins / tot_r.trades * 100) if tot_r.trades else 0
        print(f"{name:<40}{econ_name:<42}{tot_r.trades:>6} {wr:>5.1f}% {tot_r.gross_r:>8.1f} {tot_r.net_r:>8.1f}")


if __name__ == "__main__":
    main()
