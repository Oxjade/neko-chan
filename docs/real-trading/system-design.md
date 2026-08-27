# Neko — Multi-Chain Real Trading: System Design

Version: 1.0 (DESIGN ONLY — no implementation yet)
Status: DRAFT for review. Real money is involved; every section below assumes
review before any mainnet code.

---

## 0. Scope & Guardrails

The existing AI-Trader platform is **paper trading**. This design adds a
**REAL, ON-CHAIN execution layer** behind the same AI decision engine, across
three chains:

| Chain | Venue | Markets | Status |
|---|---|---|---|
| **Hyperliquid** | Hyperliquid perp DEX | crypto perps (BTC/ETH/SOL…, up to 50x), FX/indices perps | ✅ available |
| **Sui** | DeepBookV3 (spot + margin ≤10x); Bluefin (true perps) | SUI/USDC spot, margin, crypto perps | ✅ available |
| **Solana** | Jupiter (Swap, Limit/DCA, Perps) + **xStocks** (AAPLx, NVDAx, TSLAx, SPYx…) | spot tokens, perps ≤100x, tokenized US stocks | ✅ available |
| **Forex** | — | FX pairs on every chain | 🔜 **COMING-SOON** (per owner) |

**Hard rules:**
1. **Real money = real risk.** Every chain ships in this order: read-only →
   testnet → mainnet with a small cap → full cap after N verified trades.
2. **Non-custodial by default.** The operator never holds user funds. Users
   delegate *limited* trading authority per chain (no withdrawal rights).
3. **Risk guards are client-side AND can never be bypassed by the model**:
   per-trade notional cap, max open exposure, hard stop-loss pre-attached,
   kill-switch per bot.
4. **Forex is COMING-SOON on every chain** — no FX integration in v1.

---

## 1. Requirements

### Functional
| ID | Requirement |
|----|-------------|
| F1 | User chooses a chain in their bot (Telegram) and connects their wallet |
| F2 | Per-chain delegated authority: Hyperliquid API wallet, Sui dedicated trading wallet, Solana dedicated funded wallet / Jupiter keyless agent |
| F3 | The AI agent executes the SAME decisions (trend/sentiment loop) but orders route to the real venue |
| F4 | On-chain positions/PnL reflected in the Telegram dashboard in real time |
| F5 | Stops/targets: venue-native where possible (Jupiter TP/SL, HL trigger orders), else client-side close loop |
| F6 | Withdrawals/transfers of funds back to user wallet (user-signed, on demand) |
| F7 | Kill-switch: user can revoke delegated authority instantly per chain |
| F8 | Audit ledger: every order, fill, and balance change recorded (chain tx + local) |

### Non-functional
| ID | Requirement |
|----|-------------|
| N1 | No private keys of user main wallets ever stored; trading keys encrypted in vault, per-bot |
| N2 | Zero withdrawal capability for delegated keys (HL agent wallets enforce; Sui/Solana use dedicated hot wallets with small funding only) |
| N3 | Failure behavior: if venue unreachable, bot goes risk-off (no orders), never blind |
| N4 | Latency: decision → signed tx ≤ venue SLA; retries with idempotency keys |
| N5 | Compliance flags: HL geofenced jurisdictions, xStocks non-US, leverage warnings shown pre-activation |
| N6 | Cost: gas on Sui/Solana tiny; HL gasless |

---

## 2. Protocol Research Summary (verified 2026-08)

### 2.1 Hyperliquid (crypto + FX/indices perps)
- Non-custodial on-chain order book, one-block finality; up to 50x leverage.
- **API wallets (agent wallets)** are THE automation primitive: permissioned
  signers, **no withdrawal rights**, approved by master wallet, revocable,
  per-agent nonce isolation. Proven in production by Senpi/Dexly-style agents.
- Order types: limit/market, trigger (stop) orders, TWAP. Zero gas.
- ⚠️ Geofenced for US/Ontario/sanctioned; MAS lists it unlicensed — surfaced in
  onboarding, user confirms jurisdiction eligibility.

### 2.2 Sui — DeepBookV3 (+ Bluefin for true perps)
- **DeepBookV3**: on-chain CLOB for **spot** and **margin** (≤10x, isolated
  pools, real-time liquidation). Shared objects: Balance Manager, Pool
  Registry, per-pair pools. Settles <400ms. TS SDK (`@mysten/sui` +
  DeepBook client ext), Rust SDK also exists. $17B+ cumulative volume, audited.
- **Bluefin** (Sui): the perp CLOB for Sui (orders off-chain, settlement
  on-chain, ~510ms; v3 ~400ms). Use for true perps; DeepBook margin for
  leveraged spot.
- Automation: dedicated Sui trading wallet (created by us, user funds it), or
  zkLogin later. Gas ~negligible.

### 2.3 Solana — Jupiter + xStocks
- **Jupiter Developer API**: Swap (`/order` managed, `/build` raw), **LO & DCA**
  (limit orders, OCO/OTOCO = native TP/SL), **Perps** (on-chain program,
  trader↔JLP, oracle-priced, ≤100x, TP/SL orders native, two-tx request/keeper
  model). API key or keyless mode; MCP/CLI/Skills for AI agents.
- **xStocks** (Backed Finance): Token-2022 SPL tokens 1:1 backed by regulated
  custody (Maerki Baumann, InCore, Alpaca); 715+ assets (AAPLx, NVDAx, TSLAx,
  SPYx, QQQx…). Traded on Raydium, routed by Jupiter. Stock perps exist on
  Pionex-style venues; on-chain stock perps venue = **Jupiter Perps if listed /
  BulletX-class venues** (research item during build).
- Automation: dedicated Solana trading wallet (keypair generated + encrypted);
  user funds it; Jupiter orders signed by that wallet.

---

## 3. High-Level Architecture

```
[Telegram bots (Neko master + user bots)]          [AI-Trader paper platform (unchanged)]
        │                                                    │
        ▼                                                    ▼
┌──────────────────────  EXECUTION GATEWAY (new)  ──────────────────────┐
│  chain/                one adapter per venue, one order model          │
│  ├─ hl_adapter.py      Hyperliquid REST+WS  (agent wallet)            │
│  ├─ sui_adapter.py     DeepBook/Bluefin via Sui TS/RPC (trading wallet)│
│  └─ sol_adapter.py     Jupiter API + xStocks (SOL trading wallet)     │
│                                                                       │
│  execution.py          intent → venue order: market/limit/stop/TP      │
│  risk_guard.py         HARD caps: notional/position/exposure/lev      │
│  vault.py              per-bot trading keys (Fernet, separate master) │
│  ledger.py             order/fill/tx audit rows (SQLite + on-chain)   │
│  balance.py            wallet balances + on-chain positions → UI      │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ user-signed only
              [User's wallets: HL master / Sui wallet / SOL wallet]
```

**Decision loop (unchanged brain, new hands):**
1. AI agent produces intent: `{symbol, side, qty, leverage, stop, target}`
2. `execution.py` maps intent → venue order via the chain adapter
3. `risk_guard.py` checks caps BEFORE signing (never model-overridable)
4. Order signed (trading key) → submitted → confirmed on-chain
5. `ledger.py` records; `balance.py` updates; Telegram push fires

---

## 4. Wallet & Delegation Model (per chain)

| Chain | What user does | What we hold | Withdrawals |
|---|---|---|---|
| Hyperliquid | Approve an **API wallet** (agent wallet) for their account (master wallet signs approval) | agent-wallet private key (encrypted) | ❌ impossible (HL forbids) |
| Sui | Fund a **dedicated trading wallet** we generate (user sends SUI/USDC to it) | trading wallet key (encrypted) | user-signed transfer UI |
| Solana | Fund a **dedicated SOL wallet** we generate (user sends SOL/USDC) | trading wallet keypair (encrypted) | user-signed transfer UI |

- User's main keys NEVER leave their custody.
- Every delegated key: stored Fernet-encrypted with a **separate master key**
  from the Telegram/AI keys; rotated on demand; kill-switch revokes usage
  (HL: revoke agent; Sui/SOL: we simply stop signing + transfer balance back).

---

## 5. Order Model & Risk Guard

**One intent schema** across venues:
```json
{
  "chain": "hyperliquid|sui|solana",
  "venue": "hl-perp|deepbook-spot|deepbook-margin|bluefin-perp|jup-perp|xstocks",
  "symbol": "BTC|SUI|AAPLx",
  "side": "buy|sell",
  "qty": 0.01,
  "order_type": "market|limit|stop|take_profit",
  "leverage": 5,
  "stop_loss": 75400,
  "take_profit": 84000
}
```

**risk_guard.py (hard, pre-sign, per bot):**
- Max notional per order (e.g. $500 v1 mainnet)
- Max total open exposure % of wallet balance (e.g. 30%)
- Max leverage per venue (HL 5x v1, DeepBook 2x, Jupiter 3x)
- Stops mandatory on every leveraged open (venue-native or client loop)
- Daily loss limit: if realized PnL day < -X% → auto-flat + halt
- Kill-switch (user or operator) → cancel open orders + flat

---

## 6. Phased Rollout (safety)

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0** | Read-only: balances + positions read from all 3 chains into dashboard | data correct vs block explorer |
| **P1** | Testnet orders (HL testnet, Sui testnet, Solana devnet) | 20 test orders settle |
| **P2** | Mainnet SMALL: $50–$500 caps, 1 chain (Hyperliquid) | 50 trades, 0 risk-guard bypass |
| **P3** | Mainnet: Solana (Jupiter + xStocks), Sui (DeepBook spot/margin) | 100 trades/chain |
| **P4** | True perps everywhere (Bluefin, HL leveraged stocks/FX) | telemetry parity |
| **P5** | Forex across chains | 🔜 COMING-SOON (owner decision) |

---

## 7. Trade-offs (explicit)

| Decision | Chosen | Trade-off |
|---|---|---|
| Custody | Non-custodial, delegated keys | Safer, but user funds a new wallet = onboarding friction |
| Per-chain adapters vs aggregator middleware | Native adapters | More code, no middleware lock-in/risk |
| Venue-native stops vs client loop | Prefer native (Jupiter OCO, HL trigger); client loop fallback | Native = reliable, client = extra latency |
| xStocks perps | P2: spot only; perp venue researched in build | Perp liquidity for stocks is fragmented (Pionex-class venues) |
| Forex | COMING-SOON | Liquidity/venue research pending per chain |

---

## 8. What to revisit as it grows
- Multi-chain aggregated portfolio + margin cross-collateral
- Hardware-wallet-backed signing (better than hot trading wallets)
- Jurisdiction/regulatory matrix per user (HL geofence, xStocks non-US)
- Withdrawal automation with hardware approval flow
- Insurance fund / HLP-style protections monitoring

---

## 9. Build plan summary
See `PLAN.md`. Order: execution.py + risk_guard (protocol-agnostic core) →
hl_adapter (P2 target) → balance/ledger → sol_adapter → sui_adapter → UI.
Gates in `GATES.md`.

## 10. Deep-dive companion
`docs/real-trading/wallet-funding-sync.md` covers, in full detail:
wallet creation & custody per chain (API/agent wallets, dedicated trading
wallets), deposit/withdrawal rails (USDC + native-chain gas), RPC & sync
architecture (event+poll, on-chain as source of truth), the platform fee
structure (0.1% per trade on top of venue fees, accrual + sweep), and the
complete Telegram bot flows (connect wizard, deposit screens, kill-switch,
fee display).