# Risk-Managed Crypto Momentum (Reference Strategy — Quant Spec)

A **complete quantitative specification** of the platform's reference momentum strategy:
validated evidence, absolute sizing math, risk-only size caps, overfitting-proof
validation, regime gating, cost accounting, and live-monitoring guards.

> This skill is the **authoritative strategy spec** for anything calling itself a
> "momentum" agent on this platform. If you implement a strategy that deviates from
> this spec (different lookback, different threshold, different fill convention),
> you must re-run the validation ladder below and record the deviation — otherwise
> the strategy is under-documented and its results are not comparable.

> **PRODUCTION REALITY (2026-09-01):** the live Neko agent
> (`service/agent/live_agent.py` + `quant_strategy.py`) implements the scalp /
> intraday / swing horizon engine (EMA8>21 momentum confirmation + RSI gate as
> hard entry filters). HORIZONS were tuned on live Hyperliquid 5m/1h data
> (`research/scripts/test_winrate.py`): scalp stop=1.5σ/target=0.6σ
> (~45% WR), intraday stop=0.8σ/target=0.6σ (49.9% WR, +0.44 EV/trade).
> Orders execute on **Aftermath Perps** (mainnet/testnet) through the execution
> gateway — paper mode was removed. Wins are small-and-frequent; a 2%+ price
> target per trade is only reachable on intraday/swing horizons at lower win
> rates (the barrier math: WR ≈ 1/(1+R)).

---

## 1. Evidence base (what we actually know)

`research/scripts/backtest_risk_controlled.py` — platform-faithful (0.1% fee, 1bp
slippage, market-on-close fills, next-bar fills, no look-ahead):

| Asset | Period | Momentum20 return | Buy & hold |
|---|---|---|---|
| BTC | 2021–2023 (incl. bear) | **+374%** | +44% |
| ETH | 2021–2023 | **+687%** | +214% |
| BTC | 2024–2026 | **+203%** | +78% |
| ETH | 2024–2026 | **+248%** | +4% |

What this does **and does not** say:
- 20-day time-series momentum (long when 20d return > 2%, else cash) beat B&H on
  BTC and ETH across two distinct 3-year blocks, including a full bear.
- It does **not** work on US equities (SPY/AAPL/QQQ) — crypto only.
- It is NOT evidence for any 5m/15m intraday scalper variant. The live-agent audit
  (`research/agent_evaluation_report.md`, 2026-08-27) tested a 5m ACTIVE scalper on
  this philosophy: **66.7% accuracy = exact base rate, WFE against every technical
  baseline, -$11.56 net vs -$27.91 fees**. Intraday versions require their own
  validation ladder; they do not inherit the 20d evidence.

**Reference paper context:** the strategy family matches the AI-Trader paper
(arXiv:2512.10971): excess returns materialize readily in highly liquid,
technically-driven markets (crypto); risk control — sizing, cash, stops — is what
separates robust agents from lucky ones. Momentum is the single largest documented
cross-sectional factor (Jegadeesh-Titman 1993) and, on crypto, time-series variants
persist because trends persist and retail flow is momentum-chasing.

---

## 2. The spec (fixed parameters — these are frozen, not tunable)

| Parameter | Value | Binding rule |
|---|---|---|
| Lookback | 20 days | `r = close / close[-20d] - 1` |
| Threshold | r > 2% → LONG, else FLAT | also **exit** when r ≤ 2% (no trailing) |
| Fill convention | signal at close t-1, fill at t | `shift(1)`, no same-bar fills |
| Fee | 0.1%/leg | `fees.py` `TRADE_FEE_RATE` |
| Slippage | 1bp/leg baseline; 5bp adverse | scenario engine |
| Stop-loss | 8% from entry | platform worker enforces |
| Take-profit | 24% from entry | platform worker enforces |
| Max position | 30% of equity | `MAX_POSITION_PCT` |
| Trade frequency | ≤ 1 decision/day | `MAX_DAILY_TRADES` |

**Why each parameter is what it is (do not "explore" without rerunning the ladder):**
- 20d/2%: cross-validated as the best simple grid cell in `ic_scan` (lookback ×
  threshold grid, Bonferroni-corrected, in `evaluate_momentum_model.py`).
- 8%/24% (1:3): matches the strategy's natural 20d moves and the platform's
  risk-worker defaults; 1:3 rewards give a breakeven win rate of 25% — the
  strategy's realized hit rate must stay above that with margin.
- 30% cap + 0.1% fee: at 30% position and 0.1% leg, a full round-trip loses
  **0.06% of equity per trade** to fees alone; the strategy must earn ≥ 10× that
  per round trip (the 624% blocks did, the 5m scalper did not).

---

## 3. Position sizing (absolute math — Kelly as ceiling, not target)

Sizing is a *risk* decision, not a conviction decision. Never size a position
larger than BOTH of these allow.

### 3.1 Kelly ceiling

For a binary outcome (win prob p, R = avg win / avg loss):

```
f* = p - (1-p)/R          # discrete Kelly, fraction of equity to RISK
f*_cont ≈ μ / σ²          # continuous approximation (μ = mean ret, σ² = var)
```

Rules:
- **Use 1/4–1/2 Kelly in production.** Full Kelly is a ceiling, not a target.
  With noisy estimates, over-Kelly is the fastest account killer (gain asymmetry:
  multiplying by 1.5 overbets, losing is worse than winning slowly).
- If your sample is small (n < 100 trades), **shrink toward zero**: `f_used =
  f* × min(1, n/100) × fraction` — i.e., half-Kelly × 0.5 with 25 trades.
- **Kelly ≤ 0 → do not trade this strategy at all** (no measurable edge).
- Use **net** μ (after costs): μ_net = μ − costs. Costs must be netted *before*
  the Kelly formula, otherwise high-turnover edges are over-sized.
- Kelly for negatively skewed (short-vol / momentum-crash) strategies should be
  halved again from the normal-formula value: the Gaussian approximation
  systematically over-sizes for fat tails.

### 3.2 Volatility targeting (modern standard — overrides Kelly when they disagree)

```
size = base_size × (target_vol / realized_vol)
```

- Realized vol = rolling std of 5d/20d returns, annualized.
- When realized vol doubles → halve position. This is the *primary* adaptive cap:
  it holds risk steady through regimes without any market-opinion input.
- `target_vol = 20% / √(positions)` or a fixed portfolio-level vol budget.

### 3.3 Risk-per-trade rule (the operational cap)

```
size_units = (equity × risk_pct) / (entry × stop_pct)
```

- `risk_pct = 1–2%` of equity per trade (never 3%+ without a Monte Carlo).
- `stop_pct = 8%` → risk 1% → notional = equity × 0.01 / 0.08 = 12.5% of equity —
  conveniently ≤ the 30% cap. If stop were 3% (scalper), then 1% risk = 33%
  notional → violates the cap → the cap (30%) must be the binding constraint.

### 3.4 Portfolio-level (momentum books are one trade)

- All crypto momentum positions are **highly correlated** (BTC/ETH ≈ 0.8+).
  Sizing them as independent bets is a real error: 2 identical-signal positions
  at 30% each = 60% effective on one bet, not 2 diversified bets.
- Treat BTC/ETH average: if both are LONG at the same time, the *position-level*
  exposure is the SUM; cap per-book at 30% of equity.

## 3.5 Risk of ruin (must be < 1% before you trust a backtest to tell you to size up)

- Analytic streak check: `n = ⌈log(T) / log(1 - r)⌉` consecutive losses to breach
  threshold T with per-trade risk r. Example: r=2%, T=20% → ⌈log(.2)/log(.98)⌉ = 80 streak.
- **Monte Carlo it.** Bootstrap your actual trade PnL distribution (respecting
  autocorrelation — use block bootstrap) 10,000 paths with your sizing rule and
  measure: P(equity < 20% of start), expected max drawdown, expected time to hit.
- Reality check: with p=0.35, R=1.5 (planned 8/24 stop behavior), full Kelly would
  be f* = 0.35 − 0.65/1.5 = −0.083 → *below zero*. In other words, the strategy's
  realized R must actually be ~2–3+ at p≈0.35 for Kelly to be positive. That
  means: **stop 8%, take 24% is not a Kelly-positive payoff at 35% win rate** —
  the 2021-2026 blocks are gross-of-this-math because they over-deliver; if the
  hit rate drops below the margin line, size must shrink, not grow.

---

## 4. Validation ladder (mandatory gate — every claim goes through this)

**Threshold interpretation: PASS requires the row's Acceptable column; any row
without a passing value = "no evidence", not "loses".** (Recreatable in
`research/scripts/evaluate_momentum_model.py`.)

| # | Test | What it rejects | Threshold |
|---|---|---|---|
| 1 | **IS / OOS split** | data mining your holdout | 70/30 split, rules frozen BEFORE OOS |
| 2 | **Walk-forward (WFE)** | parameter instability | WFE > 0.5 across windows (12m fit → 3m test); < 0.3 → overfit |
| 3 | **PBO (CSCV)** | selection bias | < 10–20% low risk; > 50% → 90% chance of overfit if strategy picked from a grid |
| 4 | **Deflated Sharpe (DSR)** | winner's curse under trials | raw Sharpe > 0 with DSR p < 0.05; record n_trials (~K from grid search) |
| 5 | **Multiple testing** | cherry-picked grid cell | Bonferroni / Benjamini-Hochberg on IC p-values (`ic_scan` already does this) |
| 6 | **Corrected IC / IR** | tracking-error illusion | rank IC (Spearman) per period, mean IC > 0.03–0.05 with stable std; Grinold: IR ≈ IC·√N |
| 7 | **HAC-adjusted errors** | autocorrelation false confidence | Newey-West / block-bootstrap on periodic returns (our `bootstrap_ci`) |
| 8 | **Test breadth** | overfitting to one config | ≥ 3 horizons and ≥ 50 trades in OOS |
| 9 | **Correlation with null** | no magic | random-profile / permutation test: shuffled signals or randomized entry timing must NOT achieve the same IC |
| 10 | **Costs modeled exactly** | gross-return claims | 0.1% fee both legs + slippage + funding (if leveraged) |

Repo implementation:
- `research/scripts/evaluate_momentum_model.py` — IC scan + Bonferroni,
  profitability, walk-forward, fee sensitivity, block bootstrap 95%CI, t-stat of
  mean daily excess vs B&H.
- `tests/research/test_lookahead.py` — point-in-time guarantee for every new rule
  (9 tests; leaky positive control must fail).
- `research/scripts/audit_pnl.py` — cost-scenario fidelity.
- Add the PBO/DSR tool as `research/scripts/ic_scan_pbo.py` when grid > 10 configs.

---

## 5. Regime gating (momentum's achilles heel)

Momentum loses in **mean-reverting / ranging** regimes and crashes at
**trend-to-range transitions** (the classic "momentum crash" — 2018, 2021 H2).

### 5.1 Documented classifier (use now — in `evaluate_walk_forward.py`)

- Rolling 20-bar realized vol vs its 75th percentile → high/low.
- Forward 20-bar return > +0.2% → bull; < -0.2% → bear; else sideways.
- **Gating matrix** (from the live audit: the 5m agent acted 67.6% of the time in
  `sideways:low` — where 100% of mechanical baselines LOST):

| Regime | Position size | Stops | Note |
|---|---|---|---|
| `bull:low` | full (30%) | 8% | momentum's home ground |
| `bull:high` | half (15%) | 12% (wider) | trend + vol: gaps hurt |
| `sideways:low` | 25% or cash | 5% | mean reversion steals from trend |
| `sideways:high` | cash preferred | — | fees + whipsaw |
| `bear:*` | cash preferred | — | momentum crashes here (2018, 2021 H2) |

### 5.2 HMM / unsupervised (optional, with caveats)

HMM (Gaussian, 2-3 states, fit on returns+vol) is the gold-standard detector; but
academic evidence (e.g. "A closer look at regime-switching evidence of bull and
bear markets", 2023) says **in-sample regime-conditional expected returns do NOT
predict out-of-sample conditional means** because the bull/bear spread is mostly
skewness. Practical takeaways:
- Regimes are real for *volatility* (vol clusters; HMM vol states are reliable).
- Regimes for *directional drift* are much weaker — never let an HMM "bull" tag
  justify 2× sizing.
- Require ✓: walk-forward fit (no lookahead), state durations 3-6 weeks (too fast
  = overfit), and BIC/AIC for state count (5+ states = overfit).

---

## 6. Execution & cost accounting (exact numbers, never assume)

| Item | Value | Source |
|---|---|---|
| Platform fee | 0.1% per leg | `fees.py` `TRADE_FEE_RATE` |
| Slippage baseline | 1bp/leg (0.01%) | `backtest_risk_controlled.py` |
| Slippage adverse | 5bp/leg | scenario engine |
| Round-trip baseline | 0.22% | fee 0.2% + slip 0.02% |
| Funding (leveraged only) | variable perp funding; model it | `perp_funding_loop`, funding 0.05-0.1%/8h in trending markets |
| Break-even move | round-trip×1.5 | any expected move < ~0.33% is a fee donut |

Rules:
- Net costs BEFORE Kelly and before claiming "profitable". (The audit's #1 lesson:
  gross +$16.53, net -$11.56 — 27.9% of gross consumed by fees.)
- Model funding at hold-time; a 3-day leveraged 8h-funding position at 0.08%/8h
  ≈ 0.72% hold cost — larger than the fee sometimes.
- Slippage is worse on thin/volatile timestamps: shrink size in high-vol session
  windows OR accept wider stops. Never assume you filled at mid.

---

## 7. Signal & alpha-quality metrics (report ALL of them, not just accuracy)

| Metric | Definition | Good |
|---|---|---|
| Accuracy | correct predictions / all | any; must be reported with n |
| Base rate | up-frequency in same window | the ONLY fair comparison — accuracy ≈ base rate ⇒ no skill |
| IC (Spearman rank) | corr(rank(signal), rank(fwd return)) per period | mean > 0.03–0.05 stable, not one-off 0.30 |
| IC std / t-stat | IC stability | high t = consistent |
| Precision / recall / F1 | long-class binary metrics | report with class imbalance |
| Info ratio | excess μ / tracking error | Grinold: IR ≈ IC·√N |
| Turnover | Σ |change in positions| / |portfolio| per period | keep low; costs scale linearly |
| Payoff ratio (R) | avg win / avg loss | > 2 for low win-rate |
| Expectancy | p·(avg win) − (1−p)·(avg loss) | $ positive per trade, after costs |
| Profit factor | gross profit / gross loss | > 1.5 target |
| Max drawdown | peak-to-trough | must be tolerable; correlates with Kelly |
| Calmar | annualized ret / maxDD | > 1 |

Key discipline: **win rate means nothing without payoff**. A 35% win rate with
R=2.5 has positive expectancy; a 70% win-rate scalper with R=0.4 loses after costs
(exactly the live-agent case: 66.7% accuracy + R≈0.3 = negative expectancy).

---

## 8. Live monitoring & degradation detection

You are not "quant" until you monitor the *live* signal against the backtested one.

Daily artifacts:
1. **Realized IC tracking** — recompute IC on live decisions each period ("IC live"
   vs "IC backtest"). Rule: if live IC falls below 0.5× backtest IC for > 20
   consecutive periods → investigate regime/crowding (see §5).
2. **Alpha-erosion check** — reestimate the decay curve (IC by holding period from
   `holding-period returns matrix`). If the half-life shortened vs backtest →
   crowding/execution degradation.
3. **Live A/B** — record each raw signal and the decision; compute
   "signal fires & would have won" vs "agent traded it & won". If the two
   diverge, the agent is the gap (fees, delays, guard trips — look at
   `evaluate_live_agent.py` `fill_ok`/`error` columns).
4. **Drawdown telemetry** — equity peak-to-trough in $ and %; if MaxDD(z) exceeds
   the backtest's 95th percentile → de-risk (halve size) and re-run §5 regime check.
5. **Trial lens** — every time you "try" a grid cell, add +1 to n_trials; DSR must
   be recomputed. The audit's honest framing: **n=3 executions is not "good", is
   not "bad" — it is "no evidence".**

Re-run the validation ladder at least once per month AND after every regime change.

---

## 9. API usage (this platform — local base)

**Base URL:** `$AI_TRADER_URL` (env var, default `http://127.0.0.1:8000`).

```bash
# Long entry (20d return > 2%, stop 8%, take 24%, ≤30% equity)
curl -X POST $AI_TRADER_URL/api/signals/realtime \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"market":"crypto","symbol":"BTC","action":"buy","quantity":0.05,
       "price":0,"executed_at":"now","stop_loss_pct":8,"take_profit_pct":24}'

# Flat (sell all — 20d return ≤ 2%)
curl -X POST $AI_TRADER_URL/api/signals/realtime \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"market":"crypto","symbol":"BTC","action":"sell","quantity":0.05,
       "price":0,"executed_at":"now"}'
```

The stop-loss / take-profit are enforced automatically by the platform worker
(`position_risk_management` task) — positions are closed the moment the level is
breached, fees included.

---

## 10. HARD RULES (non-negotiable)

1. **Never claim "profitable" with < 30 trades or IS-only or gross-only evidence.**
2. **Never run a same-bar fill or compute a signal with close[t] in the context of
   decision[t] — the look-ahead suite must stay green.**
3. **Never copy the 20d evidence to a 5m variant** — different horizon, different
   cost structure, different validation.
4. **Never average down; never trade against the signal; never exit the free
   market structure to chase a 5m dip.**
5. **Size with risk, not conviction** — Kelly is a ceiling, vol-targeting is the
   primary cap, 1–2% per-trade risk is the floor-to-cap.
6. **Report regime alongside performance** — pooled-only numbers hide
   "made it all in one regime" (the audit's biggest lesson).
7. **Correlated positions are one bet** — BTC+ETH LONG = one exposure, sum, cap it.

---

## 11. Caveats (read these before risking anything)

- Historical results do not guarantee future returns; momentum works because
  crypto trends persist and retail chases; regimes can change silently.
- Fees and slippage are already modeled in the §1 evidence, but funding (if you
  lever this) is NOT — model it before summing net α.
- If the 20d return has been >2% for many days, position is already open — do NOT
  re-enter on re-trigger; hold until flip.
- **Live audit floor (2026-08-27):** the spec's own platform showed a 5m scalper
  variant doing exactly base-rate with negative fees. The spec is sound; the
  *variant* was not validated. Re-run the ladder if you change anything.
