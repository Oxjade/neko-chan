# Neko Sui Market-Data + Wallet-Intelligence Indexer — Design

Status: **DRAFT v0.3 → review/sign-off (decisions locked 2026-09-14, see §18). Phase 1 implemented + verified; Phase 3 adapter framework built + Cetus CLMM verified end-to-end (see §18).**
Target network: Sui mainnet only (phase 1); same pipeline reusable for testnet via config.
Parent requirement: "Sui-native GMGN data engine" for the Neko terminal. Read this doc before any production code.

> Cross-references the original spec §1–§25 (in particular §23 phase order, §24 constraints, §25 deliverables 1–13).
> Convention used below: **VERIFIED** = fact confirmed from official docs/registry/on-chain artifacts during research; **TBD-VERIFY** = identity known but must be confirmed on-chain (via the verification procedure in §6) before any decoder is written, per spec §24.

---

## 1. Verified ground truth (research snapshot, 2026-09)

| Item | Value | Status |
|---|---|---|
| JSON-RPC on Sui mainnet | Deprecated; disabled on mainnet as of 2026-07-27. New Ops/JSON-RPC will be launched outside the explorer/checkpoint-observation saving grace only via gRPC. | VERIFIED |
| gRPC v2 | GA, namespaced `sui.rpc.v2.*`. Services: `LedgerService`, `SubscriptionService`, `StateService`, `MovePackageService`, `TransactionExecutionService`, `ArchivalService` (ArchivalService exposes the same surface as LedgerService for history). | VERIFIED |
| Recommended mainnet endpoint | `fullnode.mainnet.sui.io:443` (public-good, rate-limited, fine for dev/tests; production should use a dedicated fullnode/provider). Note: `grpc.mainnet.sui.io` (older docs) **no longer resolves** as of 2026-09. | VERIFIED (live-tested) |
| Chain metadata headers | `x-sui-chain-id`, `x-sui-chain`, `x-sui-checkpoint-height` on responses; request-side chain-id guard. | VERIFIED |
| Read mask | `read_mask` FieldMask — request only fields you need (bandwidth is the real cost). | VERIFIED |
| Connection limits | Max connection age 4h (graceful GOAWAY → reconnect), 60s unary call timeout, 1024 subscription buffer, 1024 max subscribers, capped event payloads. | VERIFIED |
| SubscribeCheckpoints | Starts at the **latest** tip on connect (no from-checkpoint param in v2 subscribe); gapless, in-order per connection; resume = set checkpoint start on next connection and tolerate overlap via idempotency. | VERIFIED |
| Historical raw checkpoints | `gs://mysten-mainnet-checkpoints-use4` (GCS, requester-pays) and `s3.us-west-2.amazonaws.com/mysten-mainnet-checkpoints` (AWS creds needed). `checkpoints.mainnet.sui.io` HTTPS store only keeps ~30 days → **not** the backfill source. | VERIFIED |
| Fullnode for replay | Start a fullnode from a snapshot to serve historical ranges to our own gRPC; or use ArchivalService from a provider. | VERIFIED |
| DeepBook V3 | Package `0x337f4f4f6567fcd778d5454f27c16c70e2f274cc6377ea6249ddf491482ef497` (v6), registry `0xaf16199a2dff736e9f07a845f23c5da6df6f756eddb631aed9d24a93efc4549d`. Legacy lightweight package `0xdee9`. DEEP coin. Versions table tracks supported coins/pools. | VERIFIED |
| Cetus CLMM | Mainnet package `0x25ebb9a7c50eb17b3fa9c5a30fb8b5ad8f97caaf4928943acbcff7153dfee5e3` (mainnet-v0.0.14 on Move Registry). CLMM + DLMM; pool creation gated by allowed-pair config. MovePump/SUI pool seen: `0xa879da53319bcb59b020c4a818008879412701a3eb1cf3ac891db0fea8426ce3`. | Package VERIFIED; event structs + pool types still need on-chain dump |
| Cetus risk history | May 22 2025 CLMM integer-overflow exploit (~$220M drained, ~$162M recovered via validator freeze). Decoders must use checked math for any fee/price math (§18/§21 spirit). | VERIFIED |
| SuiPump | Permissionless bonding-curve launchpad, mainnet live. Contract set (13 modules): `bonding_curve`, `creator_vault`, `events`, `factory`(TokenRegistry), `graduation`, `launcher`, `trading`, `treasury`(PlatformTreasury), `reputation`, `gated_content`, `creator_registry`, `utils`. Whitepaper mechanics: 1B supply, 6 decimals, virtual reserves **4,369 SUI / 1,073,000,000 tokens**, graduation mint adds 200M tokens, graduation migrates liquidity to Cetus CLMM, creator royalty via `CreatorCap`. Each launch publishes a **byte-identical coin-template package** → per-token package IDs. Platform package mainnet address is in their `deployed.json` only for **testnet** currently: package `0x1a6046b029116bb4c8bf1b3f218ced1ffefd50422ef29c1cd4dab9d65ff46f17`, factory `0x6974dfeb78c1d9fcf6ef02ac96988dc2c966ecc3e5fcb3ec78a4942218bf9cc6`, treasury `0xb6f454435d60e1914c68f5d7493fd1ca0d7c2dc8b6ec297e360f41103d21b9bb` (deployed 2026-05-14). | Testnet VERIFIED; **mainnet platform package TBD-VERIFY** |
| MovePump | `movepump.com` — fair-launch memecoin platform, tokens trade on Cetus pools. | Platform mechanics TBD-VERIFY |
| Kumbaya | Memecoin launchpad, appears in Neko `lpp.json` outDexKey context. | Full identity TBD-VERIFY |
| Turbos / Aftermath / Bluefin | Spot DEXes on Sui (TurboS swap package, Aftermath af, Bluefin perps/spot). | Package IDs TBD-VERIFY on-chain |

Ongoing executions: existing Neko `service/execution/sui_adapter.py` currently talks JSON-RPC → **must be migrated to gRPC v2** before mainnet date friendliness; the indexer's gRPC client code is a shared, tested component we can reuse for that migration.

---

## 2. Deliverables 1–13 (this document maps them to sections)

1. Architecture diagram → §3
2. Data-flow diagram → §4
3. Exact Sui gRPC APIs used → §5
4. Historical source strategy → §7
5. Protocols to index (initial set) → §8
6. Package IDs / modules / event types table + verification procedure → §9
7. PostgreSQL schema → §10
8. Redis key design → §11
9. Checkpoint / recovery / idempotency → §12
10. Live-latency strategy → §13
11. Scaling → §14
12. Single-VPS estimate → §15
13. Implementation / dev-order plan (§23 order) → §16

---

## 3. Architecture

```mermaid
flowchart LR
    subgraph Live
        S1[Sui gRPC v2<br/>SubscriptionService]
    end
    subgraph Hist
        A1[ArchivalService / own fullnode]
        A2[GCS checkpoint archive<br/>gs://mysten-mainnet-checkpoints-use4]
    end

    S1 --> P[Producer<br/>SubscribeCheckpoints]
    A1 --> B[Backfill driver<br/>ListCheckpoints + GetTransaction]
    A2 --> B

    P --> CHP[Checkpoint raw store<br/>(append-only, seq-keyed)]
    B --> CHP

    CHP --> DCP[Checkpoint Processor<br/>diff objects/events<br/>seq-gap detection]
    DCP --> DEC[Adapter classifier + Move decoder<br/>GetDatatype struct layouts, cached]
    DEC --> NORM[Normalizer<br/>token/swap/curve/holder canonical model]
    NORM --> CAND[Candle engine<br/>OHLCV buckets 1s/5m/15m/1h/24h]

    CAND --> RED[Redis livestate<br/>prices, curves, trends, watermark]
    NORM --> RED

    RED --> WS[WebSocket fan-out<br/>TypeScript API + Redis pubsub]
    RED --> API[REST API]

    NORM --> PGW[PG writer workers<br/>async, batched]
    CAND --> PGW
    PGW --> PG[(PostgreSQL)]

    PG --> WPG[Wallet PnL engine]
    PG --> WL[Smart-money scoring]
    PG --> TR[Trending engine]
    WPG --> API
    WL --> API
    TR --> RED

    KEEP[Retention/pruning<br/>configurable, per table] --> PG
    FW[Producer watermark + metrics] --> ME[Tracing / metrics]
```

Components:
- **Producer** — long-lived gRPC stream (`SubscribeCheckpoints`). Handles GOAWAY (4h) reconnect, 60s unary timeout elsewhere, chain-id guard. Writes raw checkpoints + cursor.
- **Backfill driver** — same decode pipeline, fed from archival or GCS/own-fullnode; never touches the live path's cursor.
- **Checkpoint Processor** — turns `CheckpointData` + `CheckpointSummary` into a stream of transaction effects + emitted events + created/mutated object versions; computes monotonic seq and detects gaps for backfill.
- **Adapter classifier + Move decoder** — decides which protocol a tx/event belongs to by package/module (known IDs), then BCS-decodes Move struct bytes. Struct layouts pulled from `MovePackageService.GetDatatype` (on-chain truth), cached in PG+Redis (`MoveStruct` layouts). No hand-typed structs that aren't validated against real fixtures (§24: don't invent event names).
- **Normalizer** — canonical models: tokens (metadata), pools, swaps (in/out amounts in smallest units, computed price), bonding-curve states, holder deltas; all math fixed-point (no floats).
- **Candle engine** — rolling OHLCV from swap stream, maintained in Redis for hot routing and flushed to PG.
- **PG writer workers** — async, batched, idempotent upserts; each table has a natural-key unique constraint so replay is safe.
- **Wallet intelligence** — wallet tx ledger, balances, holders, realized/unrealized PnL, smart-money heuristics. Read-heavy from PG; snapshot tables updated incrementally.
- **API/WS** — TypeScript REST + WebSocket layer that serves the Neko terminal frontend; reads Redis hot state + PG derived state (Redis pubsub for fan-out).
- **Ops** — metrics per stage (chain ts → grpc recv → decoded → redis → ws), producer watermark, DLQ table for undecodable lines.

---

## 4. Data flow

```mermaid
sequenceDiagram
    participant S as Sui gRPC
    participant P as Producer
    participant D as Decoder(s)
    participant R as Redis
    participant G as PG writers
    participant W as WS/API

    loop live
        S->>P: SubscribeCheckpoints stream (gapless, in-order)
        P-->>G: append raw checkpoint + seq cursor
        P->>D: checkpoint
        D->>D: classify by package/module; decode Move events (GetDatatype layouts)
        D->>D: normalize swaps/pools/curves/holders (fixed-point)
        D->>R: price/curve/trend sets + checkpoint watermark (fast path)
        par
            D-->>G: batched upserts (idempotent)
            D-->>G: candles
            R-->>W: pubsub push
        end
    end

    loop occasional
        S-->>P: GOAWAY at 4h → reconnect (start at persisted cursor, dedupe overlap)
    end

    Note over P,G: Overlap replay is safe: unique(natural keys) + cursor watermark.
```

Hot path rule (spec: single VPS, low latency): `decode → Redis → WS` happens on stream before the DB write is durable; PG writers acknowledge the watermark asynchronously. A crash replays from the last **persisted** cursor; Redis state is rebuilt from PG + raw checkpoint replay, never trusted as source of truth.

---

## 5. Exact Sui gRPC v2 API surface used

All on `fullnode.mainnet.sui.io:443` (TLS); official pre-generated client/proto crate `sui-rpc` (crates.io, current 0.4.x; proto source vendored in-crate).

| Need | Service / method | Notes |
|---|---|---|
| Live checkpoint stream | `SubscriptionService.SubscribeCheckpoints` | Starts at latest on connect; gapless/in-order per conn; use to chase live tip. Manual reconnect with persisted cursor to resume. Expect GOAWAY every ≤4h. |
| Historical checkpoint ranges | `LedgerService.ListCheckpoints` (paged); `ArchivalService.ListCheckpoints` (same surface, history store) | Backfill loop with `checkpoint` cursor paging. |
| Checkpoint payload | `LedgerService.GetCheckpoints` with `read_mask` | Pull full checkpoint data for the range being backfilled. |
| Transactions & effects from a checkpoint | `LedgerService.GetTransaction` / `BatchGetTransactions` (v2), via checkpoint's tx digests + FieldMask | Prefer `ListEvents` when we only need filtered event types; fall back to transaction when we need `changedObjects` for holder deltas. |
| Events by type | `LedgerService.ListEvents` with `event_type` filter + cursor | ⚠️ **Not served on the public-good endpoint (measured 2026-09-14, §18)** → deep backfill re-scans raw checkpoints with an `event_type`-prefix filter; keep cursor semantics for when a dedicated fullnode is available. |
| Event-filtered checkpoint scan | `LedgerService.ListCheckpoints` + `TransactionFilter`/`EmitModule` predicate (server-side prune) | **Verified 2026-09-14:** a 60k-checkpoint window collapsed to only the 4 event-bearing checkpoints via `mod:<pkg>::<module>`; feeds `neko-verify backfill-scan/backfill-fixtures`. Prunes to event-bearing frames only — the sparse-event discovery path now. |
| Object reads (balances/holders/liquidity) | `StateService.GetObject` / `GetBalance` / `GetOwnedObjects` | For non-streamed facts: current pool reserves, wallet balances, curve state at a point in time. |
| Move struct layouts (on-chain truth) | `MovePackageService.GetPackage` + `GetDatatype` | Retrieve module list + `MoveStruct` layout per type → cache → BCS decode events generically. This is the "don't invent event names" mechanism. |
| Coin metadata | `CoinService` (coin metadata lookup) when available; else `0x2::coin::CoinMetadata` objects via StateService. | |

Decoding strategy (two tiers, one truth):
1. **Adapted/known types** — a compiled spec per verified event type (e.g. Cetus `SwapEvent`, DeepBook `PoolCreated`, SuiPump bonding ops) built from an on-chain `GetDatatype` layout dump, frozen as a tagged fixture. Fast path.
2. **Generic fallback** — any unknown event under a recognized package/module is BCS-decoded from a fresh `GetDatatype` layout (cached), tagged, and surfaced through the DLQ/review pipeline so new protocol versions get adapted without silent data loss.

Verification-before-use (spec §24): every decoder (fast or generic) must pass **parity**: decode N real on-chain transactions/events captured as fixtures; assert our canonical swaps/candles/balances reconstruct to the recorded `CheckpointSummary`/object versions.

---

## 6. Verification procedure for any new address/type (TBD-VERIFY rows)

Until this passes for a target, its adapter is **not** enabled:
1. Obtain candidate package id (docs, SDK mainnet config, explorer, Move Registry).
2. `MovePackageService.GetPackage` → confirm modules exist on mainnet; pull module list.
3. `GetDatatype` per candidate type → dump `MoveStruct` layout; record module:type names verbatim.
4. Confirm emission on mainnet and gather ≥3 distinct real transactions as fixtures by scanning raw checkpoints for the type. 2026-09-14: `ListEvents` is not served on the public-good endpoint (§18), so use `ledger_checkpoints` backfill with either a `mod:<pkg>::<module>` server-side filter (`neko-verify backfill-fixtures`, fast — frames pruned to event-bearing checkpoints) or an `event_type`-prefix filter (`neko-verify backfill-scan` / `neko-verify db-scan <define-id-prefix>` then `fixtures`).
5. Resolve the chain of identities (token coin type `<pkg>::<module>::<coin>`; pool object type; factory/registry objects like DeepBook `0xaf16...` or SuiPump `TokenRegistry` and `PlatformTreasury`; per-token curve/graduation objects).
6. Save fixture bundle to `tests/fixtures/*` (raw BCS + decoded layout + expected canonical decodes) and freeze.
7. Only then: write decoder + tests, then enable in config `protocols.adaptive.active`.

---

## 7. Historical backfill strategy

- **Sources, in preference order:**
  1. Own fullnode restored from snapshot serving our internal ArchivalService (single-writer, we control it; recommended by Sui for indexers of full history). Fullnode needs a full checkpoint store (careful: not the ~30-day trimmed store).
  2. Provider ArchivalService for targeted ranges or event-filtered deep history.
  3. Bulk archive from GCS `gs://mysten-mainnet-checkpoints-use4` (requester-pays) / S3 `mysten-mainnet-checkpoints` (AWS creds) — used for the initial deep backfill import into our raw store.
- **Storage reality (measured 2026-09):** full-mask raw checkpoints are ~860 KB each (≈15 GB/day live). Backfilling ALL of history as full raw is infeasible on a VPS (~277 TB @ 322M checkpoints). Therefore **deep history is event-filtered** (per-adapter re-scan of raw checkpoints with an `event_type` filter; `ListEvents` unavailable on the public-good endpoint — §18) from a start checkpoint — complete for the events that matter (launchpad + DEX swaps + CoinMetadata), not a full ledger. Full raw is kept only for a **bounded recent window** (configurable, default 30 days) to bound size while preserving audit/parity headroom. Prune-after-normalize (§18) applies to the derived tables' raw inputs.
- **Raw store:** SEQ + DIGEST are the only unique keys. Checkpoints appended in seq order; a gap table is maintained. We do not re-fetch checkpoints we already hold unless a gap is flagged.
- **Ordering decoupled from live:** backfill writes into the *same* normalized tables (idempotent upserts) but does not fan out to WS and does not move the live watermark. Historical completeness is measured as: `min/max seq persisted` × `coverage %` vs checkpoint range, exposed as a metric and a status endpoint.
- **Retention (spec: "retention should be configurable"):** raw checkpoint store and raw event rows keep full raw (needed for re-derivation and audit); derived tables (swaps/holders/candles) prune to a configurable window (default: keep all for `tokens`/`pools`/`bonding_curves`; time-series derived tables default 6 months, configurable; wallet PnL snapshots default 3 months).

Historical completeness contract (spec §"Historical completeness"): the indexer can answer "tokens created since X", "first swap", "graduation date", "creator wallet", "holders at T" for any T ≥ backfill start, using **backfill-from-checkpoints**, not explorer scraping.

---

## 8. Protocols to index (initial set) + statuses/token model

Model: tokens move through lifecycle states driven by on-chain events (spec "pre-bond / bonding / trade / graduation"):

```
NEW (coin template package published / treasury created)
  → PRE-BOND (curve object created, not yet tradable)
  → BONDING (trading live on bonding curve; price on virtual reserves)
  → GRADUATED (liquidity migrated to DEX: Cetus CLMM pool created; now trades on AMM)
  → (optional) REMOVED / FROZEN (pair removed, honeypot flag)
```

| Platform | Role in pipeline | Status |
|---|---|---|
| SuiPump | bonding-curve launchpad; graduation → Cetus CLMM | **v1 target**; ✅ mainnet platform VERIFIED 2026-09-14 (§9); trade mapping proven on 40-event fixture |
| MovePump | fair-launch memecoin platform; liquidity on Cetus | **v1 target**; TBD-VERIFY |
| Kumbaya | memecoin launchpad | **v1 target**; TBD-VERIFY |
| Cetus CLMM | AMM for graduated tokens (`0x25ebb...`) + all non-launchpad memecoins | **v1 target**; package VERIFIED; event/pool struct dump pending |
| DeepBook V3 | order-book DEX (spot) | NOT v1 (later) |
| Turbos / Aftermath / Bluefin | spot DEX coverage | NOT v1 (later) |

Basic **coin discovery** is protocol-agnostic: watch package-publish transactions whose modules mint a `TreasuryCap`/`CoinMetadata` + `metadata` creation, plus send/transfer of `0x2::coin::Coin` quantities, and (for launchpads) the factory `TokenRegistry`/curve-creation events. This is what feeds `tokens` even for completely unknown launchers.

Decisive questions for review below (§"Ask Me"): whether v1 includes DeepBook/Turbos/Aftermath (spot only) or launchpads-first.

---

## 9. Package IDs / modules / event types (frozen + pending)

Legend: ✅ = VERIFIED (safe to build adapter fixture on), ⏳ = TBD-VERIFY (use §6 procedure before enabling).

### SuiPump (testnet identity verified; **mainnet VERIFIED 2026-09-14**)
- Testnet platform/registry/treasury (repo `suipump-xyz/suipump-contracts` `deployed.json`, 2026-05-14, Sui 1.71.1, edition 2024.beta): ✅ `0x1a6046b029116bb4c8bf1b3f218ced1ffefd50422ef29c1cd4dab9d65ff46f17` / `0x6974dfeb78c1d9fcf6ef02ac96988dc2c966ecc3e5fcb3ec78a4942218bf9cc6` (TokenRegistry factory) / `0xb6f454435d60e1914c68f5d7493fd1ca0d7c2dc8b6ec297e360f41103d21b9bb` (PlatformTreasury). ⚠️ **Not on mainnet** — testnet identity only.
- **Mainnet V16 platform (defining id, stable across upgrades):** ✅ `0x7b4163d17ce18b386ee50929ba48fa0a2ecb60304df4b07e26835aa18617cda2` — verified on-chain via `GetPackage` (storage_id==original_id, version 1; 3 modules: `bonding_curve`, `agent_session`, `enclave_registry`). Source: live `suipump.org` app bundle env config + on-chain confirmation.
- Mainnet upgrade chain (verified): `VITE_SUIPUMP_V16_WRITE_PACKAGE` `0xb1ad998007f93af6b29bd60b345bb7ed7fd6b3e7381ca08c88051e615b174975` (version 2, original = above), `VITE_SUIPUMP_V17_PACKAGE` `0xb205fea41ccedac051bc66498e6ca68cb802c4a6ea06da12e524bed09c80d9b0` (version 1), `VITE_SUIPUMP_V17_WRITE_PACKAGE` `0x93d5b3d2b3dda384b9cfe5987cd423ec77580973a6d2f1cd3711b292aee14203` (version 3, original = V17). Registry/partner ids from the bundle: ENCLAVE `0x20d43e60…6794`, GRADUATION_REGISTRY `0x2b81ccba…08fc`, LAUNCH_ISSUER_REGISTRY `0x1a81374a…dae5`, CETUS_CLMM_PARTNER `0x40fbafb1…7870`, CETUS_DLMM_PARTNER `0xef9fa949…d259`, DEX_FEE_RECEIVER `0x4179b33a…4d35c`, PRICE_CONFIG `0xa5b38690…21f9`.
- **Real event types verified on mainnet** (`bonding_curve` module, via EventFilter-less checkpoint scan): `TokensPurchased`, `TokensSold`, `CurveCreated`, `Graduated`, `PoolRecorded`, `BuybackExecuted`, `CreatorFeesClaimed`, `ProtocolFeesClaimed`, `PayoutsUpdated`, `Comment` (+ agent_session lifecycle events per module dump). 40 real events frozen in `indexer/tests/fixtures/suipump/events.json`; offline BCS decode == node `Event.json` for all 40.
- Per-token model: each launch publishes a **byte-identical coin-template package** → per-token package id `<tokenPkg>` with token type `<tokenPkg>::template::TEMPLATE`. This is why token discovery keys off package-publish + factory events, not a fixed coin type.
- Curve constants (whitepaper, verify on-chain): supply 1,000,000,000 (6 dp); virtual reserves 4,369 SUI / 1,073,000,000 tokens; graduation adds 200,000,000 tokens; graduation migrates to Cetus CLMM. `grad_threshold_used` sampled live = 9,000,000,000,000 (9e12 raw) — matches the graduated curve spend floor in events.
- Bonding trade fields (verify on-chain): buy = `sui_in`/`tokens_out` (+ fees: airdrop/creator/lp/protocol/referral/tail_refund; post state `new_sui_reserve`/`new_token_reserve`); sell = `sui_out`/`tokens_in`. Wallet = `buyer`/`seller`; curve id = `curve_id`. Trade sides map 1:1 to `bonding_trades.side`.

### Cetus CLMM
- ✅ **VERIFIED 2026-09-14** (§6 procedure). Package v14: storage_id `0x25ebb9a7c50eb17b3fa9c5a30fb8b5ad8f97caaf4928943acbcff7153dfee5e3`, original_id `0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb`, 13 modules.
- ✅ `0x…def::pool::SwapEvent` layout captured (12 fields / positions 0..11, see §18); real mainnet events live-decoded with parity `ok: true` and recorded as offline fixtures (`neko-verify cetus-fixtures`).
- Mainnet package: ✅ `0x25ebb9a7c50eb17b3fa9c5a30fb8b5ad8f97caaf4928943acbcff7153dfee5e3` (mainnet-v0.0.14)
- MovePump/SUI pool (reference pool): ✅ `0xa879da53319bcb59b020c4a818008879412701a3eb1cf3ac891db0fea8426ce3`
- Pool discovery: allowed-pair config + `create_pool` events; pool objects carry immutable `PoolConfig`/tick state. ⏳ exact `pool.move`/`router.move` event type names + `Pool` struct layout → dump via §6.

### DeepBook V3
- Package (v6): ✅ `0x337f4f4f6567fcd778d5454f27c16c70e2f274cc6377ea6249ddf491482ef497`
- Registry: ✅ `0xaf16199a2dff736e9f07a845f23c5da6df6f756eddb631aed9d24a93efc4549d`
- Legacy package `0xdee9` path discontinued for new code. Versions table enumerates supported coins/pools per version; use registry to enumerate pools. ⏳ event struct names from v6 (`orderbook` events) → dump.

### TBD-VERIFY (blocked adapters — no decoder until procedure passes)
- Turbos swap package, Aftermath package, Bluefin spot package, MovePump platform package + mechanics, Kumbaya package + mechanics, SuiPump mainnet addresses, Cetus/DeepBook event struct names.

---

## 10. PostgreSQL schema (physical v0)

Money/price columns are `numeric` (fixed-point, exact) in the **smallest unit of the denom** (e.g. token raw units, SUI `1e9` mis), or with an explicit `scale` column where mixed. No `float8` for financial math (spec §24). Prices to USD use one quote path (SUI), value via `orm`-fed SUI/USD oracle (see §11).

Core:

- `tokens(coin_type text PK, symbol, name, decimals smallint, metadata_object_id, treasury_object_id, package_id, module_name, template_pkg_id, creator_wallet, launchpad text, created_checkpoint bigint, created_at timestamptz, status token_status, rise_data jsonb)` + `UNIQUE(coin_type)`, inspect indexes on `status`, `launchpad`, `created_at`.
- `token_meta_raw(coin_type text, checkpoint bigint, raw jsonb, UNIQUE(coin_type, checkpoint))` — metadata snapshots/updates.
- `pools(pool_object_id text PK, dex text, module text, pool_type text, coin_a, coin_b, fee_tier numeric, graduated_from text, created_checkpoint bigint, status text)`.
- `swaps(tx_digest text, event_index int, dex text, pool_id text, coin_in text, amount_in numeric, coin_out text, amount_out numeric, fee numeric, price_q numeric, price_quote numeric, sender text PK?, ts timestamptz, raw jsonb)` — **natural key** `UNIQUE(tx_digest, event_index)`; primary for replay safety; index on `(coin_in, ts)`, `(coin_out, ts)`, `(pool_id, ts)`, `(sender, ts)`.
- `pairs_dex(pair_id)` and derived `pair_stats(pair, window, vol, …)` for listing feeds.
- `bonding_curves(token text PK, platform text, curve_status, virtual_sui numeric, virtual_tokens numeric, created_checkpoint, graduated_checkpoint, reserve_sui numeric, supply_sui numeric, mcap_sui numeric, graph_id)` → PB; indexes on `(platform, curve_status)`.
- `bonding_trades(token, ts, side, amount_token, amount_sui, amount_quote, supply, reserve, mcap, price, tx)+(tx, event_index)`.
- `candles(token text, interval text, bucket timestamptz, o numeric, h numeric, l numeric, c numeric, vol_base numeric, vol_quote numeric, n_trades int, PRIMARY KEY (token, interval, bucket))`.
- `wallets(wallet text PK, first_seen, last_seen, tags jsonb, is_bot boolean, metadata jsonb)`.
- `wallet_balances(wallet text, coin_type text, balance numeric, updated_at, PRIMARY KEY(wallet, coin_type))`.
- `holder_snapshots(token text, ts timestamptz, holder_count int, top10_pct numeric, met access jsonb, PRIMARY KEY(token, ts))`.
- `wallet_pnl(wallet text, coin_type text, period text, realized numeric, unrealized numeric, roi numeric, PRIMARY KEY(wallet, coin_type, period))` + flow: `wall ET txs ledger wallet_tx(wallet, ts, tx, action, token, amount, quote, counterparty, UNIQUE(wallet, tx, action))`.
- `smart_wallets(wallet text PK, score numeric, model jsonb, updated_at)`.
- `checkpoint_progress(name text PK, last_seq bigint, watermark_ts, ts, next_weight bigint)` — single-writer cursors (live + per backfill lane).
- `raw_checkpoints(seq bigint PK, digest, data bytea, ts)` + `raw_events(seq bigint, event_index int, tx_digest, event_type text, bcs bytea, raw jsonb, PK(seq, event_index))` — optional raw retention (configurable, default on for re-derivation/audit).
- `dlq(id bigserial PK, source, seq, event_type, payload jsonb, reason text, ts)` — undecodable lines never silently dropped (spec §24).
- `sui_oracle(ts, price_usd numeric, source text)`.

Index strategy: writing time-series tables with low-cardinality buckets + natural-key unique constraints; `swaps` partitioned by month in production; `candles` PK is a great partition key. WAL level + archive for PG is out of scope on the single VPS; PG serves the API and wallet engine; Redis serves the hot live path.

---

## 11. Redis key design (hot live path)

Namespace prefixes (`nx:`); Lua for atomic ops; TTLs so nothing lingers forever.

| Key | Type | Purpose | TTL |
|---|---|---|---|
| `nx:price:{coin_type}:last` | hash | `{price_q, price_usd, ts, seq}` | 1d |
| `nx:price:{coin_type}:hist` | zset (score=ts) | rolling prices (for charts) | 6h |
| `nx:candle:{interval}:{coin_type}:{slot}` | hash | hot OHLCV bucket being built | 2× interval |
| `nx:curve:{token}` | hash | live bond state (reserve, supply, mcap, status) | 1d |
| `nx:trend:{window}:{coin_type}` | zset | volume/momentum buckets 5m/15m/1h/24h | window×2 |
| `nx:top:{window}` | zset | ranked coins by volume/gainers/losers | window×2 |
| `nx:graduations` | stream/list | just-graduated tokens feed | 1d |
| `nx:new-pairs` | stream | pool/curve creation feed | 1d |
| `nx:tx:{coin_type}` | stream | per-token trade feed → WS queue | 1h |
| `nx:ws:{channel}` | pubsub | fan-out to WS servers | n/a |
| `nx:cfg:coins` | hash | watchlist/ignorelist | 1d |
| `nx:watermark:live` | string | last processed checkpoint seq (produced) | 1d |
| `nx:watermark:persisted` | string | last durably written seq | 1d |
| `nx:oracle:sui-usd` | string | SUI/USD price | 60s |
| `nx:struct:{module}:{type}` | string | cached `MoveStruct` layout json | 7d |
| `nx:dedupe:{...}` | set | fast-path dedupe of processed `(tx,event)` | 1d (PG unique is source of truth) |

Redis Streams are the queue abstraction between live decode and PG writers (spec scaling → later replace with a broker). Partition by `hash(coin_type || chain)` for sharded workers later.

---

## 12. Checkpoint / recovery / idempotency

- **Cursor:** `checkpoint_progress.live` written every checkpoint (cheap) and fsync-acked on a timer (default 500ms); on start, Producer resumes from the last persisted seq rather than "latest + wait", replaying any gap (dedupe makes overlap safe).
- **Idempotency is structural, not trust-based:** natural-key unique constraints (`swaps(tx_digest,event_index)`, `bonding_trades(...)`, `candles PK`, `tokens(coin_type)`, `pools(pool_object_id)`, holder snapshots `(token,ts)`...). Writers use `INSERT ... ON CONFLICT DO NOTHING`/`UPDATE` semantics that make replay converge.
- **Out-of-order:** the stream is in-order per connection; any seq detected lower than watermark implies duplicate (skip) or gap (backfill trigger). Gap handling: `raw_checkpoints` gap table + a repair worker that pulls the missing seq range from archival; normalized tables are only written in seq order per writer partition, so candles/price never see regression.
- **Crash:** Redis is rebuilt from PG (candles/curves replay from raw or normalized), raw store is source of truth. Watermark lag is the health metric: `live_watermark_seq - persisted_seq`, alerted if > 2 min.
- **GOAWAY (4h):** Producer loops: reconnect → start from persisted cursor → first pass marks processed seqs → fast-forward to live tip. Stream subscription max age handled in a connection wrapper.

---

## 13. Live-latency strategy

Target: **price/curve/trade feed < 500 ms p99** from checkpoint tip to WS client on the same VPS (Sui chain → gRPC → decode → Redis → WS), measured per stage (trace spans: `receive`, `decode`, `normalize`, `redis`, `ws`).

- `SubscribeCheckpoints` gives tx effects as soon as finalized; producer does **no DB writes** in the hot path — only Redis + WS broadcast. PG writers drain asynchronously with an in-memory window (default 5 s / 2k rows).
- FieldMask discipline on `read_mask` to cut payload; only requested event types fetched from checkpoints (`ListEvents` filter when available, else digest → `BatchGetTransactions`).
- Lua-atomic Redis price/trend updates; pubsub fan-out; WS server sends prices keyed by `nx:top:*` subscriptions.
- Watermark publish lets clients compute staleness (`x-sui-checkpoint-height` + checkpoint seq in every WS message).
- Degrade: if Redis is down, PG path still catches up; WS clients get backlog from `nx:tx:*` replays / raw store replay to watermark.

---

## 14. Scaling

- **V0 (single VPS):** Producer + decoders + PG + Redis + API/WS on one host (§15). Everything async I/O; backfill driver paused/held low-priority during live peaks (config throttles concurrent backfill decode).
- **V1 split (this repo structure supports it):** `producer → Redis Stream partitions (hash(coin)) → decode workers → PG writers`, API/WS stateless behind LB; PG to managed or two-node (one R/W, one replica for wallet-engine reads).
- **V2:** fan-out WS nodes; add dedicated fullnode sidecar for archival ranges; shard PG by token shard if needed.
- All components already communicate via Redis + PG only (no cross-process shared state), so the split is config + deployment, not rewrites.

---

## 15. Single-VPS estimate (v0)

Assumptions: Sui mainnet checkpoint cadence ~1 per second (order of seconds; spikes at epochs), filtered event stream after our adapters ≈ hundreds of events/s at memecoin churn, candles + swaps dominate PG writes; raw store kept for audit.

| Resource | v0 estimate | Rationale |
|---|---|---|
| CPU | 4 vCPU (burst ≥ 3.0 GHz) | Producer ~0.5 core; decode/normalize ~1–1.5; PG ~1; API/WS ~1 |
| RAM | 8 GB | PG shared_buffers 2 GB, Redis ≤ 1 GB (TTL-bounded), Rust workers ≤ 2 GB |
| Disk | 100 GB NVMe SSD, extendable | PG time-series (pruned) + raw checkpoints (audit window) |
| Bandwidth | 2–5 Mbps avg, bursts | gRPC checkpoint fetch + WS live fan-out |
| Provider | any (Hetzner/DigitalOcean/etc.) | nothing provider-specific; monitor current endpoint |

Budget hint: single AMD/Intel 4-vCPU + 8 GB + 100 GB is comfortably within $20–35/mo equivalents; no Kubernetes anywhere (spec).

---

## 16. Implementation plan (§23 order, each phase ships tests + gates)

1. **gRPC checkpoint stream + cursor** — grpc channel wrapper (TLS, headers, reconnect-on-GOAWAY), `SubscribeCheckpoints` → raw store + `checkpoint_progress`. Gate: streams 10k live checkpoints with zero gap; reconnect test kils stream mid-run.
2. **Historical backfill harness** — archival/GCS/own-fullnode import, same decode path, progress + coverage metrics, throttling; `backfill verify` = full snapshot parity against provider for a sampled range.
3. **Adapter framework + generic Move decoder** — `GetDatatype` layout cache, BCS decode lib, fixture runner; DLQ; parity tests on real mainnet txs. Gate: decode any unknown event under known package without crash; parity on N=100 real txs.
4. **Normalization core** — canonical token/swap/curve/holder models, fixed-point math, price/candle engine. Gate: property tests (no float), roundtrip decode = effect.
5. **Redis livestate** — price/trend/curve keys + watermark; gate: redis state equals PG after crash-replay do.
6. **PostgreSQL pipeline** — batched idempotent writers, unique constraints, pruning; gate: replay 2× produces identical DB.
7. **REST + WebSocket API** — expose tokens/pairs/swaps/candles/curves/wallets (+ `nx:*` streams UNSS). Align with Neko terminal routes (`frontend/`).
8. **Wallet intelligence v0** — wallet ledger, balances, holders via `StateService` reads + event deltas; index `(wallet, ts)`.
9. **Launchpad / pre-bond / bonding adapters** — SuiPump (✓ procedure), MovePump, Kumbaya; lifecycle + graduation linkage history; gate: indexed a real newly-launched token end-to-end from publish → curve → graduation with on-chain backing txs.
10. **Wallet PnL engine + smart-money scoring v0** — realized/unrealized from swaps+candles+oracle; heuristics (entry timing, repeat winners, kin-wallets, rugs avoided). Gate: PnL reconciles against manual wallet analysis on 20 sampled wallets.
11. **Trending discovery** — momentum windows, gainers/losers, new-pairs feeds; tie to Neko terminal feeds. Gate: output matches manual top-N inspection on a live day.
12. **Production hardening** — metrics/tracing, alerting (watermark lag, DLQ rate, gap count), soak test 72h on VPS, runbook, PG vacuum tuning, raw retention policy enforcement.
13. **Existing execution adapter migration note** — `service/execution/sui_adapter.py` off JSON-RPC onto the shared gRPC client (post JSON-RPC mainnet shutdown; not part of indexer scope but shares the client code).

---

## 17. Security / correctness notes (from spec §24 §10 §21)

- **Money math:** `numeric`/`decimal` only; overflow hardened (lesson: Cetus CLMM 2025 integer-overflow exploit). All bps/fee math validated by property tests.
- **No invented types:** every struct decoded from on-chain layout or a frozen fixture verified on-chain; `GetDatatype` is source of truth.
- **No explorer scraping** — everything from gRPC/raw checkpoints.
- **Idempotency + audit:** natural-key unique constraints everywhere; DLQ table for anything undecodable; raw store kept for re-derivation.
- **Config:** `protocols.adaptive.active` gates which adapters run; **nothing adaptive is silent** — new package/type flips a flag and lands in a review queue, not production data.

---

## 18. Decisions (locked 2026-09-14) + open items

Locked:
1. **Language:** Rust for the indexer core (producer/decoder/normalize/writers using the official published `sui-rpc` crate — proto types + resumable stream clients), TypeScript for the API/WebSocket layer that serves the Neko terminal. Repo Python remains for `service/` execution gateway only.
2. **Live endpoint:** public-good `fullnode.mainnet.sui.io:443` for phase-1 dev; production keeps a dedicated fullnode/provider. (Decision was "public-good OK".)
3. **Raw retention:** prune-after-normalize. Raw kept for a bounded window only (default 30 days; configurable) + event-filtered deep history; see §7 for the measured cost signal (~15 GB/day full-mask).
4. **execution adapter migration:** in-scope and now — `service/execution/sui_adapter.py` moves off JSON-RPC onto the same gRPC v2 client pattern (before mainnet shutdown deadline).
5. **Protocol priority: locked — Launchpads + Cetus.** v1 adapters: SuiPump, MovePump, Kumbaya (bonding curves) + Cetus CLMM (graduated liquidity). DeepBook/Turbos/Aftermath/Bluefin remain TBD-VERIFY rows, not in v1.

Phase-1 status (2026-09-14): live `SubscribeCheckpoints` intake verified against mainnet —
tip ≈ seq 322,673,242, ~1.1 ckpt/s; idempotent raw persistence + same-tx cursor; restart
resumes at cursor+1 with zero gaps/duplicates. Reference: `.unlazy/neko-indexer/GATES.md`.
Implementation notes from phase 1 (proto/SDK gotchas) recorded in that ledger.

Phase-3 status (2026-09-14): adapter framework (type-tag parser, strict BCS reader, on-chain
layout resolver, generic decoder, checkpoint event capture, offline fixture replay) built
and **Cetus CLMM verified end-to-end on mainnet**:

1. **`ListEvents` is not served on the public-good endpoint** (measured: filterless descending
   scan → 40K+ watermark frames / 0 events; bounded seq window 322673340..322673400 → 2073
   frames / 0 events). Do not size the backfill design (§7, §5 row 3) around `ListEvents`.
   Authoritative capture path is `Checkpoint.transactions[].events.events[]` — full `Event`
   protos (BCS `contents` + the node's own JSON mirror) already persisted in `raw_checkpoints`.
2. **Event identity vs emit package:** `Event.package_id` is the *upgraded* storage id
   (measured `0x25ebb9…e5e3`), while the emitted type string embeds the *defining* (original)
   id (`0x1eabed72…2fb`, = `GetPackage.original_id`). The defining id is the stable identity
   across upgrades → adapters classify on `event_type` prefix, not `package_id`.
3. **Cetus v14 facts (recorded 2026-09-14 via `neko-verify package`):** storage_id
   `0x25ebb9a7c50eb17b3fa9c5a30fb8b5ad8f97caaf4928943acbcff7153dfee5e3`,
   original_id `0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb`,
   13 modules / pool=27 datatypes. `0x…def::pool::SwapEvent` on-chain layout (12 fields):
   `atob:bool, pool:ID, partner:ID, amount_in:u64, amount_out:u64, ref_amount:u64,
   fee_amount:u64, vault_a_amount:u64, vault_b_amount:u64, before_sqrt_price:u128,
   after_sqrt_price:u128, steps:u64` (positions 0..11).
4. **Canonical JSON shape (locked):** all money ints (`u64/u128/u256`) decode to decimal
   **strings** (matches the node's JSON, precision-safe, no floats); single-field
   `{bytes: address}` wrappers (`0x2::object::ID`) decode to the address string; bools and
   small uints are JSON numbers; `numeric_equal` is a canonical equivalence for parity
   (`neko-verify cetus-swaps` live parity `ok: true` ×12; offline fixture replay
   `cetus_swap_fixture_replay` passes network-free — fixtures under
   `indexer/tests/fixtures/cetus/swaps.json`, regenerate via `neko-verify cetus-fixtures`).
5. **Adapter verification loop (frozen):** discover real event types from raw checkpoints
   (`neko-verify db-scan`), capture fixtures (`neko-verify fixtures`), then the decoder must
   pass the network-free fixture replay BEFORE it is wired into normalization.
6. **G3 (adapter framework + Cetus proof) met:** `cargo test --lib` 12/12; clean bin build.
   Next up: MovePump/SuiPump/Kumbaya event discovery + first adapter in `adapt/`, then
   backfill harness + Redis phase 5.
7. **SuiPump mainnet VERIFIED 2026-09-14 (§6 procedure):** live-app bundle env vars extracted
   (`suipump.org/assets/index-*.js`) + `GetPackage` confirmed: V16 defining id
   `0x7b4163d1…cda2` (3 modules: `bonding_curve`, `agent_session`, `enclave_registry`),
   upgrade chain V16-write/V17/V17-write verified. Real events discovered by scanning
   checkpoints with a server-side **`EmitModule` transaction filter** (new tool
   `neko-verify backfill-scan/backfill-fixtures`): a 60k-checkpoint window pruned to the 4
   event-bearing frames; 40 events (10 layout types) frozen in
   `indexer/tests/fixtures/suipump/events.json`; offline replay == node JSON for all 40
   (`suipump_events_fixture_replay`). `TokensPurchased/Sold` map 1:1 to
   `bonding_trades(side, token_amount, sui_amount, fees, reserves, curve_id)`.
8. **G4 (map-events→canonical) mapping layer built:** `adapt/normalize.rs` — venue from the
   defining-id prefix, exact decimal-string money, zero arithmetic/floats, no invented sides.
   Proven on BOTH fixtures (`cargo test --lib` 17/17): every SuiPump buy/sell and Cetus swap
   maps; SuiPump fee/comment/payout/buyback events pass through as raw only.
9. **Node JSON conventions now decoder-truth:** empty Move `vector` → `null`, `Option<T>` →
   `null`/bare value, `0x1::string::String` → UTF-8 text (all matched the node's `Event.json`
   during SuiPump parity).