"""
Real-market backtest harness for AI-Trader.

Simulates the platform's exact execution model (services.py + routes_signals.py):
  - fills at candle close (matches price_fetcher's candle-close approximation)
  - 0.1% fee on every trade (fees.py TRADE_FEE_RATE)
  - $100,000 starting cash (routes_trading INITIAL_CAPITAL)
  - weighted-average entry on add (long & short), reduction/close otherwise
  - longs = positive qty, shorts = negative qty, PnL mirrors services.py
  - equity = cash + qty*price (long) or cash + |qty|*(2*entry - price) (short)

Strategies benchmarked against buy-and-hold on real data (yfinance).
"""

import argparse
import os
import sys

import pandas as pd
import numpy as np

TRADE_FEE_RATE = 0.001
INITIAL_CAPITAL = 100_000.0


# ---------------------------------------------------------------- data

def load_series(symbol: str, start: str, end: str, interval: str = "1d") -> pd.Series:
    """Real daily OHLCV via yfinance."""
    import yfinance as yf

    df = yf.download(symbol, start=start, end=end, interval=interval, progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        raise RuntimeError(f"No data for {symbol} {start}..{end}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return close.dropna()


# ---------------------------------------------------------------- portfolio

class Portfolio:
    """Mirrors services.py position math: long qty>0, short qty<0."""

    def __init__(self):
        self.cash = INITIAL_CAPITAL
        self.qty = 0.0
        self.entry = 0.0
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.equity_curve: list = []
        self.peak = INITIAL_CAPITAL
        self.max_drawdown = 0.0
        self.trades = 0

    def equity(self, price: float) -> float:
        if self.qty == 0:
            return self.cash
        if self.qty > 0:
            return self.cash + self.qty * price
        return self.cash + abs(self.qty) * (2 * self.entry - price)

    def buy(self, price: float, qty: float) -> bool:
        """buy: increase long (or close short in the platform you'd cover; here used for longs only)."""
        value = price * qty
        fee = value * TRADE_FEE_RATE
        if self.cash < value + fee:
            return False
        self.cash -= value + fee
        self.fees_paid += fee
        total = self.qty * self.entry + qty * price
        self.qty += qty
        self.entry = total / self.qty
        self.trades += 1
        return True

    def sell(self, price: float, qty: float) -> bool:
        """sell: decrease/close long."""
        if self.qty <= 0 or qty > self.qty:
            return False
        value = price * qty
        fee = value * TRADE_FEE_RATE
        self.realized_pnl += (price - self.entry) * qty - fee
        self.cash += value - fee
        self.fees_paid += fee
        self.qty -= qty
        self.trades += 1
        return True

    def short(self, price: float, qty: float) -> bool:
        """short: open/increase short (negative qty)."""
        value = price * qty
        fee = value * TRADE_FEE_RATE
        if self.cash < value + fee:
            return False
        self.cash -= value + fee
        self.fees_paid += fee
        cur_short = abs(self.qty)
        total = cur_short * self.entry + qty * price
        self.qty -= qty
        self.entry = total / abs(self.qty)
        self.trades += 1
        return True

    def cover(self, price: float, qty: float) -> bool:
        """cover: decrease/close short."""
        if self.qty >= 0 or qty > abs(self.qty):
            return False
        value = price * qty
        fee = value * TRADE_FEE_RATE
        self.realized_pnl += (self.entry - price) * qty - fee
        self.cash += value - fee
        self.fees_paid += fee
        self.qty += qty
        self.trades += 1
        return True

    def close_all(self, price: float):
        if self.qty > 0:
            self.sell(price, self.qty)
        elif self.qty < 0:
            self.cover(price, abs(self.qty))

    def mark(self, price: float):
        eq = self.equity(price)
        self.equity_curve.append(eq)
        self.peak = max(self.peak, eq)
        if self.peak > 0:
            self.max_drawdown = max(self.max_drawdown, (self.peak - eq) / self.peak)

    def position_qty(self, price: float) -> float:
        """Size the position so the trade value is ~all available cash (1x notional)."""
        if self.cash <= 0 or price <= 0:
            return 0.0
        return (self.cash * 0.99) / (price * (1 + TRADE_FEE_RATE))

    def target_state(self, desired: float, price: float):
        """desired: +1 long, 0 flat, -1 short. One rebalance per bar, market-on-close."""
        if desired == 1 and self.qty < 0:
            self.close_all(price)
            self.buy(price, self.position_qty(price))
        elif desired == 1 and self.qty == 0:
            self.buy(price, self.position_qty(price))
        elif desired == 0:
            self.close_all(price)
        elif desired == -1 and self.qty > 0:
            self.close_all(price)
            self.short(price, self.position_qty(price))
        elif desired == -1 and self.qty == 0:
            self.short(price, self.position_qty(price))




# ---------------------------------------------------------------- strategies

def strategy_buy_and_hold(closes: pd.Series) -> pd.Series:
    return pd.Series(1.0, index=closes.index)


def strategy_sma_crossover(closes: pd.Series, fast: int = 20, slow: int = 50) -> pd.Series:
    fast_ma = closes.rolling(fast).mean()
    slow_ma = closes.rolling(slow).mean()
    sig = pd.Series(np.where(fast_ma > slow_ma, 1.0, 0.0), index=closes.index)
    return sig.fillna(0.0)


def strategy_momentum(closes: pd.Series, lookback: int = 20) -> pd.Series:
    mom = closes.pct_change(lookback)
    return pd.Series(np.where(mom > 0, 1.0, 0.0), index=closes.index).fillna(0.0)


def strategy_rsi_reversion(closes: pd.Series, period: int = 14, buy: float = 30, sell: float = 70) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = (100 - 100 / (1 + rs)).fillna(50.0)
    sig = pd.Series(np.where(rsi < buy, 1.0, np.where(rsi > sell, 0.0, np.nan)), index=closes.index)
    return sig.ffill().fillna(0.0)


def strategy_trend_band(closes: pd.Series, fast: int = 10, slow: int = 30) -> pd.Series:
    fast_ma = closes.rolling(fast).mean()
    slow_ma = closes.rolling(slow).mean()
    band = fast_ma - slow_ma
    return pd.Series(np.where(band > 0, 1.0, np.where(band < 0, -1.0, 0.0)), index=closes.index).fillna(0.0)


def strategy_sma_crossover_short(closes: pd.Series, fast: int = 20, slow: int = 50) -> pd.Series:
    fast_ma = closes.rolling(fast).mean()
    slow_ma = closes.rolling(slow).mean()
    return pd.Series(np.where(fast_ma > slow_ma, 1.0, np.where(fast_ma < slow_ma, -1.0, 0.0)), index=closes.index).fillna(0.0)


STRATEGIES = {
    "buyhold": strategy_buy_and_hold,
    "sma20_50": lambda c: strategy_sma_crossover(c, 20, 50),
    "sma50_200": lambda c: strategy_sma_crossover(c, 50, 200),
    "sma20_50_longshort": strategy_sma_crossover_short,
    "momentum20": lambda c: strategy_momentum(c, 20),
    "rsi_reversion": strategy_rsi_reversion,
    "trend_band_10_30": strategy_trend_band,
}


# ---------------------------------------------------------------- engine

def run_backtest(closes: pd.Series, strategy_fn, symbol: str, strategy_name: str) -> dict:
    pf = Portfolio()
    target = strategy_fn(closes)

    for i, (ts, close) in enumerate(closes.items()):
        price = float(close)
        desired = float(target.iloc[i])
        if desired != (1.0 if pf.qty > 0 else (-1.0 if pf.qty < 0 else 0.0)):
            pf.target_state(desired, price)
        pf.mark(price)

    pf.close_all(float(closes.iloc[-1]))

    eq = pd.Series(pf.equity_curve)
    ret = eq.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * np.sqrt(252)) if len(ret) > 1 and ret.std() > 0 else 0.0
    total_return = (pf.equity_curve[-1] / INITIAL_CAPITAL - 1) * 100
    bh = (float(closes.iloc[-1]) / float(closes.iloc[0]) - 1) * 100

    return {
        "symbol": symbol,
        "strategy": strategy_name,
        "return_pct": round(total_return, 2),
        "buyhold_pct": round(bh, 2),
        "excess_pct": round(total_return - bh, 2),
        "max_dd_pct": round(pf.max_drawdown * 100, 2),
        "sharpe": round(sharpe, 2),
        "fees_usd": round(pf.fees_paid, 2),
        "trades": pf.trades,
        "final_cash": round(pf.cash, 2),
        "n_bars": len(closes),
    }


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="AI-Trader real-market backtest")
    parser.add_argument("--symbols", default="BTC-USD,ETH-USD,SPY,AAPL,QQQ",
                        help="comma-separated yfinance symbols")
    parser.add_argument("--start", default="2024-01-01", help="start date")
    parser.add_argument("--end", default="2026-08-26", help="end date")
    parser.add_argument("--interval", default="1d", help="candle interval")
    parser.add_argument("--strategies", default=None,
                        help="comma-separated strategy names; default: all")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    strat_names = [s.strip() for s in args.strategies.split(",")] if args.strategies else list(STRATEGIES)

    results = []
    for symbol in symbols:
        try:
            closes = load_series(symbol, args.start, args.end, args.interval)
        except Exception as e:
            print(f"[skip] {symbol}: {e}", file=sys.stderr)
            continue
        for name in strat_names:
            fn = STRATEGIES.get(name)
            if fn is None:
                print(f"[skip] unknown strategy {name}", file=sys.stderr)
                continue
            results.append(run_backtest(closes, fn, symbol, name))

    print(f"\n{'Symbol':<10}{'Strategy':<20}{'Return%':>9}{'BuyHold%':>10}{'Excess%':>9}"
          f"{'MaxDD%':>8}{'Sharpe':>8}{'Fees$':>9}{'Trades':>7}")
    print("-" * 94)
    for r in sorted(results, key=lambda x: x["excess_pct"], reverse=True):
        print(f"{r['symbol']:<10}{r['strategy']:<20}{r['return_pct']:>9.2f}{r['buyhold_pct']:>10.2f}"
              f"{r['excess_pct']:>9.2f}{r['max_dd_pct']:>8.2f}{r['sharpe']:>8.2f}{r['fees_usd']:>9.2f}{r['trades']:>7}")

    print("\nStrategy aggregate (mean excess return over buy-and-hold, all symbols):")
    agg = {}
    for r in results:
        agg.setdefault(r["strategy"], []).append(r["excess_pct"])
    for name, vals in sorted(agg.items(), key=lambda kv: np.mean(kv[1]), reverse=True):
        beats = sum(1 for v in vals if v > 0)
        print(f"  {name:<20} mean excess: {np.mean(vals):>+7.2f}%   "
              f"beat buy&hold on {beats}/{len(vals)} symbols")

    csv_path = os.path.join(os.path.dirname(__file__), "..", "exports", "tables", "backtest_results.csv")
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    pd.DataFrame(results).to_csv(csv_path, index=False)
    print(f"\nSaved {os.path.abspath(csv_path)}")


if __name__ == "__main__":
    main()