"""
Risk-controlled backtest for AI-Trader.

Tests the core finding of the AI-Trader paper (arXiv:2512.10971) and HKU
Agentic Trader experiments: risk control capability (position sizing, stop
losses, staying in cash) determines cross-market robustness more than signal
accuracy.

Execution model (more realistic than backtest_real_market.py):
  - signals computed on bar close, filled at NEXT bar open (no look-ahead)
  - 0.1% platform fee + 1bp slippage per fill
  - optional stop-loss / take-profit checked against intra-bar extremes
  - optional volatility-targeted position sizing (ATR-based)
"""

import argparse
import os
import sys

import pandas as pd
import numpy as np

TRADE_FEE_RATE = 0.001
SLIPPAGE_BPS = 0.0001
INITIAL_CAPITAL = 100_000.0
ATR_PERIOD = 14


# ---------------------------------------------------------------- data

def load_ohlc(symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
    import yfinance as yf

    df = yf.download(symbol, start=start, end=end, interval=interval, progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        raise RuntimeError(f"No data for {symbol} {start}..{end}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    return df


# ---------------------------------------------------------------- risk engine

class RiskPortfolio:
    def __init__(self, stop_pct: float = 0.0, take_pct: float = 0.0, vol_target: float = 0.0):
        self.cash = INITIAL_CAPITAL
        self.qty = 0.0
        self.entry = 0.0
        self.fees_paid = 0.0
        self.slippage_paid = 0.0
        self.realized_pnl = 0.0
        self.equity_curve: list = []
        self.peak = INITIAL_CAPITAL
        self.max_drawdown = 0.0
        self.trades = 0
        self.stop_pct = stop_pct
        self.take_pct = take_pct
        self.vol_target = vol_target
        self.atr: float | None = None

    def equity(self, price: float) -> float:
        if self.qty == 0:
            return self.cash
        if self.qty > 0:
            return self.cash + self.qty * price
        return self.cash + abs(self.qty) * (2 * self.entry - price)

    def _fill_cost(self, value: float) -> float:
        return value * (TRADE_FEE_RATE + SLIPPAGE_BPS)

    def size(self, price: float) -> float:
        if self.vol_target > 0 and self.atr and self.atr > 0:
            risk_per_trade = self.cash * self.vol_target
            return max(risk_per_trade / (3 * self.atr), 0.0)
        return (self.cash * 0.99) / (price * (1 + TRADE_FEE_RATE + SLIPPAGE_BPS))

    def enter_long(self, price: float):
        qty = self.size(price)
        cost = self._fill_cost(price * qty)
        if self.cash < price * qty + cost:
            return
        self.cash -= price * qty + cost
        self.fees_paid += price * qty * TRADE_FEE_RATE
        self.slippage_paid += price * qty * SLIPPAGE_BPS
        self.qty = qty
        self.entry = price
        self.trades += 1

    def exit_long(self, price: float):
        value = price * self.qty
        cost = self._fill_cost(value)
        self.realized_pnl += (price - self.entry) * self.qty - cost
        self.cash += value - cost
        self.fees_paid += value * TRADE_FEE_RATE
        self.slippage_paid += value * SLIPPAGE_BPS
        self.qty = 0.0
        self.entry = 0.0
        self.trades += 1

    def update_atr(self, ohlc: pd.DataFrame, i: int):
        if i < ATR_PERIOD + 1:
            return
        tr = ohlc.iloc[i - ATR_PERIOD : i]
        ranges = np.maximum(tr["High"] - tr["Low"], np.maximum(
            (tr["High"] - tr["Close"].shift(1)).abs(),
            (tr["Low"] - tr["Close"].shift(1)).abs(),
        ))
        self.atr = float(ranges.mean())

    def check_stops(self, bar_high: float, bar_low: float) -> str | None:
        """Returns exit price if a stop/target was hit during the bar."""
        if self.qty == 0:
            return None
        if self.qty > 0:
            if self.stop_pct > 0 and bar_low <= self.entry * (1 - self.stop_pct):
                return self.entry * (1 - self.stop_pct)
            if self.take_pct > 0 and bar_high >= self.entry * (1 + self.take_pct):
                return self.entry * (1 + self.take_pct)
        return None

    def mark(self, price: float):
        eq = self.equity(price)
        self.equity_curve.append(eq)
        self.peak = max(self.peak, eq)
        if self.peak > 0:
            self.max_drawdown = max(self.max_drawdown, (self.peak - eq) / self.peak)


def run_risk_backtest(ohlc: pd.DataFrame, strategy_fn, symbol: str, strategy_name: str,
                      stop_pct: float = 0.0, take_pct: float = 0.0, vol_target: float = 0.0) -> dict:
    pf = RiskPortfolio(stop_pct, take_pct, vol_target)
    target = strategy_fn(ohlc)
    closes = ohlc["Close"]
    opens = ohlc["Open"]
    n = len(ohlc)
    pending: float | None = None  # signal computed on close i-1, filled at open i

    for i in range(n):
        o, h, l, c = float(opens.iloc[i]), float(ohlc["High"].iloc[i]), float(ohlc["Low"].iloc[i]), float(closes.iloc[i])
        pf.update_atr(ohlc, i)

        if pf.qty > 0:
            stop_hit = pf.check_stops(h, l)
            if stop_hit is not None:
                pf.exit_long(stop_hit)
                pending = 0.0  # risk-off after a stop-out
            else:
                desired = float(target.iloc[i])
                if desired <= 0:
                    pf.exit_long(o)
                pending = None
        else:
            desired = float(target.iloc[i])
            if desired > 0:
                if pending is None:
                    pending = desired
                else:
                    pf.enter_long(o)
                    pending = None
            elif pending is not None:
                pending = None

        pf.mark(c)

    if pf.qty > 0:
        pf.exit_long(float(closes.iloc[-1]))
    elif pending is not None:
        pass

    eq = pd.Series(pf.equity_curve)
    ret = eq.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * np.sqrt(252)) if len(ret) > 1 and ret.std() > 0 else 0.0
    downside = ret[ret < 0]
    sortino = float(ret.mean() / downside.std() * np.sqrt(252)) if len(downside) > 1 and downside.std() > 0 else 0.0
    total_return = (pf.equity_curve[-1] / INITIAL_CAPITAL - 1) * 100
    bh = (float(closes.iloc[-1]) / float(closes.iloc[0]) - 1) * 100
    calmar = total_return / (pf.max_drawdown * 100) if pf.max_drawdown > 0 else 0.0

    return {
        "symbol": symbol,
        "strategy": strategy_name,
        "return_pct": round(total_return, 2),
        "buyhold_pct": round(bh, 2),
        "excess_pct": round(total_return - bh, 2),
        "max_dd_pct": round(pf.max_drawdown * 100, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "fees_usd": round(pf.fees_paid, 2),
        "slippage_usd": round(pf.slippage_paid, 2),
        "trades": pf.trades,
        "n_bars": n,
    }


# ---------------------------------------------------------------- strategies

def sig_trend_sma(ohlc: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    close = ohlc["Close"]
    f = close.rolling(fast).mean()
    s = close.rolling(slow).mean()
    return pd.Series(np.where(f > s, 1.0, 0.0), index=ohlc.index).fillna(0.0)


def sig_momentum(ohlc: pd.DataFrame, lookback: int = 20) -> pd.Series:
    mom = ohlc["Close"].pct_change(lookback)
    return pd.Series(np.where(mom > 0.02, 1.0, 0.0), index=ohlc.index).fillna(0.0)


def sig_buyhold(ohlc: pd.DataFrame) -> pd.Series:
    return pd.Series(1.0, index=ohlc.index)


STRATEGIES = {
    "buyhold": sig_buyhold,
    "trend20_50": lambda o: sig_trend_sma(o, 20, 50),
    "trend50_200": lambda o: sig_trend_sma(o, 50, 200),
    "momentum20": lambda o: sig_momentum(o, 20),
}

# (name, stop_pct, take_pct, vol_target)
RISK_PRESETS = {
    "no_stop": (0.0, 0.0, 0.0),
    "stop8": (0.08, 0.0, 0.0),
    "stop8_take24": (0.08, 0.24, 0.0),
    "stop5_take15": (0.05, 0.15, 0.0),
    "vol_target_2": (0.08, 0.0, 0.02),
}


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="AI-Trader risk-controlled backtest")
    parser.add_argument("--symbols", default="BTC-USD,ETH-USD,SPY,AAPL,QQQ",
                        help="comma-separated yfinance symbols")
    parser.add_argument("--start", default="2021-01-01", help="start date")
    parser.add_argument("--end", default="2026-08-26", help="end date")
    parser.add_argument("--interval", default="1d", help="candle interval")
    parser.add_argument("--strategies", default=None, help="comma-separated; default all")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    strat_names = [s.strip() for s in args.strategies.split(",")] if args.strategies else list(STRATEGIES)

    results = []
    for symbol in symbols:
        try:
            ohlc = load_ohlc(symbol, args.start, args.end, args.interval)
        except Exception as e:
            print(f"[skip] {symbol}: {e}", file=sys.stderr)
            continue
        for sname in strat_names:
            fn = STRATEGIES.get(sname)
            if fn is None:
                continue
            for rname, (stop, take, vol) in RISK_PRESETS.items():
                results.append(run_risk_backtest(ohlc, fn, symbol, f"{sname}+{rname}", stop, take, vol))

    hdr = (f"{'Symbol':<10}{'Strategy':<24}{'Return%':>9}{'BuyHold%':>10}{'Excess%':>9}"
           f"{'MaxDD%':>8}{'Sharpe':>8}{'Sortino':>8}{'Calmar':>8}{'Trades':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(results, key=lambda x: x["excess_pct"], reverse=True):
        print(f"{r['symbol']:<10}{r['strategy']:<24}{r['return_pct']:>9.2f}{r['buyhold_pct']:>10.2f}"
              f"{r['excess_pct']:>9.2f}{r['max_dd_pct']:>8.2f}{r['sharpe']:>8.2f}{r['sortino']:>8.2f}"
              f"{r['calmar']:>8.2f}{r['trades']:>7}")

    print("\nRisk preset aggregate (mean excess over buy-and-hold, all strategies/symbols):")
    agg = {}
    for r in results:
        preset = r["strategy"].split("+", 1)[-1]
        agg.setdefault(preset, []).append(r["excess_pct"])
    for name, vals in sorted(agg.items(), key=lambda kv: np.mean(kv[1]), reverse=True):
        beats = sum(1 for v in vals if v > 0)
        print(f"  {name:<18} mean excess: {np.mean(vals):>+7.2f}%  beat B&H: {beats}/{len(vals)}  "
              f"mean MaxDD: {np.mean([r['max_dd_pct'] for r in results if r['strategy'].endswith(name)]):.1f}%")

    out = os.path.join(os.path.dirname(__file__), "..", "exports", "tables", "backtest_risk_controlled.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    pd.DataFrame(results).to_csv(out, index=False)
    print(f"\nSaved {os.path.abspath(out)}")


if __name__ == "__main__":
    main()