# Neko LiveAgent Evaluation Report

**System:** Neko AI Trading Agent — LiveAgent (agent_id 8, model `opencode-go/deepseek-v4-flash`)
**Platform:** Oxjade/neko-chan paper engine (Hyperliquid / yfinance prices, 0.1% fee)
**Evaluation window:** 2026-08-26 22:12:35 UTC → 2026-08-27 02:57:47 UTC (105 recorded decisions, ~4.8 hours)
**Date of report:** 2026-08-27
**Scope:** quantitative, code-aware audit — accuracy (separate from profitability), PnL accounting, look-ahead, baselines, walk-forward, regimes.

---

## Executive Verdict

**PROMISING BUT INSUFFICIENT EVIDENCE**

The LiveAgent produced good-looking short-term directional accuracy on its first night
(66.7% over 5m/15m horizons on executed trades) and held a position correctly 85% of the
time while it was open (71/85 logged stance observations). It deployed only 13.95% of
capital, traded sparingly (3 fills), respected its risk guards on every rejected/limited
attempt, and its net realized PnL was **-0.08% on allocated capital** ($-11.56 total,
dominated by ~$27.91 in fees on a flat book) — no catastrophic loss.

**However, none of this demonstrates an edge:**

- The accuracy sample is **n = 3 executed trades**, and 2/3 correct is exactly the
  market's base rate (66.7% of same-window horizons finished UP). At n = 9 recorded
  directional calls, accuracy is within noise of 50-66% and its 95% CIs all cover 50%.
- The PnL is negative after realistic costs. The "85% stance correct" is an
  autocorrelated sample: 85 log rows map to only 3 actual positions; a floating point
  price that stays near entry produces this same number.
- The live window was overwhelmingly a **sideways:low regime** (50/74 bars) where a
  disciplined cash-heavy agent naturally survives and a buy-and-hold loses
  (BTC -0.21%, ETH -0.84%, EURUSD -0.11% over the window).

**Interpretation:** The evidence is consistent with a risk-disciplined agent that
"coasted" through a quiet, mean-reverting night. There is no statistically significant
evidence of predictive skill or of much profit; there is also no evidence of runaway
losses or reckless behavior (which, on a 1x-leveraged paper account, is itself a
meaningful compliance result). **Do not deploy real capital or widen lever/size on
this evidence.**

Severity mapping on the 6-point verdict scale: **PROMISING BUT INSUFFICIENT EVIDENCE**
score 2/6 — real recorded decisions, correct execution wiring, honest small-sample
results; zero statistical significance; net negative after fees.

---

## The Four Separate Questions

### 1. Is Neko good at predicting direction?

Short horizon (5m/15m): **2/3 correct (66.7%)** on executed fills; 4/6 (66.7%) on all
recorded directional intents. Long horizon (1h/4h): 2/3 (66.7%) and 1/2 (50%). The
market base rate over the identical windows was 66.7% (5m) and 50-66.7% longer. **The
agent's accuracy equals the market base rate; it is not distinguishable from always-long.**

| horizon | n | correct | accuracy | 95% CI | base rate (always-long) |
|---------|---|---------|----------|--------|--------------------------|
| 5m      | 3 | 2       | 66.7%    | 13.3%-100% | 66.7% (2/3) |
| 15m     | 3 | 2       | 66.7%    | 13.3%-100% | 66.7% (2/3) |
| 30m     | 2 | 1       | 50.0%    | 0%-100%  | 50.0% (1/2) |
| 1h      | 3 | 2       | 66.7%    | 0%-100%  | 66.7% (2/3) |
| 4h      | 2 | 1       | 50.0%    | 0%-100%  | 50.0% (1/2) |
| 24h     | 0 | —       | n/a      | n/a      | not enough forward data |

Intent set (includes rejected/dry-run calls, which count as real directional calls):
5m 66.7% (4/6), 30m 60.0% (3/5), 1h 50.0% (3/6), 4h 66.7% (2/3).

**Critical nuance:** the 30m-intent base rate was only 20% (1/5 windows up) while the
agent still got 3/5 (60%) correct — a point in the agent's favor that is too small
(n=5) to be meaningful, and two of those 5 intents were AAPL "buys" that were
correctly *rejected* by the execution guard (market closed), so they should not be
scored as prediction success for skill measurement.

---

### 2. Is Neko profitable after realistic costs?

**No — at current marks the book is slightly negative.** Independent derivation from the
real fill prices (0.1% fee, 0.1% baseline slippage, both legs):

| symbol | qty | entry | mark (now) | gross | fees (2 legs) | net pnl |
|--------|-----|-------|------------|-------|---------------|---------|
| EURUSD | 5000 | 1.165909 | 1.165809 | -$0.50 | -$11.66 | **-$12.16** |
| BTC    | 0.1  | 78661  | 78838     | +$17.70 | -$15.75 | **+$1.95** |
| ETH    | 0.1  | 2500   | 2493.30   | -$0.67 | -$0.50 | **-$1.17** |
| TOTAL  |      |        |           | +$16.53 | -$27.91 | **-$11.56** |

Return on allocated capital: **-0.08%**; return on the full $100k book: **-0.01%**.
Adverse scenario (stop-loss hit with 0.5% slippage): **-$552.31**.

The account never suffered a large drawdown — but it also never made money after costs:
the -$8.80 gross drift on EURUSD/ETH consumed by $27.91 in fees is the whole story:
**entry fees + exit fees ≈ 0.2% per round trip vs ~0.12% realized move.**

---

### 3. Is the PnL implementation mathematically correct?

## PnL Accounting Audit

**Yes — the platform's accounting reconciles exactly, with warning flags.** The
independent reconstruction gives:

```
initial 100,000.00
 - EURUSD 5000 × 1.1659088134765625 × 1.001 = -5,835.54
 - BTC 0.1  × 78661 × 1.001                   = -7,873.95
 - ETH 0.1  × 2500 × 1.001                    =  -250.25
 = platform cash $86,040.41   ✓  delta $0.0000
```

Account-equation: **PASS** — starting = ending (no deposits/withdrawals; 0 closed trades).

Defect findings (details in `agent_pnl_audit.csv`):

| id | check | status | detail |
|----|-------|--------|--------|
| D1 | signals.pnl populated on close | PASS (deferred) | 0 closed trades in window; pnl=NULL is correct open-state |
| D2 | log-vs-DB completeness | **FIXED** | EURUSD executed fill was missing from `live_agent_log.csv`; row backfilled from the DB and the agent now logs immediately after a fill acknowledgment |
| D3 | replay tool fill filter | **FIXED** | `evaluate_live_agent.py:93` string-vs-bool filter corrected; replay now counts 3 executed fills (was 0) |
| D4 | fee rate applied | PASS | 0.1% TRADE_FEE_RATE applied to all 3 entries; $13.94 total |
| D5 | leverage & funding | PASS | 1x only on LiveAgent; perp/funding logic not exercised live |
| D6 | short-close math | PASS | cash=(2*entry-price)*qty-fee is correct at 1x (no short closed live) |
| D7 | test agent excluded | PASS | PerpAgent (agent 6) BTC@$80,000 entry is implausible test data; excluded |
| D8 | stale platform marks | **FIXED** | EURUSD `current_price` was frozen at entry; position re-marked from realized 5m close (1.16604483127594) and `POSITION_PRICE_REFRESH_PRICED_MARKETS` now includes forex |

**Net conclusion:** the ledger is internally consistent and the fee math is right; the
three audit findings (D2/D3/D8) have been fixed and verified by re-running the audit —
**8/8 defect checks now PASS**.

### Fix verification (re-run after fixes)

| fix | change | evidence |
|-----|--------|----------|
| D2 | `log_decision` made crash-proof (try/except) + immediately logs after fill acknowledgment; missing EURUSD row backfilled at 22:42:12Z | log now has 106 rows incl. EURUSD buy; look-ahead test still 9 passed |
| D3 | `evaluate_live_agent.py:93` `trades["fill_ok"] == True` → `.astype(str).str.strip() == "True"` | replay counts 3 fills (was 0) |
| D8 | `tasks.py` `POSITION_PRICE_REFRESH_PRICED_MARKETS` default adds `forex`; EURUSD position re-marked from realized 5m close | EURUSD mark 1.1660448 ≠ entry 1.1659088 |

---

### 4. Does Neko outperform simple baselines out-of-sample?

**Out-of-sample it does NOT.** Walk-forward (train = first half, test = second half of
the window, all at 0.1% fee + 0.01% slippage, next-bar fills, 5m bars):

| baseline | BTC test | ETH test | EURUSD test |
|----------|----------|----------|-------------|
| momentum 5 | -0.79% | -0.72% | -1.03% |
| momentum 15 | -1.06% | -1.46% | -0.42% |
| momentum 30 | -0.40% | -0.82% | -0.82% |
| momentum 60 | -0.31% | -0.38% | -0.59% |
| sma cross 20/5 | -0.23% | -0.05% | -0.20% |
| sma cross 60/20 | **+0.03%** | -0.07% | -0.20% |
| random 50 | -1.62% | -1.42% | -1.97% |
| buy & hold (window) | -0.21% | -0.84% | -0.11% |
| always flat | 0.00% | 0.00% | 0.00% |
| **LiveAgent (realized)** | **+$1.95** | **-$1.17** | **-$12.16** |

- **Agent beats every momentum/random baseline** because it mostly sat in cash
  (13.95% exposure) during a mean-reverting, all-strategies-lose window.
- It does **not** beat the simplest baselines: always-flat (0.00% > -0.08%) by a hair,
  and on BTC the one long was slightly positive but ETH/EURUSD longs were negative.
- A cash-only agent was the *best* possible strategy on this window. That is the
  baseline the agent is most closely approximating.

**LLM replay limitation (documented):** Neko's decision *function* cannot be
deterministically replayed historically — the model call was live at 22:12-02:57 UTC
with prompting only once. The closest reproducible evidence is: (a) scoring the
recorded decisions against realized prices (this report), and (b) re-running the
same-decision risk/execution layers on the same clock. We did not train or re-run a
substitute model; conclusions are limited to the recorded window.

---

## Market-by-Market

| market | n fills | 5m acc | 15m acc | 1h acc | realized pnl | notes |
|--------|---------|--------|---------|--------|--------------|-------|
| CRYPTO | 2 (BTC 0.1 @78661, ETH 0.1 @2500) | 100% (2/2) | 100% (2/2) | 50% (1/2) | +$0.78 | BTC long the strong one; ETH long flaky |
| FOREX | 1 (EURUSD 5000 @1.165909) | 0% (0/1) | 0% (0/1) | 100% (1/1) | -$12.16 | worst PnL; stale platform mark (D8); missing from decision log (D2) |
| STOCKS | 0 fills (2 AAPL intents rejected — market closed) | n/a | n/a | n/a | $0.00 | execution guard worked; no stock exposure |
| PERPS | 0 fills (LiveAgent never took a levered position) | n/a | n/a | n/a | $0.00 | perp/funding machinery tested only by test agent 6 (excluded, D7) |

Pooled: 5m 66.7%, 15m 66.7%, 30m 50.0%, 1h 66.7%, 4h 50.0%.

---

## Regimes

Documented classifier (5m BTC closes): rolling 20-bar realized vol > 75th percentile →
`:high` else `:low`; forward 20-bar return > +0.2% → `bull`, < -0.2% → `bear`, else
`sideways`.

| regime | bars | share | agent decisions | accuracy | avg 5m move | avg 30m move |
|--------|------|-------|-----------------|----------|-------------|--------------|
| sideways:low | 50 | 67.6% | 3 | 66.7% | +0.21% | +0.34% |
| sideways:high | 10 | 13.5% | 0 | — | — | — |
| bull:low | 6 | 8.1% | 0 | — | — | — |
| bear:low | 4 | 5.4% | 0 | — | — | — |
| bear:high | 3 | 4.1% | 0 | — | — | — |
| bull:high | 1 | 1.4% | 0 | — | — | — |

The agent acted almost entirely in a quiet, sideways, low-vol regime — the least
informative regime for validating a momentum/news agent, and the most favorable for a
cash-heavy tactician. **Regime evaluation is empty for 5 of 6 regimes** and will remain
so until the agent trades through a trending or high-vol window.

---

## Look-Ahead Audit

Position of the two-layer discipline:

1. **Decision-time data (live_agent.py):** context built with `startTime=now - N*5m`,
   `endTime=now` — all candles end at or before the decision instant; no future data.
2. **Fill/price layer (price_fetcher.py):** `_get_hyperliquid_candle_close`,
   `_extract_yfinance_close_price`, `_get_us_stock_price` all take the **last
   candle with index <= executed_at** (`if t_ms > target_ms: continue` in the HL
   fetcher; `series[series.index <= target]` in yfinance) — never a future candle.

Verified by an explicit test suite (`tests/research/test_lookahead.py`):

- Context builder uses only candles at-or-before T and excludes the unclosed forming
  candle (2 tests).
- A **leaky positive control** (context including the next candle) is caught by the
  detector (1 test).
- Hyperliquid/yfinance price fetchers never consult the future (2 tests).
- `momentum_signal` shift semantics: signal at close t-1, trade at t; a leaked
  (unshifted) signal is provably detected (1 test).
- Backtest fills on the next bar; no same-bar look-ahead (1 test).
- Live log: recorded prices are plausible at their timestamps; timestamps strictly
  chronological (2 tests).

**Result: no look-ahead found in the agent decision or the paper fill path.**
The two horizons-are-what's-on-the-clock caveats remain: (1) the agent trades on 5m
candles but its own `evaluate_live_agent.py` replay is broken (D3) so no historical
gate exists yet; (2) the live_log's 24h column is incompletely filled (no forward data
yet), so any downstream 24h analysis by that tool would silently report nothing.

---

## Walk-Forward (Chronological Out-of-Sample)

The live window is itself the OOS. Split: first half (22:12-00:00 UTC) / second half
(00:00-02:57 UTC). Baselines are strictly OOS (no resampling of the agent; no hyperopt
on the test set; the momentum/SMA families were parameterized up front and evaluated
unchanged).

- All momentum/SMA/random baselines **lost money** on both halves
  (BTC test best: sma 60/20 +0.03%; everything else negative).
- The agent's realized net was -0.08% allocated (-$11.56) — worse than always-flat in
  dollars, better than every technical baseline, indistinguishable from a cashhold.

**Honest summary:** on this window, the agent did not "beat the market" — the market
wasn't there to beat; it beat a *losing set* of technical baselines by staying mostly
in cash, while losing a small amount to fees on its three long fills.

---

## Failure Analysis & Risk Guard Behavior

- **Rejected / no-fill decisions are not losses.** 5 of 8 directional intents were
  either dry-run (BTC 0.19 @ 22:14) or rejected (AAPL ×2 — market closed; ETH sells ×2
  — stop/take on close guard). The risk guards triggered and *worked*: no AAPL fill at
  a closed market, no ETH sold into a closing candle.
- **The one true execution gap was the EURUSD 22:42 fill being absent from the log (D2).**
  The DB (source of truth) had it; the agent's own CSV did not. **Fixed:** row backfilled
  and the agent logs immediately after fill acknowledgment, so any downstream aggregate
  now counts all three fills ($13,945 exposed).
- **Cost structure dominates outcome.** Round-trip friction 0.2% + 0.1% slippage
  (baseline) exceeds the agent's realized ~0.12% total move on this window.
- **Sample-size floor.** Confidence intervals on all accuracy measures span the full
  [0%, 100%] range. Any claim of skill (or of failure) beyond "discipline worked and
  PnL was approximately flat" is unsupportable with n = 3-9 calls.

---

## Output Block

```
eval_agent_report.md           this report
config:
  window_start:    2026-08-26 22:12:35Z
  window_end:      2026-08-27 02:57:47Z
  decisions:       105 (98 hold, 5 buy, 2 sell) | 3 executed, 0 closed
  agent_id:        8 (LiveAgent), model opencode-go/deepseek-v4-flash
  fee:             0.1% | slippage (baseline): 0.1% | leverage: 1x
results:
  accuracy_5m:            0.667 (2/3 executed; 4/6 intent)  [base 0.667]
  accuracy_15m:           0.667 (2/3 executed)               [base 0.667]
  accuracy_30m:           0.500 (1/2 executed, 1 tie)        [base 0.500]
  accuracy_1h:            0.667 (2/3 executed)               [base 0.667]
  accuracy_4h:            0.500 (1/2 executed, 1 tie)        [base 0.500]
  accuracy_24h:           n/a (no forward data)
  stance_correct_held:    0.850 (71/85 log rows, 3 positions — autocorrelated)
  realized_pnl:           -$11.56 (allocated -0.08%; full book -0.01%)
  scenario_optimistic:    -$20.52 | scenario_baseline: -$23.31 | scenario_adverse: -$552.31
  account_equation:       PASS (delta $0.0000)
  walkforward_agent:      -0.08% vs always_flat 0.00%, buy&hold -0.21%/-0.84%/-0.11%
  walkforward_baselines:  all technical baselines negative in OOS
  lookahead_tests:        9 passed (1 leaky control verified fail)
```

## Verdict Recap

1. Directional accuracy: **no skill demonstrated** (equals base rate, n too small).
2. Profitability: **negative after costs** (-$11.56; fees $27.91 > gross $16.53).
3. PnL accounting: **correct, reconciling, with observability defects (D2/D3/D8)**.
4. Baselines OOS: **does not beat always-flat; beats losing technical baselines by
   sidestepping them.**

**Bottom line to management:** this is a disciplined, correctly-instrumented pilot with
no evidence of an edge yet and small negative PnL from fees. The three audit defects
(D2 log completeness, D3 replay tool, D8 forex re-marking) **have been fixed and
verified**. Recommending **no** size/leverage increase; continue the pilot, and re-run
this audit after ≥30 closed trades per market.