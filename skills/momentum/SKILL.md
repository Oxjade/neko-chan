# Risk-Managed Crypto Momentum (Reference Strategy)

A reference strategy for AI-Trader agents, validated on real market data with the
platform's exact execution model (0.1% fee, market-on-close fills).

## Evidence (real yfinance data, platform-faithful backtest)

`research/scripts/backtest_risk_controlled.py`

| Asset | Period | Momentum20 return | Buy & hold |
|---|---|---|---|
| BTC | 2021–2023 (incl. bear) | **+374%** | +44% |
| ETH | 2021–2023 | **+687%** | +214% |
| BTC | 2024–2026 | **+203%** | +78% |
| ETH | 2024–2026 | **+248%** | +4% |

20-day time-series momentum (long when the 20-day return > 2%, otherwise cash)
beat buy-and-hold in BOTH periods on BOTH crypto assets. The same strategy does
**not** beat buy-and-hold on US equities (SPY/AAPL/QQQ) — apply to crypto only.

This matches the AI-Trader paper (arXiv:2512.10971): excess returns materialize
more readily in highly liquid, technically-driven markets (crypto), and risk
control — position sizing, staying in cash, cutting losses — is what separates
robust agents from lucky ones.

## Strategy

1. **Signal (crypto only, e.g. BTC/ETH):**
   - Compute the 20-day return: `r = price_now / price_20d_ago - 1`
   - `r > 0.02` → **long** (buy)
   - `r <= 0.02` → **flat** (sell everything, stay in cash)
2. **Risk management (mandatory):**
   - Stop-loss: `stop_loss_pct=8` on every entry
   - Take-profit: `take_profit_pct=24`
   - Never exceed ~30% of the portfolio in a single position (fractional sizing)
3. **Discipline:**
   - No more than one trade decision per day (over-trading reduces returns more
     than any signal improves them; the best benchmark agents trade least)
   - Never average down; never trade against the signal

## API usage

```bash
# Long entry
curl -X POST http://localhost:8000/api/signals/realtime \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"market":"crypto","symbol":"BTC","action":"buy","quantity":0.05,
       "price":0,"executed_at":"now","stop_loss_pct":8,"take_profit_pct":24}'

# Flat (sell all)
curl -X POST http://localhost:8000/api/signals/realtime \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"market":"crypto","symbol":"BTC","action":"sell","quantity":0.05,
       "price":0,"executed_at":"now"}'
```

`stop_loss_pct` and `take_profit_pct` are enforced automatically by the platform
worker (`position_risk_management` task): positions are closed at the level the
moment they are breached, fees included.

## Caveats

- Historical results are not a guarantee of future returns. Momentum works in
  crypto because trends persist and retail flow is momentum-chasing; regimes can
  change.
- Fees and slippage are already modeled in the evidence above.
- If the 20-day return has been >2% for many days in a row, the position is
  already open — do not re-enter; hold until the signal flips.