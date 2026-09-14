# Degen Mode — System Design (Sui / SuiPump mainnet)

Status: DRAFT v6 — rewritten against **verified mainnet on-chain state** (GraphQL
`graphql.mainnet.sui.io`), multi-launchpad venue model (Suipump live / Blast 🔒),
Telegram-first product surface. The earlier draft was based on the open-source
`suipump-contracts` *testnet* code, which differs from what is live. Do not reuse
those assumptions.

## 0. Implementation status
- **P0 engine implemented** in `service/degen/`: chain client (GraphQL+Blockvision),
  ledger, curve validator (hot path), dev-screen, honeypot dry-run (needs funded
  sender), metrics (keyless price/mcap/grad), PTB builders, executor (real fill
  accounting, min-out floor, per-order cap, kill switch, daily-loss), bundle fee
  leg (flat 5 SUI → `NEKO_FEE_WALLET` before legs), deployer-watch sniper,
  order evaluator (rung plans, venue flip on Graduated), Telegram-first UI
  (hub/cards/sheets, 3-min card lifecycle, callback-data via `ui_ref`), runtime
  factory (mainnet-only; dark behind `DEGEN_ENABLED=1`).
- **Integrity (fixed in v6)**: buys execute only on a live `curve` asset; sells
  charge the exit fee on the SUI leg (0.5% → Neko); fills are booked only on an
  explicit `SUCCESS` digest using the real on-chain token delta; slippage floor
  auto-set from the curve (`_min_out`); burst aborts after first-leg failure and
  charges the 5 SUI fee once from the main wallet; post-grad path returns pending;
  sniper re-validates the curve at fire time; `dg:gen5` bundle generation wired;
  receipts pinned (not auto-deleted) while cards/sheets expire at T+180s.
- **P1+ open** (honest): market-swap tx on Aftermath (`swap_tx` upstream
  NotImplementedError → sign-and-submit PTB instead), gRPC streaming upgrade
  (currently GraphQL polling ~2s), copy-Trade ingestion, reprice lanes,
  real post-grad sign/broadcast to Aftermath, honeypot sender funded (`$DEGEN_SENDER`),
  first-funded-wallet calibration of `simulateTransaction` command casing.
- Tests: `tests/degen/` 37 pass; full suite `pytest tests/` = **285 passed,
  1 skipped** as of 2026-09-14.

## 0. Ground truth (all verified on mainnet 2026-09-13)

### 0.1 Contracts / shared objects
| Name | ID | Notes |
|---|---|---|
| Bonding-curve package (current) | `0xb205fea41ccedac051bc66498e6ca68cb802c4a6ea06da12e524bed09c80d9b0` | module `bonding_curve` holds `buy`/`sell`/`graduate`/`Curve<T>`. NOT the `trading` module from the testnet repo. |
| Bonding-curve package (v0, legacy) | `0x7b4163d17ce18b386ee50929ba48fa0a2ecb60304df4b07e26835aa18617cda2` | a few live tokens still point here. Must handle **both** package IDs. |
| PriceConfig (shared) | `0xaebff66fd224e4fefae2426b5cdb30c6e5c488dce5ec34a730246e8b91d44ff9` | arg to `buy`; carries `sui_price_scaled` (oracle) |
| GraduationRegistry (shared) | `0x36a82ab92b7b2b45a137674a5fc1fff89bfd9895653fa32ed7250d30379360a0` | for `..._with_cap` variants (relayer only) |
| LaunchIssuerRegistry (shared) | `0xb622741bfcfd6ef13b40c2d5c2adc8d796f68b3b1254fabaa571bffa3a91e875` | launch-ticket gating |
| Cetus CLMM pool pkg | `0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb::pool::Pool<T,SUI>` | where liquidity lands post-graduation |
| Clock | `0x6` | |

Token type is **per-launch**: each token gets its own published package
`0x<pkg>::suipump::SUIPUMP` (e.g. Suicat `0x63e89267…`, SuiFrog `0x607e781f…`).
`CurveCreated.contents` carries the `type` string — parse the token type + curve
id from the event, never hardcode.

### 0.2 Function signatures (BCS arg order — confirmed via disassembly)
```
bonding_curve::buy<T>(
    &mut Curve<T>, Coin<SUI>, u64 min_tokens_out,
    Option<address> referrer, &PriceConfig, &Clock, &mut TxContext
) -> Coin<T>

bonding_curve::sell<T>(
    &mut Curve<T>, Coin<T>, u64 min_sui_out,
    Option<address>, &mut TxContext
) -> Coin<SUI>

bonding_curve::graduate<T>(
    &mut Curve<T>, &mut CoinMetadata<T>, &PriceConfig, &Clock, &mut TxContext
)
```
`Option<address>` for referrer = `vector<[]>` empty encoding (no referrer).

### 0.3 Economics
- **Graduation threshold: 9,000 SUI** in curve (per-curve `graduation_target` u8 can differ; verify live).
- On graduate: `final_sui_reserve ≈ 8,910` stays in the curve; `creator_bonus 45` +
  `protocol_bonus 45` skimmed (≈1%). Backend relayer then moves reserves + token
  supply into a Cetus pool (≈$8–9K minted liquidity per graduation at current SUI).
- Fees embedded in buy/sell price (constant-product with virtual reserves); the
  live frontend constants: `CREATOR_SHARE_BPS`, buyback, airdrop fees — model as a
  ~1% round-trip effective unless you read `Curve` fields precisely.

### 0.4 Security model (this is the part that changed vs. the old draft)
- `graduate()` is **public and anyone can call it**, but the caller gets **zero**:
  bonus→creator, protocol cut→curve. Verified: 7 Graduated events, 7 different
  sender addresses, none privileged.
- The **liquidity handoff** (`claim_graduation_funds`, `record_graduation_pool`,
  `_with_cap`) requires `AdminCap` / `GraduationCap` → **only SuiPump's relayer**
  can move reserves into Cetus.
- **Therefore "be first to graduate and own the LP" is impossible on mainnet.** The
  testnet `graduation::trigger → ctx.sender()` honeypot is patched. Our bot must
  treat graduation as an *observed event*, never an action to race.
- Creator-reputation (`reputation` module / early-sell flag) is **spoofable** via
  throwaway wallets — do not trust SuiPump's dev-score; build our own (see §5.4).
- No public mempool on Sui: cannot front-run a pending tx; atomic `create_and_return
  + buy` in one PTB (what a dev does) is unbeatable. Best achievable = react to
  `CurveCreated` within ~1 checkpoint (~400 ms).

---

## 1. What "Degen Mode" is

An opt-in high-risk execution lane per user-bot, alongside the existing Swing lane.

**Launchpad venues** (where the meme is born):
- **Suipump** — LIVE, fully audited (§0). Packages `0xb205fea…` + legacy `0x7b4163d…`.
- **Blast** (`blast.fun`) — **announced / coming soon**; we have NOT verified any live
  contracts. Ships as a 🔒 locked tile. Enabling it = same §0-probe obligation:
  package discovery, `buy`/`sell` signatures, graduation model, honeypot/authority
  screen, then a per-Blast validator. No feature-parity promises until that's done.
- **Both** — per-feature multi-feed (sniper/copy subscribe Suipump ∧ Blast); the CA
  resolver **auto-detects** the launchpad from the token package id, so users never
  hand-pick. Until Blast goes live, "Both" behaves as "Suipump" (toggle greyed, not
  silently broken).

Terminology: **launchpad** = Suipump/Blast (token source). **execution venue** =
`curve` / Cetus pool / Aftermath (where we trade). Launchpads sit behind a **Venue
adapter (§2.1)** so Blast slots in without touching sniper/copy/order/cap code.

Four capabilities, all **keyless** (manual PTBs signed by the user's own hot key via
`service/execution/sui_adapter.py`; no SuiPump/Blast API key, no Node sidecar):

1. **Sniper** — sub-checkpoint buys of freshly created curves.
2. **Wallet tracker (copy)** — mirror target wallets' trades.
3. **Agentic orders** — limit / intelligent-DCA / TP-SL on curve + Cetus.
4. **Bundler** *(separate feature)* — split one decision across N owned wallets.

### Hard product constraints (carry over from the master-bot rules)
- Degen only unlocks if the bot's owner has an **AI key** (existing gate).
- Per-order + daily caps, per-symbol cap, correlation guard, **kill switch** (reuse
  the copytrade risk framework already in the repo).
- Degen positions are **excluded** from published signals / copy-trade feed.
- Everything runs on the user's own wallet; Neko never custodies beyond the
  already-stored encrypted key.

---

## 2. Architecture

```
                  ┌────────────────────────────────────────────┐
                  │  Streamer (gRPC SubscribeTransaction*/     │
   Sui mainnet    │  Events) — CurveCreated, Graduated,        │
   gRPC/GraphQL ─►│  per LAUNCHPAD adapter (§2.1):             │
                  │  Suipump pkgs x2 live · Blast = OFF (🔒)   │
                  └───────────────┬────────────────────────────┘
                                  │ normalized events (internal bus)
             ┌────────────────────┼─────────────────────┬─────────────────┐
             ▼                    ▼                     ▼                 ▼
      ┌────────────┐      ┌──────────────┐      ┌─────────────┐   ┌───────────┐
      │  Sniper    │      │ Copy-Tracker │      │  Order Engine│   │ Bundle    │
      │ strategy   │      │ per target   │      │ limit/DCA/   │   │ planner   │
      │            │      │ wallets      │      │ TP-SL        │   │ (N wallet)│
      └─────┬──────┘      └──────┬───────┘      └──────┬──────┘   └─────┬─────┘
            │                    │                     │                 │
            └────────────────────┴─────────┬───────────┴─────────────────┘
                                            ▼
                                  ┌──────────────────────┐
                                  │  Degen Executor      │
                                  │ builds PTB, signs    │
                                  │ (exec_vault hot key),│
                                  │ broadcasts to N      │
                                  │ fullnodes, tracks    │
                                  └──────────┬───────────┘
                           pre-grad          │            post-grad
              launchpad adapter::buy/sell     │   Cetus pool / Aftermath Router
                                            ▼
                                    position/DB + Telegram alerts
```

Single process, one asyncio event loop, reusing the existing bot runtime. The
Streamer is a **shared read resource** (one gRPC subscription feeds all four
consumers); the Executor is the **single write resource** so all caps/kill-switch
logic is enforced in one place.

### 2.1 Venue adapter (the blast-ready seam)
One `Launchpad` interface per launchpad; everything above it is launchpad-agnostic.
Suipump implements it today (§0 facts); Blast returns `status=COMING_SOON` and is
feature-flagged OFF.

```
Launchpad:
  id                          # 'suipump' | 'blast'
  live: bool                  # False => hub tile shows 🔒, 'both' skips silently? NO: greyed + tooltip
  packages: [addr]            # curve pkg(s) to subscribe (suipump: 0xb205fea.. + 0x7b4163..)
  detect(token_type|url|obj)  # auto-classify a pasted CA → (launchpad, curve|pool, token_type)
  read_price(curve|pool)      # -> price, reserves, progress->graduation
  build_buy(ptb, curve, coin, min_out, ref)   # PTB call per launchpad ABI
  build_sell(ptb, curve, coin, min_out)
  feed(events)                # normalize native events -> internal CurveCreated/Trade/Graduated
  validate_curve(obj)         # §5.3 trust boundary, per launchpad (different forged-object risks)
  dev_screen(curve, creator)  # §5.4 heuristics, per launchpad defaults
```
Rules: caps (§5.1) are **global across launchpads** (a 2-SUI order cap is 2 SUI, not
2 per launchpad); position/copy/order tables carry a `launchpad` column + a composite
`(launchpad, curve_id)` key; per-fill toasts badge the launchpad. The AI layer (§3.4a)
gets a `launchpad` field in its plan context — the model may *prefer* one but the
executor still enforces per-launchpad validators. Blast integration is gated on the
§0-probe (its graduation model could differ — e.g. if their trigger has no AdminCap
equivalent, our "never race graduation" rule becomes an ACTIVE exploit surface we
must screen for, not a solved problem).

---

## 3. Components

### 3.0 Metrics engine — pricing, mcap, graduation (ALL keyless, object-read only)
Every number shown on any degen card comes from chain objects we already poll +
our own trade stream. No Suipump API, no Aftermath dependency for metrics.

**Curve state inputs** (one GraphQL object read, ~1s cadence):
`effective_sui_reserve()` / `effective_token_reserve()` / `circulating_supply()` /
`current_grad_threshold()` / `graduated` / `pool_id` — ALL public getters on the live
package (verified). Plus `PriceConfig` (shared `0xaebff66…`) field `sui_price_scaled`
= their SUI/USD oracle.

| Metric | Formula (pre-grad, on curve) |
|---|---|
| price (SUI) | `effective_sui_reserve / effective_token_reserve` (marginal AMM price) |
| mcap (SUI) | `price_sui × circulating_supply()` — circulating, NOT total supply (curve-held tokens aren't cap; honest memecoin convention) |
| mcap (USD) | `mcap_sui × PriceConfig.sui_price_scaled` (÷ its scale; if oracle stale >1h, blend a public SUI/USD spot and label it) |
| FDV | `price_sui × total_supply` — shown only in expanded card, not the headline |
| liquidity | `effective_sui_reserve` (pre) / pool `coin_a,coin_b` reserves (post) |
| volume / trades / buyers / unique wallets | **our streamer** is the indexer (`Trade` events per curve) — same source the UI's 24h stats render from; cross-check vs their API is optional, never a dependency |
| holders / dev % / top-10 concentration | per-address `balances(coinType)` queries for curve creator + top holders (batched, cached 30s) |

**Post-grad pricing** = the Cetus CLMM pool object directly: `current_sqrt_price` →
spot, reserves from the tick range — or cheaper: take Aftermath's `trade-route` quote
for our own order size as the *executable* price (impact included). UI shows
"price now" (pool spot) and "you pay" (route quote) as two lines.

**Graduation calculation** (the marquee metric):
- progress = `effective_sui_reserve / current_grad_threshold()` — threshold is READ
  per-curve (the u8 `graduation_target` + `dampened_grad_threshold` prove it varies;
  9,000 SUI is *this cohort's* number, not a constant). `resolve_grad_threshold(&PriceConfig,&Clock)`
  shows thresholds track the USD oracle → with SUI/USD moving, the *SUI* threshold
  can slope — hence read live, display `target ≈ X SUI (≈ $Y)`.
- `left` = threshold − reserve; **ETA** = `left / EMA(sui_in/sec, 30m)` with the
  confidence band (trade-arrivals are bursty — show "~1-3h", never a fake minute).
- **mcap-at-graduation** (what users actually want: "what's my bag worth if it
  graduates?"): the virtual-reserve constants in the public GitHub repo are TESTNET
  — never reuse. Instead **fit the curve live**: every trade on a curve gives us a
  `(Δsui_in, Δtokens_out)` pair → solve the invariant k per curve from ≥5 trades
  (regression; display a tiny `🎯 fit 99%` chip), then extrapolate price at
  reserve=threshold. Low-trade curves show a dashed "~more data" state instead of a
  fake precise number. Same k drives the sniper's late filter (§3.2: progress>X%).
- `Graduated` event gives `final_sui_reserve` → after each token graduates we log
  actual-vs-projected grad mcap per curve → a public-facing **projection accuracy
  stat** ("our grad estimates: ±7% across last 9 graduations") builds trust and
  regression-tests our fit for free.

### 3.1 Streamer (event ingestion)
- Transport: **gRPC** `SubscribeTransactionEvents`/`SubscribeTransactionEffects`
  for **all live launchpad packages** (Suipump ×2 now; Blast packages auto-join when
  its adapter goes live) + Cetus pool pkg + Aftermath (latency-critical);
  **GraphQL** as fallback + backfill on reconnect (checkpoint-cursor resume, dedup
  by `(checkpoint, eventSeq)`). JSON-RPC is deprecated on public fullnodes — do not
  depend on it.
- "Both" venue mode = union of feeds, one cursor per launchpad so a Blast outage
  never stalls the Suipump pipeline.
- Emits normalized internal events:
  `CurveCreated{pkg,curve_id,token_type,creator,ts}`,
  `Trade{side,curve_id,wallet,sui_in,tokens_out,price,ts}`,
  `Graduated{curve_id,final_sui,creator,ts}`, `PoolRecorded{curve_id,cetus_pool_id}`.
- Must catch up after outage (gap → GraphQL replay) and dedup idempotently — same
  contract as the heartbeat skill already used for AI-Trader.
- Health: watchdog restart, cursor persisted to `state/`.

### 3.1a Graduation state machine — how the bot KNOWS (verified on mainnet)
Three independent signals, in order of authority — the bot uses on-chain, never the
website's API as ground truth (their `/tokens graduated:true` is cross-check only):

| State | Proof | Meaning for execution |
|---|---|---|
| `PRE_GRAD` | `curve.graduated == false` + `pool_id == None` | curve `buy`/`sell` are the venue |
| `GRADUATING` (limbo) | `graduated == true` but `pool_id == None` | **untradeable** — `buy` hard-aborts (code 4, verified in disassembly); relayer is still minting the Cetus pool. Position shows ⏳, never an error; TP/SL rungs pause |
| `POST_GRAD` | `PoolRecorded` observed / `pool_id == Some(id)` | venue = that Cetus pool (route via adapter/Cetus/Aftermath) |

How each is detected:
1. **Events (push)**: `Graduated{curve_id, final_sui_reserve, creator_bonus,
   protocol_bonus, graduation_target}` (fired by anyone calling `graduate()` or inline
   at threshold) and `PoolRecorded` (fired by `record_graduation_pool[_with_cap]`,
   AdminCap/GraduationCap → relayer only). Streamer subscribes both → flip state.
2. **State (pull, ~1-2s polls for hot positions)**: `Curve` object contents:
   `graduated: bool` + `pool_id: Option<ID>` getters (public, confirmed). Also
   `grad_funds_claimed: bool` — the curve's SUI is drained by the relayer's claim, so
   `graduated && sui_reserve==0 && pool_id==None` = funds moved, pool pending.
3. **Execution feedback (absolute)**: a `buy`/`sell` attempt that aborts `ERR 4` on a
   curve = graduated; executor treats abort-4 as a forced re-resolve→flip venue and
   retries once against the pool. (Sells after graduation are DEAD — curve reserve is
   empty; pool is the only exit.)
Pre-grad progress (for UI "%→grad" + sniper late-filter): `sui_reserve /
current_grad_threshold()` — the per-curve getter is public (`dampened_grad_threshold`
too: thresholds can slope, never hardcode 9000).

### 3.2 Sniper — deployer-watch model (owner spec)
**The sniper is a deployer-watcher: the user adds wallet addresses to watch, and the
instant one of them launches a token, we detect it and snipe it.** No watched
wallets = sniper idle (it is never a "buy everything that launches" machine by
default).

**What the user does:**
- `[🪝 Sniper]` → `[➕ watch deployer]` → paste a wallet address (or tap any
  deployer from a token card / wallet profile / leaderboard → `[watch launches]`).
  List is per-bot, unlimited, each with a one-tap `size` chip (0.2/0.5/1 SUI) and
  ON/OFF.
- Mode: `⚡ auto-snipe` (default once a watched deployer has ≥1 previous non-rug
  launch; otherwise `👀 suggest` first-fire confirmation) · `Exits`: `🎯 attach
  TP/SL` / `bare`.
- Optional filters per watched wallet: `min dev buy ≥ chip` (skip empty launches),
  `first N sec only` (skip if we're late), `dev-screen 🟢 required` (ON default).

**What the system does per `CurveCreated{creator ∈ watched}` (hot path, no LLM):**
parse → curve ref from the event → §5.3 validator + §5.4 dev-screen (<50ms,
funding graph pre-cached) → size+clamp (caps/correlation/kill-switch) → sign →
multi-node broadcast → on fill: position `via 🪝` + attach exits + `[🧺 spread to
bundle]` tap. A watched wallet that also *buys* an existing token is **copy**
(§3.3), not sniper — two feeds, one streamer, clearly separated in the UI.
- **Bundled launch snipe**: if the user has a bundle and `💥 spread` is ON for the
  wallet, the snipe fires the spread-burst (§3.6) instead of a single leg.

**The physics, stated honestly in UI:** the dev's launch buy is atomic with
creation — **it cannot be bought before**. Watching a deployer = first public
entry, landing target = next checkpoint after `CurveCreated` (~0.4–1.5s end-to-end);
we log per-fill `lag_ms` and show the distribution.

### 3.2a Gas engineering (snipe hot path)
**Myth to kill before anyone builds on it:** Sui has no fee-bidding mempool —
`gas_budget` is a **cap, not a priority**. Validators include txs within checkpoints
regardless of budget, so paying a 1 SUI cap does NOT snipe faster than a 0.1 cap.
What it *does* buy: tolerance for storage-heavy multi-object PTBs and safety margin
against pathological overcharge. **A failed/aborted snipe pays only executed work
(typically 0.001–0.005 SUI)** — a 1 SUI cap costs nothing extra when the tx aborts.

Config: `snipe_gas_cap` default **1 SUI per tx** (owner's call, accepted as sane cap;
not needed for speed). Display realized-vs-cap always (`you paid avg 0.0026 SUI`) so
users never confuse cap with fee. Each bundler/fan-out leg signs with **its own**
budget + its own gas coin.

The **actual** speed levers (this is where the snipe is won):
1. **Pre-split gas pool** — `gas_pool` table: N small `Coin<SUI>` (~0.1 SUI) split
   from the treasury at idle; a snipe PTB references one ready coin → **zero
   `SplitCoins` work, minimal PTB, tiny tx** (pool size = max concurrent in-flight
   snipes; janitor refills at idle).
2. **Minimal PTB** — one gas coin, one split-of-sui for the buy amount (pre-split too:
   `buy_pool` of sized coins), one `launchpad::buy<T>`, `TransferObjects` to user.
   No batching: batched PTBs = bigger verify + more input refs = slower *and* all-or-nothing
   fails together. Baskets (§3.6) are the batched-PTB path, explicitly not snipe.
3. **Fresh object refs, zero lookups** — curve id+version arrive in the event payload;
   treasury/registry refs cached with version-refresh janitor. Stale-ref → single
   re-fetch retry inside the same checkpoint window.
4. **Broadcast fan-out** — submit the signed tx to **3–5 independent fullnodes**
   simultaneously (different operators/providers); first-in wins; no mempool racing,
   just maximize the chance the next-authority's node holds it.
5. **`gas_price = max(provider minimum, reference × 1.1)`** — some public RPCs reject
   at/below reference; 10% over reference only dodges *rejection floors*, it is NOT
   priority (don't advertise it as such).
6. **Pre-validated templates** — snipe PTB shape is fixed (buy<new type>): dryRun
   once per shape/launchpad, cache `effects` + real cost; per-fire path does
   build→sign→send with no simulation round trip (simulation adds a full RTT —
   suggest mode can afford it, auto-fire must not).
7. **Storage hygiene** — janitor burns dead dust positions (`bonding_curve::burn_tokens`
   is public on mainnet) + merges stray gas coins → reclaims SUI storage rebates
   so the pool self-funds its own overhead.

**Fee routing — read this before anyone says "let the leftover gas go to our fee
wallet":**
- `gas_budget` is a **ceiling, not a payment**. Unused budget is *never charged* — it
  simply remains in the user's own gas coin. There is no "change" flowing anywhere to
  redirect. The fee actually paid = gas units × gas_price, and that goes to **Sui
  validators**, not to us. Higher budget/price also buys zero priority (no mempool
  auction) — so "pay max, skim the rest" has nothing to skim.
- A failed/aborted snipe still only pays executed work (~0.001–0.003 SUI). The 1 SUI
  cap is pure safety headroom (oddly large PTBs / storage edge cases), not a cost.
- **Fee schedule (owner spec):**
  | Action | Fee | Mechanism |
  |---|---|---|
  | single buy/sell (curve or Aftermath) | 0.5% | in-PTB split leg (§below) pre-grad; `integratorFee` post-grad |
  | **any bundle / spread-burst** | **flat 5 SUI → NEKO_FEE_WALLET** | first leg of the burst PTB batch: `SplitCoins(5 SUI)` → fee wallet, charged per burst, shown on Confirm |
  | sniper fills | 0.5% | same as single buy (snipe ≠ bundle unless `spread` ON → then the 5-SUI bundle fee applies) |
  - `referrer: Option<address>` is recorded (`referral_fee` in trade events) but the
    bonding_curve module contains **no on-chain payout to the referrer** (only
    `assert_no_self_referral`) — don't build revenue on it without their docs; payouts
    on the curve are CreatorCap-controlled (`payouts: Vec<Payout>`), not buyer-settable.
  - Fee wallet = cold, sweep-on-threshold; it receives SUI/token legs only — never holds
    user keys, and degen positions stay user-owned (fees are separate output legs).

### 3.3 Wallet tracking / copy — user mechanics
**Two layers, not one** (the *monitor* is generic Sui, the *copy* is launchpad-scoped):
1. **Wallet monitor (ANY address, ALL movement)** — gRPC subscribe to txs that touch
   the address + ~10s GraphQL balance poll. Tracked as a live asset feed:
   SUI balance, every token balance, and **every movement**: buys, sells, deposits,
   *transfers to a fresh wallet* (dump signal), transfers to CEX-tagged addresses
   (exit signal). The Tracker card shows `wallet state + last-20m movements`; copy
   rules can ride *non-launchpad* exits ("moved 60% of $X out → alert + auto-sell mine
   if slippage < cap"). This is the "tracks asset movement and balance" surface.
2. **Copy engine** (§below) acts on that stream: `Trade` on a Suipump/Blast curve =
   actionable mirror; other movements = alerts + invalidators, never blind trades.

**What the user does:**
1. `[👥 Copy wallets]` → paste an address (or forward a tx/screenshot we parse) →
   `WALLET PROFILE` card built from OUR streamer history, never from launchpad data:
   - win rate, realized SUI P&L, #trades, avg hold time, realized +/− histogram;
   - launchpad split (Suipump vs Blast), pre-grad vs post-grad style;
   - **rug exposure** — how many wallets they bought ever failed §5.4 later;
   - top wins/losses by symbol (tapping one opens that token's card + who else profited).
2. `[Follow]` → **policy chips** (each is one tap, stored per wallet):
   - `Copy size`: `25% of their size` / `fixed 0.5 SUI` / `max per trade…` — mirrored
     buy SUI = `clamp(their_sui_in × pct, min 0.1, per-order cap, daily cap left)`;
   - `Mode`: `📣 Notify-first` (DEFAULT: we post "🐋 0x12…ab bought 2 SUI of $X —
     [Copy now] [Skip]", auto-expires in N seconds) vs `⚡ Auto-fire`;
   - `Scope`: this launchpad / both; `Buys only` vs `exits too`;
   - `Dedupe`: skip if we already hold/sniped the same curve (anti-stacking).
3. `[Follow leaderboard]` — ranked wallets (same metrics) so users pick proven
   wallets instead of guessing; users can also *share* their own leaderboard status.

**Deployer-follow (shared primitive with the bundler, §3.6):** follow an address *as a
deployer*: watch `CurveCreated` where `creator = addr` → arm entry on their next
launch. Same arming path the bundler consumes, minus the spread. Funding-graph check
(§5.4) runs on the deployer at fire time; fails → refuse + tell the user why.

**What the system does per tracked `Trade` event** (no model call):
resolve `wallet ∈ targets ∧ scope ∧ dedupe ∧ caps-left ∧ !kill-switch ∧ dev-screen pass`
→ arm one-shot intent: pre-grad = `launchpad::buy` w/ tight slippage; post-grad =
Cetus/Aftermath route → fill → position tagged `via @wallet-name` everywhere (cards,
history, P&L). Copy **sells** fire TP/SL-style exits at the same policy.

**Safety invalidations (auto-unfollow + tell the user why):** wallet's recent buys
rugged >X% of times · wallet is creator-linked (wash pattern, §5.4) · their wins all
predate a data gap · user's copy P&L breaches a sub-cap → lane OFF, not bot-wide.
Notify-first means the *user's* click is the trigger — copy failures never surprise
them, and one bad whale costs at most one prompt they ignored.

### 3.4 Agentic order engine (limit / DCA / TP-SL)
- Reuses existing `OrderIntent` (`order_model.py`: market|limit|stop|take_profit).
- **On-curve limit**: scheduler polls `Curve` price via `current_price`/reserves
  (GraphQL) at ~1s; when crossed → fire buy/sell PTB. (No on-chain resting orders
  on a bonding curve; this is a monitored trigger, not a book order — label it
  "conditional order" in UI to stay honest.)
- **Intelligent DCA — user mechanics**: from any position card `[➕ DCA]` opens the
  **lane editor** — all tap-based, no typing:
  - `Total DCA budget`: chips `1 / 2 / 5 SUI / Max%` (hard-clamped to remaining caps);
  - `Style`:
    `🪃 Dip ladder` = add on −10/−25/−40% from avg price (3 preset ladder shapes:
    Light 2-lane / Standard 3 / Heavy 4),
    `📈 Momentum ladder` = add on +15% / +35% WITH graduation progress, but only
    while dev-screen holds and volume rising — "pyramid the winners" thesis;
    `🎯 Target price` = one rung at a user-picked price (from card sparkline tap);
  - `Split`: `Equal` or `Back-load (30/70)`;
  - `Always on` (in every DCA): aggregate basket stop from §5.1 (lanes can never push
    total loss past the position's SL), and `Cancel all on graduation pump`-style
    **invalidation chips** (`dev sold?` / `volume dead?` / `graduated & −X%`).
  - The card shows live lane state: `● fired 0.5 @ 4.1M · ◐ armed 1.0 at ≤3.3M (−20%)
    · ○ 1.5 at ≤2.5M` with per-lane `[pause][edit][cancel]`.
  - Every rung re-fires through the §5.3/§5.4 screen **at trigger time, not setup
    time** — a token that rugs after you armed the ladder does NOT get its lane
    executed; we cancel, explain, and re-price the remaining plan.
  - Post-graduation: unfired lanes **compile into Aftermath resting limit orders**
    (§3.5) re-anchored to pool price — no more watcher; ⏳ GRADUATING limbo (§3.1a)
    pauses them until `pool_id` appears.
  - AI mode: the LLM **sizes each rung at runtime** from live context — chip / reserve
    depth, curve slope (k), sell-side pressure, recent Graduated spacing — emitting a
    concrete `lane_shape_json` `[{dip_pct,size_share},…]`. The editor shows it live
    with a ✨ badge and it is the working plan; user can downgrade to preset or
    hand-edit before it arms. Without AI key → presets only (Light 2 / Standard 3 /
    Heavy 4). The model never fires a lane; the deterministic fast loop does.
- **TP/SL**: from position card `[🎯 TP/SL]`: chips (`+50/+100/2x/4x`, `−25/−50/hard`)
  → one conditional sell each; `trailing` toggle (reuse the live_agent trailing-high
  cache pattern) so "let winners run" is one switch. Full-partial chips: `sell 50%
  at TP, let 50% ride trailing`. Pre-grad = `sell`; post-grad = pool route. Auto-
  escalates at the `Graduated` event (position venue flips curve→pool).
- **Exit slippage**: pool exits are thin; enforce min-out with a bounded retry and a
  hard "if unfillable, mark illiquid + alert" branch.

### 3.4a AI mode — planning vs. execution split (`live_agent.py`)
Reuse the existing slow/fast split; **never put the LLM in the order trigger.**
- **Slow layer** = `ask_model()` (interval, `_last_llm_at`-throttled, 1 high-conviction
  decision/cycle). It **sizes the plan live at runtime**: emits a structured
  `degen_plan` (entry thesis, conviction, TP/SL %, and the DCA **lane shape**
  `[{dip_pct,size_share},…]` re-computed each cycle from current reserve/curve
  state, invalidation rules, venue preference).
- **Fast layer** = deterministic evaluator: reads curve/pool price ~1s, arms N
  conditional rungs from the plan, fires the Executor on cross, **re-sizes remaining
  lanes for the new average price (pure math, no model call)**. Limit/TP/SL = the
  existing bps-offset + clamped `stop_loss_pct/take_profit_pct` machinery.
- Model output is **advisory → `validate()` + clamp + §5.1/§5.3/§5.4 screen** before
  any rung can arm; the model cannot place what the deterministic layer rejects.
- Graduation flips venue mid-plan mechanically (curve→Aftermath/Cetus) without a
  model turn; the next model cycle just re-reads it as feedback.
- Cost stays **flat**: LLM spend = cycles, not armed-rung count (8 rungs ≠ 8 calls).
- Fail-static: model error/timeout → keep last valid plan, keep executing rungs,
  never invent a trade. Kill-switch is independent of any model turn.

### 3.5 Post-grad venue — Aftermath swaps (imports the `AftermathSpotAdapter` LIBRARY)
**Naming note so this never confuses product again:** `service/spot/` is the internal
*package* for Aftermath's swap+limit-order API (vs `service/execution/` = perps).
Degen consumes it as a **library at adapter level** — there is NO "Spot" tab or copy
in the degen UX; the planned standalone "Spot" dashboard section (that package's
README §65) is a separate long-only product line and stays outside degen. All
post-grad trades remain inside 🎰 DEGEN, badged by venue: `🐸 curve` vs `🎨 Aftermath`.
The Aftermath spot adapter already implements everything degen needs; the degen bot
uses it as a LIBRARY (`AftermathSpotAdapter` + `SpotGateway`), no new API surface.
Keyless + self-custody: Aftermath returns **serialized PTBs**, we sign with the
user's key (exec_vault) and broadcast via the same GraphQL path the perps adapter
uses. Auth for signed reads = one-time terms signature (b"Aftermath Terms and
Conditions", Ed25519, cached per wallet).

**Buy/sell flow for a `POST_GRAD` position:**
1. Inputs: `token_type = 0x<pkg>::suipump::SUIPUMP` (from `CurveCreated`) + `pool_id`.
2. **Routeability probe**: try `POST /router/trade-route {SUI → token, amount, slippage}`.
   Success → cache the positive (`route_cache`, TTL 10m). Error "unsupported" → the
   token's fresh Cetus pool isn't in their routing yet — fall back to **direct Cetus
   CLMM PTB** (§3.5a) and retry Aftermath on a backoff (60s→10min).
   NOTE: `supported-coins` is a 7.8 MB / 24h-cached list — too stale+heavy for the hot
   path; direct-quote probes govern freshness, background refresh keeps the cache for
   browsing UIs only.
3. **Market swap**: `trade-route` → compose via `transactions/add-trade` into our own
   minimal PTB (the documented flow; `transactions/trade` schema-undocumented) →
   sign → broadcast → effects → fill.
4. **Min-order + impact gates**: `/limit-orders/min-order-size-usd` checked before
   arming any rung; quote's price impact surfaced in the confirm sheet and **refused
   > X% (default 8%)** — post-grad meme pools are thin, this is the #1 exit risk.

**Post-grad orders get UPGRADED semantics (real on-chain, not watched):**
- `limit` → Aftermath **resting limit order** (`create-order`: allocateCoinType/amount,
  buyCoinType, `outputToInputExchangeRate`, expiry) — no watcher needed, cancel via
  signed `/limit-orders/cancel`.
- `stop`/SL → `outputToInputStopLossExchangeRate` field on the SAME resting order —
  native SL, survives our process dying. DCA lanes post-grad = resting orders.
- TP → resting limit sell; ladder fires on their book.
- So the AI/fast-loop split (§3.4a) shifts venue: PRE_GRAD = watcher arms PTB fires;
  POST_GRAD = plan compiles to resting orders, watcher only tracks/re-arms on expiry.

**Fee wallet (§3.2a follow-up, fully resolved):**
| Venue | Fee mechanism |
|---|---|
| Pre-grad curve (our PTB) | in-PTB `SplitCoins(fee)→NEKO_FEE_WALLET` leg (§3.2a) |
| Post-grad Aftermath limit | **native `integratorFee {feeRecipient, feeBps}`** — already wired (`AFTERMATH_FEE_ADDR`, 50 bps) |
| Post-grad Aftermath swap | no integrator field → post-fill USDC sweep (existing gateway behavior) or fold into same-sweep |

**Fill/P&L accounting**: Aftermath quote's expected out + returned route fees are
recorded pre-trade; realized from tx effects; `VENUE_FEE_BPS["aftermath-spot"]` set
to their real taker fee (verify per quote, they pass it in `trade-route` response).

### 3.5a Cetus-direct fallback (thin/fresh-pool or Aftermath outage)
- Build the CLMM swap PTB ourselves against `pool_id` (type
  `0x1eabed72…::pool::Pool<T,SUI>`): `cetus::swap` with known sqrt-price bounds from
  `current_sqrt_price` + our slippage → same sign/broadcast hot path, fully keyless.
- Use when: Aftermath hasn't indexed the fresh pool yet (minutes post-grad), route
  errors, or the aggregated route is worse than direct (compare quote before firing —
  cheap: both are off-chain reads).
- Same §5 guards: min-out enforced, impact cap, caps, kill-switch (its cancel-all must
  also cancel Aftermath resting orders → `/limit-orders/cancel` loop added to §5.1).

### 3.6 Bundler — user mechanics (separate feature)
**What it actually is on Sui (physics, not opinion):** a PTB has ONE sender, so
"buy the same token from 5 wallets atomically" does not exist. Two real, shippable
mechanics — both **own-wallets-only**, third-party/fresh wallets declined (§manip):

1. **Spread-burst (1 decision → N wallets, `💥 immediate`)** — the headline flow:
   user gives a **CA**, a **deployer wallet**, or arms `[🪤 next launch]` on a
   deployer-follow → bot takes a **budget** (never the whole balance without an
   explicit `Max`), splits it across their wallets and buys the position in all of
   them back-to-back (~same checkpoint most of the time, NOT atomic).
   - **Owner spec defaults**: bundling is a **paid feature — a flat 5 SUI fee goes to
     NEKO_FEE_WALLET per spread-burst** (that 5 SUI is the price of using the bundler;
     the trade budget is chosen on top of it via chips 1/2/5/10 SUI or Max), **up to 20
     wallets** per bundle, speed is the point → `legs pre-funded` is the default
     funding mode (JIT shown as the slow option).
   - **Wallet generation = the SAME code path as onboarding**: legs are created with
     `exec_vault.generate_key_material("sui")` (the exact function the introduction /
     `⚙️ Generate Wallet` flow uses — `userbot._generate_user_wallet`), NOT pasted
     third-party keys. `[🧺 + generate wallets ×N]` one tap → N fresh wallets appear.
   - **Key storage rule**: every leg key is Fernet-encrypted via `ExecVault` into
     the **user's own bot DB row** (same store as their main wallet), keyed per bot,
     one slot per wallet. Keys are **never mixed/shared across bots, users or legs**
     — no reuse "for no reason": leg i signs only leg i's tx. Export = per-wallet
     `[reveal]` behind bot-auth; `[delete]` sweeps remaining gas to main first.
   - Funding modes, cost stated: `legs pre-funded ✓ (1 tx per wallet — fastest)`
     vs `⏱ JIT fund`: one atomic main-wallet PTB (`SplitCoins` + `TransferObjects`
     SUI to each leg) → then N buy PTBs per wallet ⇒ **+1 checkpoint (~+400ms)**.
   - deployer-input resolution: addr → latest `CurveCreated{creator=addr}` = `buy now`;
     `[🪤 next]` = armed watcher that fires the spread on their *next* launch
     (passes §5.3/§5.4 + caps at fire time, one-shot or sticky, auto-expires).
   - `[🧺 Bundled buy]` on the Token Card: amount chips `5 SUI ✓` (default) and split
     preview `5 = 0.25×20` (`[⚖️ even]` / `[🎲 custom lanes]`); Confirm sheet states
     the non-atomicity: "N separate txs; if some land and others don't, you'll be
     told instantly."
   - **Orphan policy chip** (pick at setup): `rebalance-sell` / `leave + alert` /
     `abort-all-on-first-fail`. Failed legs are never silently ignored.
   - Global caps still count the SUM of legs; correlation guard counts the token;
     platform fee leg → NEKO_FEE_WALLET (§3.2a) on every fill, incl. each burst leg.
2. **Basket (1 wallet → N tokens, one PTB, atomic)** — `SplitCoins` + N
   `launchpad::buy` calls in one tx: "new-tickernight, 0.5 SUI into each of these 8
   launches" with a per-token `min_out`. Real atomicity, per-wallet only. Max legs
   per PTB ~8–10 (budget/gas guardrail).

**How it composes with the other features:** DCA lanes can **rotate wallets**
(`lane1→w1, lane2→w2…` chip `🔀 spread lanes`) so averaging-down doesn't concentrate
risk; sniper `Bundled entry` fan-out per launchpad; Copy legs go to the wallet the
user designates per tracked-whale (`copy into: main / bundle-slot-2`); spread-burst
auto-arms TP/SL per leg so one rug leg can't drag the others' budget.

**UI honesty:** the Hub tab stays `🧺 Bundle` even for fan-out; the setup card
line: "Same-wallet basket = one tx (atomic). Multi-wallet = fast parallel txs, not
one." No fake "atomic bundle" claims.

---

## 4. Data model (additions)
```
degen_config(user_id)        : enabled, ai_key_ok, mode[], launchpads[suipump|blast|both], budget_sui, caps, track_wallets[], bundle_wallets[]
degen_position(id,user,launchpad,curve,pkg,token_type,venue[curve|pool],entry_sui,tokens,qty,status,graduated,open_dca_lanes[],tp,sl)   # key: (launchpad,curve)
degen_plan(id,user,launchpad,curve,source[manual|ai],conviction,thesis,tp_pct,sl_pct,lane_shape_json,invalidation_json,venue_pref,ttl)  # LLM slow-layer output → compiled to degen_order rows
degen_order(user,plan_id,intent,type[market|limit|dca|tp|sl],launchpad,curve,target_price,qty_sui,state,fires)
degen_fill(order_id,tx_digest,checkpoint,sui,tokens,price,leg_index)
wallet_score(addr)           : win_rate, realized_sui, hold_secs, trades, updated_at   # our own, not SuiPump's
stream_cursor(launchpad)     : checkpoint, event_seq                                    # per-launchpad resume/dedup
gas_pool(coin_id,wallet,amount_mist,state[free|inflight|refill])  # pre-split gas+buy coins, snipe hot path
bundle_wallet(bot_id,slot[1..20],label,address,key_ref,   # key_ref → per-slot Encrypted in the USER'S bot DB row (ExecVault); never shared across bots/legs
              prefunded_mist,gas_mist,state[active|sweeping|deleted])
snipe_watch(bot_id,deployer_addr,size_sui,mode[auto|suggest],spread_burst:bool,on,created_at)
snipe_log(id,launchpad,curve,decision[fired|suggest|rejected],reason,lag_ms,filled,realized_gas_mist)
launchpad_adapter_state(id)  : live, packages[], probe_done_at, graduation_model_notes  # Blast lives here until §0-probe passes
```
Reuse `exec_vault` (Fernet hot keys) for signing. Reuse existing DB layer/migrations.

---

## 5. Risk & safety

### 5.1 Caps (enforce in Executor, single choke point)
- per-order SUI cap, per-day loss cap, per-symbol (curve) exposure cap, max open
  degen positions, global kill-switch (manual + auto on drawdown).
### 5.2 Slippage & MEV
- `min_tokens_out`/`min_sui_out` always set (no naked swaps); tight slippage on
  sniper, wider-with-alert on exits. No cross-tx atomicity → assume adverse
  selection between our read and execution; size down accordingly.
### 5.3 Rug/honeypot screen (our own)
- Curve is `key`+`store`, `paused` flag, buyback/lock configs, `Option` metadata
  fields can change → **verify `curve_id` is a real SuiPump curve of the known
  package** (PDA-like trust boundary): reject unknown packages, reject curves whose
  creator isn't in `CurveCreated`. This is the P0 item from the last audit
  (buying an attacker-crafted object = no protection).
### 5.3a Honeypot test = buy→sell-back dryRun (the actual mechanism)
Mainnet `create_and_return` takes the creator's `TreasuryCap` as an argument →
creators publish **arbitrary token packages** → transfer restrictions are a real
risk. Test: `dryRunTransactionBlock` with a minimal PTB (split 0.01 SUI-equivalent →
`launchpad::buy` → `launchpad::sell` the received coin → drop dust). Simulation
needs no real funds and no on-chain effect; a revert on the sell leg = honeypot
for this exact curve state, surfaced verbatim on the card (§6.3a step 4).
### 5.4 Dev/wallet screen (replace SuiPump reputation)
- Creator funded-from-fresh-wallet, creator buys via multiple wallets pre-launch,
  creator holds >X% across linked wallets, dev sells in first N minutes → block/flag.
  Use our `wallet_score` (own data) + Sui funding-graph (gRPC trace of creator's
  funding source). Never trust `reputation`/early-sell flag.
### 5.5 Failure modes
- gRPC gap → replay via GraphQL before firing (don't snipe blind); duplicate event
  → idempotency key on `tx_digest`; orphaned leg in bundler → reconcile + halt lane;
  fullnode broadcast fails → retry across nodes, else cancel (never assume fill).

---

## 6. Product & UX — Telegram-first (the surface users actually touch)

`service/frontend/` is stale (only a baked `dist/`, no source) and the live product
IS Telegram inline cards (`userbot.py` HTML panels + `callback_data` buttons +
`_safe_edit` edit-in-place). So degen is **not a new web app** — it is a set of
edit-in-place Telegram screens hanging off the existing dashboard, in neko-chan's
voice. A shareable web token page is P-later, not P0.

### 6.1 Screen map (each screen = one editable message; nav = inline buttons)
```
DASHBOARD (existing) ──[🎰 DEGEN]──► DEGEN HUB
                                        ├─ status banner (OFF/ON, AI-key gate)
                                        ├─ venue row: [🐸 Suipump ✓] [💣 Blast 🔒soon] [⚡ Both]
                                        ├─ [Buy a meme] [My positions] [Orders]
                                        ├─ [Sniper] [Copy wallets] [Bundle]
                                        ├─ [Risk & caps]  [KILL ⛔]
                                        └─ one-tap presets: Suipump recent-grads / trending
                                           (Blast tab appears only when it's live)
DEGEN HUB ──[Buy a meme]──► CA → RESOLVER → TOKEN CARD ──► CONFIRM SHEET ──► FILL RECEIPT
TOKEN CARD ◄────────────────────────────────────────────────────────────────┘
DEGEN HUB ──[My positions]──► POSITIONS LIST ──► POSITION (manage TP/SL/DCA/Sell)
DEGEN HUB ──[Orders]──► ARMED RUNGS (conditional orders list) ──► edit/cancel
DEGEN HUB ──[Sniper]/[Copy]/[Bundle]/[Risk]──► settings screens (toggles + numbers, no jargon)
```
All screens are reached by tap; **typing free text is never required to trade**.

### 6.2 Venue chooser (Suipump / Blast / Both)
- Hub venue row is a 3-chip toggle stored in `degen_config.launchpads`:
  - `🐸 Suipump ✓` — active.
  - `💣 Blast 🔒` — greyed, tap = a 3-line teaser card: what it is, what "live"
    unlocks (sniper/copy/browse on Blast curves), `[🔔 Ping me]` waitlist button
    (writes `waitlist_blast`), and an honest status line: *"not trading yet — we
    only enable it after we've audited their contracts, same as we did Suipump."*
    This mirrors the existing Solana/Hyperliquid "coming soon" pattern in
    `messages.py` — same product language everywhere.
  - `⚡ Both` — selectable but behaves as Suipump until Blast is live; helper text
    "auto-routes each CA to its launchpad".
- **Pasted CAs auto-route regardless of the chip** (`detect()` per §2.1): chip governs
  *browsing/sniper/copy scope*, never manual-trade eligibility — resolve + pass the
  right gauntlet (§5.3/§5.4 launchpad, §6.3b generic) = tradeable even from a tab the
  user didn't pick. (A user on "Suipump" pasting an Aftermath-only graduated token
  gets the generic card, not a refusal.)
- Token Card always carries the launchpad badge (🐸/💣) next to the venue badge so
  "which launchpad + which execution venue" is never ambiguous.

### 6.3 The money path: drop a CA → buy a meme (≤3 taps from Hub)
Accept ANY of these in the Buy input (one resolver per launchpad, so users can't get it wrong):
`0x<pkg>::suipump::SUIPUMP` (token type) · a curve id · a Cetus pool id · the
`suipump.org/...` URL · a GeckoTerminal Sui URL · a bare token object id.
The **resolver** figures out: which of the two packages, pre-grad (curve) vs
post-grad (pool), symbol/name, live price — then renders the Token Card.

**Token Card** (one screen, top-to-bottom decision order):
1. Header: icon + NAME ($SYM) + state badge: `🟢 LIVE on curve` / `⏳ GRADUATING —
   pool pending, untradeable (usually <1 min)` / `🎨 GRADUATED → pool` + age (§3.1a).
2. **Risk strip** (the §5.3/§5.4 screen, translated to color + words, not numbers):
   `🟢 Safe to trade · 🔵 Dev holds 3% · 🟡 61% to graduation · 💧 LIQUIDITY OK`
   — plus the explicit PASS/FAIL of the honeypot check ("can you sell?" tested live).
3. Price + mcap + a tiny sparkline (reuse existing chart templates), progress bar.
4. **Amount chips**: `0.5 SUI` `1` `2` `5` `Max` `✏️ Custom` — no decimals ever shown
   to the user; SUI is the unit.
5. **Slippage** as a preset pill (Safe 5% / Normal 10% / Degen 25%), with the resulting
   `You get ≈ N $SYM` line updating live under it.
6. Big `🚀 BUY $SYM` button + `⚙️ Set TP/SL/DCA now` (skippable).

**Confirm Sheet** (last stop before it costs anything, so it can say everything once):
"Buying 1 SUI of $SYM at ≈ $0.0000xyz. You'll get ≈ 4.1M $SYM. Slippage 5% →
worst case 3.9M. Service fee 0.5% (≈ 20.5K $SYM to Neko) — all-in, no hidden gas
markups: you pay chain gas of ~0.002 SUI at any outcome. **Honeypot check PASSED** ✅.
Post-buy you'll hold 0.4% of supply. Daily cap remaining: 6 SUI." + `[Confirm] [Cancel]`. If the §5.3/§5.4 screen **fails**,
this sheet never opens — the card turns red with the reason ("⛔ can't sell this —
transfer is locked; likely honeypot") and BUY is disabled.

**Fill Receipt**: digest ✓, price paid, qty, current value, and ONE tap `[Add TP/SL]`
`[Sell]` — position then lives in Positions.

### 6.3a What happens when a user pastes a CA (full resolve pipeline)
In degen mode the bot treats ANY `0x…` the user sends in chat as a CA paste (no need
to press [Buy a meme] first — paste to the bot = same pipeline). Sequence:

```
paste → [1 normalize] → [2 identify asset] → [3 classify venue/state]
      → [4 safety gauntlet] → [5 render card] → user decides → [6 execute]
```

**1. Normalize** — accept: `0x<pkg>::<mod>::<SYMBOL>` type string · object id (curve,
pool, or token pkg) · suipump.org / blast.fun URL (regex out the id) · GeckoTerminal
Sui URL. Bare `0x…+64hex` is ambiguous (wallet vs object): try object lookup first;
not an object → treat as address → `WALLET PROFILE` (§3.3) or deployer card with
`[follow as deployer 🪤]`.

**2. Identify** — GraphQL object fetch; must exist & be live (pins `version` for the
whole card — refs are reused at buy time, stale-ref → single re-resolve). If the paste
is a token PACKAGE/type (not a curve object): find the curve by **object type** —
`objects(filter: {type: "<launchPkg>::bonding_curve::Curve<0x<pkg>::suipump::SUIPUMP>"})`
(ObjectFilter supports `type` — confirmed on GraphQL). Try Suipump live pkg, then
legacy pkg, then Blast when live; ≥1 match = launchpad token.

**3. Classify** via §3.1a state machine (curve `graduated` + `pool_id`, or pool type
`0x1eabed72…::pool::Pool<T,SUI>` on paste):
| Outcome | Card |
|---|---|
| curve, `!graduated` | **TOKEN CARD — pre-grad**: price from reserves, %→graduation, curve buy/sell path |
| curve, `graduated`, `pool_id None` | **⏳ GRADUATING card**: facts + "tradeable in ~seconds" + `[🔔 notify when pool live]` |
| graduated, `pool_id Some` (or pool id pasted) | **POST-GRAD card**: Aftermath quote/price impact, pool liquidity, resting-limit/SL options (§3.5) |
| no curve anywhere, Aftermath routes it | **GENERIC card** (§6.3b) — tradable in degen regardless of venue chip |
| unknown pkg, Aftermath errors | **HONEST REJECT**: "not a Suipump/Blast token and not routable — here's what we can trade" |
| matches Blast pkg pre-launch | **🔒 coming-soon card** (§6.2) |

**4. Safety gauntlet** (runs before the card can enable BUY, ~parallel, <2s):
- §5.3 **curve validator**: curve object type/creator/registry all agree with the
  `CurveCreated` event (forged-object defense);
- metadata: name/symbol/icon from their indexer API (`onrender/token/<curveId>`) with
  on-chain fallback — always labeled *creator-supplied* (scam names aren't our names);
- §5.4 **dev/wallet screen** (funding graph, dev holds, dev sells);
- **LIVE honeypot test** (§5.3a): `dryRunTransactionBlock` of a tiny
  **buy→sell-back PTB** — inside the simulation the bot-owned coin from the buy funds
  the sell, so a success = "sellable right now" proof against THIS curve state.
  Fails → red card with the reason; passes → `honeypot ✓ tested live` (the ✅ users
  see in the confirm sheet is this dryRun, not a vibes score).

**5. Render** the matching card (§6.3 layout). Everything the user then taps is
already validated; **6. Execution** re-checks only the cheap invariants at tap
(anti-bot delay elapsed? still `!graduated`? caps? kill-switch?) and re-runs the
dev-screen for armed lanes at fire time (§3.4 rule: screens run at TRIGGER time).

### 6.3b Generic tokens (not Suipump/Blast, but Aftermath-routable)
The venue chips (🐸/⚡) gate **browsing, sniper feed and copy scope** — never a
manual paste: if you're in degen and paste a CA that lives only on a DEX (graduated
token from an unknown/other launchpad, or any Aftermath-supported coin), you get the
**GENERIC card**: price/liquidity from the Aftermath quote itself (they aggregate
Cetus/Turbos/FlowX/7k — the route IS the market data).

Degen feature map on generic tokens:
| Feature | Generic token |
|---|---|
| market buy/sell | ✅ Aftermath PTB (user-signed, §3.5) |
| TP/SL, limit, DCA | ✅ **resting limit orders + native SL** (the good tier) |
| graduation progress/%→grad | ❌ no curve exists — strip hidden, not shown as 0 |
| dev-screen (§5.4 curve heuristics) | ❌ replaced by **package audit** below |
| sniper / deployer-follow / copy-mirror | ❌ out of scope by default (their feed is launchpad-shaped); copy still *alerts* on tracked-wallet movements into the token |
| honeypot dryRun (§5.3a) | ✅ re-pointed: buy→sell-back through Aftermath `trade-route` legs |

**Generic gauntlet** (`generic_screen`, runs instead of §5.3/§5.4 — the risks are
structurally different on a non-launchpad token):
1. **Mint authority**: token package's `TreasuryCap` still owned by a mutable address
   → 🚩 "can print infinite supply" (block by default, chip `allow mintable` off).
2. **Upgrade policy**: package upgradable → owner could add transfer restrictions
   later → 🚩 amber ("can become a honeypot; today's sell test is not forever").
3. **LP/depth**: pool liquidity ≥ `min_generic_liq` (e.g. 200 SUI) + price impact ≤
   cap at order size + top-LP concentration shown ("68% of LP held by 1 wallet ⚠").
4. Live sell-back dryRun (same as launchpad path) — mint/upgrade caps don't excuse
   outright transfer blocks; those get caught here too.

Master toggle `allow non-launchpad tokens` in Risk &
caps (default ON — it's degen mode; OFF = honest-reject card). Caps/kill-switch/fees
identical (integratorFee path, §3.5).

### 6.3c Card & message lifecycle (owner spec: no card clutter)
- The moment the user **commits a buy/sell from a card** (confirm tap): the card, the
  confirm sheet, **and the user's own pasted-CA input message are all deleted**.
- Any degen card that gets no action **auto-deletes after 3 minutes** (unified
  timer for hub/position/order confirm noise; the receipt is the exception — pinned
  until replaced).
- Rationale: stale cards in a fast trading chat = misclicks + leaked positions for
  shared/family devices; one source of truth = the Positions list, not chat history.

### 6.4 Making it friendly (the actual product craft, not vibes)
- **Two power-user text verbs, everything else is taps** (owner spec):
  `Track <wallet>` → registers a copy wallet and replies with its profile card;
  `Snipe <wallet>` → adds it to the deployer-watch list (§3.2). Plain `0x…` alone =
  CA resolve. No other free-text is ever required.
- **Progressive disclosure**: default path is "paste CA → chip → Confirm." TP/SL/DCA/
  sniper/copy/bundle are all OFF unless opened. A degen never sees limit-order jargon
  on the buy screen.
- **Numbers in human units**: SUI in/out, %, "61% to graduation", "$8.9K liquidity" —
  never raw mist/64-bit ints. Big round amount chips.
- **Plain-language honesty**: "conditional order" tooltip = "we watch the price and
  buy for you; it's not sitting on an exchange book." Bundler note: "each wallet's
  buy is its own transaction, so they aren't perfectly simultaneous."
- **Fail-loud + recoverable**: every reject names a fix ("add more SUI", "lower the
  lane size", "raise slippage to 10% to fill"); fills/errors toast to the Hub; kill
  is one big always-visible button.
- **Guided first run**: first `DEGEN` tap = 4-screen coach (connect AI key → set a
  budget → paste a CA → confirm) that ends on a real, tiny, cap-safe first trade.
- **Empty & safety states designed**: no positions → "Paste a CA or try a trending
  grad" + quick chips; caps spent → card disabled with a "come back / raise cap"
  path; kill engaged → Hub shows why + how to reset (never silent).
- **Trust cues on every card**: LIVE/GRADUATED venue badge, honeypot ✅, creator
  holding bar, "you won't be custodied — signed on your device with your key."

### 6.5 Positions & risk screens (the "manage" half of friendliness)
- **Positions list**: per position — value, P&L (colored), venue badge, and a
  `[DCA]`/`[TP-SL]`/`[Sell]` row. One tab to see everything armed.
- **Position manage**: sliders-as-chips for TP %, SL %, and "DCA add on −10/−25/−40%";
  preview reads "if $SYM drops 25%, we auto-add 0.5 SUI (lane 2/3)". Shows aggregate
  basket stop so DCA never quietly breaks §5.1 caps.
- **Orders screen**: every armed rung with trigger price, current distance, and
  `[cancel]`; fired ones move to History. Makes the fast-loop (§3.4) feel controllable.
- **Risk & caps**: budget SUI, per-order, daily-loss, max-open, correlation toggle,
  kill switch — number entry via `+/-` steppers and presets, not free math.
- **Copy screen**: wallet profile cards, `[Follow]` policy chips, notify-first vs
  auto-fire, leaderboard — mechanics in §3.3.
- **Bundle screen**: wallet manager (add/test-sign/gas-chip), per-wallet slot caps,
  default split + orphan policy, basket editor — mechanics in §3.6.

### 6.6 Build note: reuse, don't rebuild
New callbacks (`degen:*`) mount onto the existing router + `_safe_edit` + notifier +
card templates; positions/fills reuse the dashboard's P&L renderers; the ONLY genuinely
new UI primitive is the **CA→Token Card resolver** (§6.3). Ship Hub+Buy+Positions in
P5 behind the AI-key gate; the rest of §6.5 is P5+.

### 6.7 Dashboard layouts (Telegram cards, edit-in-place; `│`-borders = divider rows)
**A) DEGEN HUB** (parent screen — everything one tap deep, live counters so idle
users still glance):
```
🎰 DEGEN MODE  🟢 ON        budget: 3.0 / 12 SUI left today
──────────────────────────────────────────────────
venue: [🐸 Suipump ✓] [💣 Blast 🔒] [⚡ Both]
──────────────────────────────────────────────────
[🎯 Buy a meme]   [📊 Positions (3)]   [📋 Orders (4 armed)]
──────────────────────────────────────────────────
👥 Copy     3 wallets · 🐋 just moved: $FROG→fresh wallet ⚠
🧺 Bundle   4 wallets · 3.0 SUI ready · legs ✓ pre-funded
🪝 Sniper   ON · 2 launches caught · 0 chased (cap left 1)
✨ AI       2 plans armed · next re-plan 00:42
──────────────────────────────────────────────────
              [🛑 KILL ALL DEGEN]
```

**B) WALLET TRACKER** (§3.3 monitor+copy; one card per wallet, `▲▼` live edits):
```
👥 COPY & WATCH            [➕ Watch wallet]  [🏆 Leaderboard]
defaults: 📣 notify-first · size 25% · launchpads: 🐸
──────────────────────────────────────────────────
🐋 0x7ac2…f19  [🐸 main]      ● LIVE · since 08-02
   bal 412.6 SUI ▼2 · $FROG 8.2M · $ZAP 3.1M
   24h: +84 SUI realized · WR 61% · hold 42m · 9 trades
   last moves:
   12:04 bought $FROG 2 SUI  → copied 0.5 SUI ✓ 🟢 +12%
   11:58 sent 5.0M $ZAP → fresh wallet ⚠  [alert fired · exits OFF]
   [rules ✎] [pause] [remove]
──────────────────────────────────────────────────
🐋 0x91ee…04d   👤 profile ready · [View profile]  [Follow]
🪤 deployer 0x3f8…ab  [🪤 arm next launch]  3 past launches · dev-screen 🟡
──────────────────────────────────────────────────
[⚙️ copy defaults]           [← hub]
```
Rules editor (`rules ✎`): chips for size mode / notify-vs-auto / exits / follow-as-
deployer; never a text box for numbers.

**C) POSITION + SMART DCA** (§3.4; this doubles as the DCA lane editor screen):
```
$SUICAT 🐸· 💧 curve 61%→grad · 💧liq $8.9K ✓   [chart▴]
entered 3.20 SUI · avg 3.05 · value 4.90  🟢 +53%
[🎯 TP/SL ✎] [➕ DCA] [🧺 spread] [⚡ Sell 50%] [Sell all]
──────────────────────────────────────────────────
DCA · Standard ladder · budget 2.0 · left 1.5   [🔀 spread lanes ON]
 ● lane1 0.5 @ −10%  fired 12:03 → avg 3.05
 ◐ lane2 0.5 @ −25%  price 1.2% away   [pause] [✎] [✕]
 ○ lane3 1.0 @ −40%  armed             [✎] [✕]
 invalidators: dev sells >0.5% ✓ · vol<0.2/15m ✓ · grad&−20% ✓
 basket stop −50% always · honeypot re-check every lane ✓
──────────────────────────────────────────────────
TP/SL: 🎯 +100% / +2x / 🛑 −50% · [trailing ▾ off] [preset ▾ Standard]
```

**D) BUNDLER** (§3.6; setup + spread-burst in one screen):
```
🧺 BUNDLE   8/20 wallets ✓ · all generated here (no pasted keys) · keys: user row 🔐
budget: [1] [2 ✓] [5] [Max] · split: [⚖️ even ✓] [🎲 custom]
bundle fee: 5.0 SUI → Neko (charged per burst) · funding: [✓ pre-funded] [⏱ JIT]
──────────────────────────────────────────────────
target: [CA 0x…] │ [deployer 0x…] │ [🪤 next launch]
   → $NEWCOIN 🐸 curve 🟢 honeypot ✓ · dev 3% 🟡
──────────────────────────────────────────────────
legs (5.0 = 0.25×20):
   main ✓ 7.1 · w2 ✓ 2.2 · w3 ⚠ low gas [top-up] · w4..w20 ✓
   [+ generate wallets ×5] [sweep idle] [reveal] [delete→main]
──────────────────────────────────────────────────
[💥 SPREAD-BUY ALL]   [🧺 basket ▸ 1 tx, 8 tokens]
orphans: [rebalance-sell ▾] · fee: 5.0 SUI burst → Neko ✓ 0.5% fills
last burst: 20/20 ✓ 5.0 SUI · holders:20 ✓ · 680ms total
```

**E) SNIPER** (§3.2/§3.2a — deployer-watch model):
```
🪝 SNIPER        [ON ✓]  idle — add a wallet to watch
watch list:
   0x91ee…04d   [🔥 auto] 0.5 SUI · spread ✓ · last: $ZAP +142%
   0x3f8…ab21   [👀 ask ] 0.2 SUI · 4 launches fired · WR 60%
   [➕ watch deployer]   (or: type  Snipe 0x…)
──────────────────────────────────────────────────
filters: [dev-screen 🟢 ✓] [min dev buy 0.1 ✓] [first 5s only]
exits: [🎯 attach TP/SL ✓] [bare] · cap today 2.0/3 left · anti-chain: 1/24h per dev
──────────────────────────────────────────────────
12:03:41 ✔ $QUACK by 0x91ee → fired 0.5 · +410ms · 🟢 +22% [open]
12:02:58 🚫 $HON by 0x3f8 — fresh-funded dev            [why?]
6d stats: 14 fired · med lag +520ms · gas 0.031 tot · win rate by dev:
   0x91ee 60% · 0x3f8 30% → [unwatch]
──────────────────────────────────────────────────
⚙️ gas/tx cap [1.0 SUI ▾] · realized avg 0.0026 · coin pool 8/20 ✓ · nodes 4
```
The snipe screen is a **deployer leaderboard you curate**: lag stats, per-dev win
rate, one-tap unwatch. No watchlist = nothing fires (stated in the header).

Shared layout rules: every screen = one editable message; status lines are `● ◐ ○`
glyphs (fired/armed/paused) so state reads at a glance; every 🔒/⚠/🟡 chip has a one
tap `[?]` explainer; the hub's feature rows ARE the mini-dashboards (live counts +
last-event line) so users see value before opening anything.

## 7. Phasing / acceptance gates
- **P0 (safe, read-only)**: Streamer + CurveCreated/Graduated ingestion, both pkgs,
  position/price readers, curve-validator (5.3), dev-screen (5.4) — *no orders*.
  Gate: replay 24h mainnet, zero dup, validator rejects forged curve objects (unit).
- **P1**: manual buy/sell PTB via our signer on **testnet-funded → 1 tiny mainnet**
  with slippage + caps; full tx tracking. Gate: round-trip succeeds, min-out honored,
  DB reflects fills.
- **P2**: Order Engine — conditional orders (pre-grad watcher) + TP/SL, §3.1a state
  machine, post-grad Aftermath swap + resting limits (§3.5) — gate: measure fresh-pool
  route-availability lag + fallback coverage.
- **P3**: Sniper + Copy streamer-side (both consume P0). **P3b**: copy UX — wallet
  profile card, follow-policy chips, notify-first approvals, leaderboard (§3.3).
- **P4**: DCA lane editor + TP/SL chips (§3.4) → Bundler: basket PTB first, fan-out
  second (§3.6 / §8 Q2).
- **P5**: Telegram degen UI (§6): Degen Hub + CA→Token Card→Confirm→Fill money path +
  Positions list, behind the AI-key gate, staged to the 5 owners. **P5b**: Orders,
  Risk&caps, Sniper/Copy/Bundle settings, guided first-run.

### 7.1 Blast go-live runbook (when they ship)
1. §0-probe their contracts (find package via on-chain first txs of their app, or
   their GitHub if published) — buy/sell ABI, curve type, graduation model, admin
   caps, honeypot posture. **Do not wire from their UI's SDK defaults; verify on-chain.**
2. If graduation is NOT cap-gated like Suipump's → treat "be first to graduate" as a
   live exploit vector, decide our stance (never call vs call defensively), document.
3. Implement `Launchpad(blast)` + validator + dev-screen params → `launchpad_adapter_state`
   `probe_done_at`/`live=true` behind a kill-flag; blast tile un-locks → 🔔 pings fire.
4. Gate: same P0 replay bar as Suipump (24h events, zero dup, forged-curve reject).

## 8. Open questions before building
0. **Confirm Telegram-only for degen** (web token page deferred) — matches the repo
   where `service/frontend` is a stale `dist/`. If you DO want a web degen terminal
   (Pump.fun-style paste-CA page), that's a separate build; say so and I'll spec it.
1. **Sui RPC access**: public gRPC/GraphQL only, or do you have a paid/self node /
   Surflux for the latency-critical subscribe? Sniper viability depends on this.
2. **Bundler**: mechanics for both sanctioned forms are now specced (§3.6: fan-out +
   atomic basket). Confirm which users get first — recommend **basket** in P4 (atomic,
   one key, trivially safe) and fan-out P4b (multi-key custody + orphan handling
   needs the extra review). Launch-side fake-distribution stays declined either way.
3. **Post-grad venue** (resolved §3.5): Aftermath swaps+resting limits, reusing
   `service/spot/AftermathSpotAdapter` — keyless, self-custody, `AFTERMATH_FEE_ADDR`
   already configured. Remaining: measure **route availability lag** for a token in
   its first minutes post-grad (drives how often the §3.5a Cetus-direct fallback fires)
   — P2 gate item.
4. **DCA/TP-SL scope** (resolved by §3.4/§3.4a): preset ladders by default, LLM
   *pre-fills* lane shapes behind the AI-key gate. Confirm users see the ✨ AI-suggested
   ladder pre-selected, or as an opt-in chip, on first `[➕ DCA]`.
5. **Confirm Telegram-only UI** (no new web frontend) — see §8.0.
