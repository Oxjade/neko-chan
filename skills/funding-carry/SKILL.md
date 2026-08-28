# Funding-Rate Carry (Cash-and-Carry / Basis Trade) — Reference Strategy Spec

A **complete quantitative specification** of a delta-neutral perp-funding carry
strategy implemented live in `service/agent/funding_carry.py`. It collects the
structural funding payment on Hyperliquid perpetuals **without predicting price
direction**, and is the strategy that makes the **short leg profitable in any
regime** — the opposite of the directional-momentum short, which the live test
(`.unlazy/live-short-test/`) showed loses in an up-drift market.

> This skill is the **authoritative strategy spec** for anything calling itself a
> "funding", "basis", "cash-and-carry", or "carry" strategy on this platform (and
> for the paper/live plumbing in `service/agent/funding_carry.py`). Deviation from
> any frozen parameter below requires re-running the validation ladder in §5.

---

## 1. What the strategy is (and why it's not a momentum bet)

A perpetual has no expiry, so funding keeps the perp price tethered to spot by
transferring a periodic payment between longs and shorts. **When funding is
positive, longs pay shorts; when negative, shorts pay longs.**

The carry trade exploits this structural mechanic, not price direction:

```
funding > 0  ->  SHORT the perp  +  LONG equal-notional spot  ->  collect funding
funding < 0  ->  LONG  the perp  +  SHORT equal-notional spot  ->  collect |funding|
```

The spot and perp legs cancel directional (delta) exposure, so **price moves do
not matter**; profit = funding received − fees − slippage. This is the same
mechanism run at scale by Ethena USDe (~$3.9B) and Falcon Finance (~$1.6B), and
is the most durable crypto edge on Hyperliquid because positive funding persists
while leverage demand is structurally asymmetric (retail over-longs).

### Why this fixes the short-leg problem

The live momentum short test (2026-08-27) measured a SOL short losing −$0.15 in
an up-drift market: **directional shorts only profit in bears.** Funding carry
inverts the logic — you short the *perp* precisely to *receive* funding while
hedging the price risk with a spot long. The short side is now a carry collector,
profitable regardless of trend direction.

---

## 2. Live snapshot (measured 2026-08-27 via `funding_carry.py`)

```
BTC  +10.9% APY  (SHORT perp carry, net ~+7.3% after 30d costs)
ETH  + 9.8% APY  (SHORT perp carry, net ~+6.1%)
SOL  +10.9% APY  (SHORT perp carry, net ~+7.3%)
SUI  +10.9% APY  (SHORT perp carry, net ~+7.3%)
HYPE +10.9% APY  (SHORT perp carry, net ~+7.3%)
SEI  +10.9% APY  (SHORT perp carry, net ~+7.3%)
NEAR +29.8% APY  (SHORT perp carry, net ~+26.2%)  <- standout
ATOM - 8.2% APY  (LONG perp carry,  net ~+4.5%)
```

Run it yourself any time:

```bash
.venv/bin/python service/agent/funding_carry.py 100000
# or programmatic:
python -c "
from service.agent.funding_carry import scan_carry
for d in scan_carry(100000.0): print(d['symbol'], f\"{d['net_annualized_apy']*100:+.1f}%\", d['collect_side'])
"
```

The net APY already subtracts a 0.30% round trip (0.025% taker + 0.05% slippage
per leg × 4 legs) amortized over a 30-day hold.

---

## 3. The spec (frozen parameters — do not tune casually)

| Parameter | Value | Binding rule |
|---|---|---|
| Data source | Hyperliquid `metaAndAssetCtxs` (`funding`, `markPx`, `premium`) | one bulk call, no per-symbol polling |
| Collect side | funding>0 → **short_perp+long_spot**; funding<0 → **long_perp+short_spot** | sign of funding decides the side |
| Funding floor | &#124;annualized funding&#124; ≥ 4% APY | below this fees dominate (`FUNDING_APY_FLOOR`) |
| Min net carry | net APY ≥ 5% (`MIN_ANNUAL_CARRY`) | below this, not worth the legs |
| Taker fee | 2.5 bps / leg | Hyperliquid taker |
| Slippage | 5 bps / leg | conservative baseline (1 leg + taker) |
| Legs per cycle | 4 (2 perp + 2 spot, one round trip) | open+close each side |
| Holding period | 30 days (`HOLD_DAYS`) | amortizes round-trip cost; funding accrues daily |
| Max carry notional | 10% of equity / symbol (`MAX_SYMBOL_CARRY_PCT`) | keeps capital working, caps basis-blowout risk |
| Rebalance | hourly funding resettle (`REBALANCE_HOURS=1`) | re-evaluate; do not churn legs |

**Cost model (one-way leg = taker + slippage = 7.5 bps):**

```
total_roundtrip_cost = 4 × (2.5 + 5) bps = 0.30% of notional
annualized_roundtrip_drag = 0.30% × (365 / 30) = 3.65% APY
net_apy = |funding_apy| − 3.65%            (per hold cycle)
cycle_net = |funding_apy| × (30/365) − 0.30%   (one 30-day cycle, % of notional)
```

Why 30 days: a 10.9% carry nets ~7.3% APY (0.60% per cycle). A 4% carry would
net only ~0.35% — hence the 5% net floor. A 30%+ carry (NEAR) nets ~26% APY
(2.15% per cycle).

---

## 4. Position sizing & risk

Sizing follows the momentum skill's absolute-math discipline (§3), with
carry-specific twists.

### 4.1 Kelly / vol-targeting are near-irrelevant here — the edge is the carry

- Carry is low-variance and nearly directionless; the traditional Kelly on
  directional win/loss doesn't apply. The binding constraint is **capital
  efficiency**: each carry locks capital on BOTH legs (spot + perp margin).
- Size to the **notional cap** (10% of equity per symbol) and spread across
  multiple carry symbols so no single basis-blowout concentrates risk.

### 4.2 The real risks (and how the spec handles them)

| Risk | Mechanism | Mitigation in spec |
|---|---|---|
| Funding reversal | positive→negative flips your payer | floor gate + re-evaluate hourly; exit if net < 0 |
| Basis blowout | perp deviates 5-15% from spot in stress | **1× leverage on the short-perp leg** (never lever the perp side) |
| Leg/slippage risk | spot & perp legs not filled at same price | use 5bps slippage, act in low-vol, limit orders |
| Deleveraging (ADL) | extreme vol forces profitable positions | small per-symbol notional (10%), diversify |
| Opportunity cost | capital locked in modest-yield carry | only carry symbols with net APY ≥ 5% |

### 4.3 Risk of ruin

Carry books have far lower ruin odds than directional books (near-market-neutral),
but a **funding sign reversal** is the tail: if you hold long-perp carry and
funding inverts, you start paying. Guard: exit any symbol whose net APY crosses
below the floor — never hold a position that has become a payer.

---

## 5. Validation ladder (mandatory gate)

Carry is a *structural* claim, so validate the mechanism, not a backtested curve:

| # | Test | What it rejects | Pass condition |
|---|---|---|---|
| 1 | **Live funding fetch** | stale/fabricated carry | `funding_carry.py` reads `metaAndAssetCtxs`; funding APY in a sane range (±100%) |
| 2 | **Cost floor** | fee-heavy illusion | net APY ≥ 5% only after the 4-leg cost model (§3) — positive gross but negative net is not tradeable |
| 3 | **Payer check** | holding a position that flipped | exit any symbol when sign of net carry inverts / net < floor |
| 4 | **Basis watch** | spot-perp price divergence | skip entry if &#124;premium&#124; > 1% (perp stretched vs oracle) to avoid entering into a squeeze |
| 5 | **Look-ahead** | using future funding | funding used at decision time is the *current* settle rate, never a forward one |
| 6 | **Regime independence** | relying on trend direction | carry must hold (approx.) flat in both a bull and a bear test window |

This ladder is lighter than the momentum ladder because the strategy does not
claim a predictive alpha — it claims a structural payment. The burden is on
*not overpaying* (gates 2-4), not on overfitting.

---

## 6. Implementation surface

- `service/agent/funding_carry.py` — analysis + signal layer:
  - `fetch_funding_and_mark()` → live per-symbol funding APY, mark, premium
  - `carry_decision(row, equity)` → per-symbol carry signal (side, net APY, size)
  - `scan_carry(equity)` → ranked eligible carries
  - `summarize()` → prompt/log-ready summary
- Execution (the actual perp + spot legs) goes through the **VenueRouter**
  (`service/execution/router.py`) with `OrderIntent(chain, venue, symbol, side,
  qty, idempotency_key, ...)` — the router applies RiskGuard before signing,
  ledger idempotency, writes fills + `fee_platform` + `fee_venue`, and killswitch
  hooks. See `service/agent/live_agent.py` for how a strategy's decision becomes
  an intent.

### Wiring a carry decision into the router (pattern)

```python
from service.execution.order_model import OrderIntent
from service.execution.router import VenueRouter
from service.agent.funding_carry import scan_carry

router = ...            # built via build_router(ledger, risk_profile)
for c in scan_carry(equity):
    if not c["eligible"]:
        continue
    # short_perp -> sell perp (collect); hedge leg handled by platform spot book
    side = "sell" if c["collect_side"] == "short_perp" else "buy"
    intent = OrderIntent(
        chain="hyperliquid", venue="hl-perp", symbol=c["symbol"],
        side=side, qty=c["notional_alloc"] / c["mark"], order_type="market",
        idempotency_key=f"carry-{c['symbol']}-{c['ts']}",
    )
    router.submit(1, intent, c["mark"])
```

---

## 7. Relation to the directional book

- **Use carry for the capital that would otherwise idle** between momentum
  signals — it converts a 0% cash drag into a ~6-26% market-neutral yield.
- The carry **short-perp leg and the directional short leg must not be confused**:
  one collects funding (hedged), the other bets on price (unhedged). A directional
  bear-short and a carry short-perp on the same symbol are different positions and
  must be tracked separately in the ledger (`exec_positions.side`, `qty<0`).
