# PLAN — Neko agent quantitative audit

Scope: code-aware quantitative audit of Neko's AI trading agent. Evaluation
only. No production trading, wallet, signing, or risk-guard changes. Changes
are isolated to research/, tests/, and evaluation tooling.

## Contract inventory (from the task request)

| id | Outcome (independently omittable) | Evidence |
|----|----------------------------------|----------|
| C1 | Trace the real decision path (data -> context -> trend+sentiment -> AI decision -> intent -> risk -> execution -> position -> exit -> PnL -> export) with exact files/functions | report section 2 |
| C2 | Identify exactly what the agent predicts (action vs directional prediction) and extract all decision fields | evaluate_agent_accuracy output |
| C3 | Directional accuracy separated from profitability, over 5m/15m/30m/1h/4h/24h where data permits, binary metrics, confidence=unavailable (no score emitted) | tables + report |
| C4 | Look-ahead audit with an explicit failing test (future data must not influence a decision) | tests/research/test_lookahead.py + report |
| C5 | Independent audit of the existing PnL implementation vs derived math, account equation, long/short/lev/partial/funding/fees; flag every discrepancy | audit_pnl output + report |
| C6 | Realistic execution scenarios (optimistic/baseline/adverse) using the actual Neko paper engine, no fabricated numbers | audit_pnl scenarios + report |
| C7 | Market-by-market results (crypto/forex/stocks/perps) without mixing | tables + report |
| C8 | Market-regime evaluation (bull/bear/sideways/high-vol/low-vol) with a documented classifier | tables + report |
| C9 | Baselines (buy&hold, always-long, always-short, random, momentum) on the same period/costs | walk-forward output + report |
| C10 | Beta vs skill separation (agent vs benchmark vs excess; directional vs risk-adjusted vs absolute) | report |
| C11 | Statistical tests: bootstrap CIs, block bootstrap, multiple-testing, inconclusive states made explicit | report |
| C12 | Chronological walk-forward (train/dev -> validation -> test -> next window), holdout untouched | evaluate_walk_forward output |
| C13 | Live-agent data validation (log vs DB vs market), separate LIVE/OBSERVATIONAL section, not mixed with backtests | report |
| C14 | Accuracy report (overall + per-market + per-timeframe) | tables + report |
| C15 | Profitability report (capital, gross/net, fees, funding, slippage, return, win rate, PF, expectancy, MaxDD, Sharpe, Sortino, counts) | tables + report |
| C16 | Failure analysis with classified loss categories | tables + report |
| C17 | Final report with executive verdict + four separate critical questions + output block | research/agent_evaluation_report.md |

## Key environment facts (verified before work)

- Live agent: service/agent/live_agent.py, model LIVE_AGENT_MODEL default
  opencode-go/deepseek-v4-flash, ACTIVE_MODE=1 scalper prompt. Decisions logged
  to research/exports/live_agent_log.csv (105 rows, 2026-08-26T22:12 ->
  2026-08-27T02:57 UTC). No confidence field is emitted by the agent.
- Authoritative platform DB: service/server/data/clawtrader.db (SQLite in this
  environment; DATABASE_URL empty). agents=46, signals=18 (2 funding rows),
  positions=10. LiveAgent=agent 8.
- Paper engine: routes_signals.py (fill+cash), services.py _update_position_from_signal
  (positions), tasks.py position_risk_management_loop + perp_funding_loop +
  _record_profit_history_once (exits/funding/PnL snapshots). Fee=0.1%
  (fees.py). Short math: cover cash = (2*entry - price)*qty - fee.
- Research pipeline: research/scripts/{export_research_dataset,build_agent_features,
  compute_metrics,analyze_experiments,generate_figures,evaluate_momentum_model,
  backtest_real_market,backtest_risk_controlled,evaluate_live_agent}.py +
  research_common.py (bootstrap_ci, BH correction). block_bootstrap_ci lives in
  evaluate_momentum_model.py.
- Realized-price data: Hyperliquid candleSnapshot API (BTC/ETH 5m, working),
  yfinance (EURUSD=X 5m working for the window, AAPL 5m).
- Known defects from code read (to verify programmatically):
  - signals.pnl / exit_price never populated -> trades.csv pnl = NULL.
  - evaluate_live_agent.py compares fill_ok string to bool True -> replays no fills.
  - Live log is missing the EURUSD long opened 2026-08-26T22:42:12 (DB has it).
  - PerpAgent (test agent 6) signals carry implausible fills (e.g. sell @ 99000).

## Decisions / limitations recorded

- LLM decision step cannot be deterministically replayed historically. The
  closest reproducible evaluation = score the actually recorded decisions
  against realized prices; reconstruct the deterministic context+risk+execution
  layers; record model/config. No substitute model is used.
- Confidence = unavailable for every decision (schema has no confidence field;
  live JSON has none).
- 24h horizon: no future data exists yet for the window; 4h only for early
  decisions. Reported as NA where data does not permit.