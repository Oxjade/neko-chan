# Neko — Real Trading: Wallet, Funding, Sync & Fee Design (Deep Dive)

Version: 1.0 (DESIGN ONLY)
Companion to `docs/real-trading/system-design.md`. This document goes layer by
layer: how wallets are born, how money gets in and out, how the data stays in
sync, how we charge, and how the whole thing lives inside the Telegram bot.

---

## 1. WALLET CREATION & CUSTODY (per chain)

### 1.1 Hyperliquid — API (agent) wallet
```
1. User opens "Connect Hyperliquid" in their bot → we generate an agent keypair
   (secp256k1/ECDSA via eth-style key, viem-compatible).
2. We show the user an approval link/instruction: sign a Hyperliquid
   "Approve Agent" tx from their MASTER wallet (app.hyperliquid.xyz/API).
   Agent wallet name = "neko_<bot_id> valid_until <expiry>" (expiry defaults
   90 days, renewable).
3. We verify approval via RPC (agentAddress visible under master's agents).
4. Agent private key → exec_vault (Fernet, EXEC master key), never logged.
```
- **Permission scope**: trade + cancel only. **Withdrawal: structurally
  impossible** (Hyperliquid forbids agent withdrawal) — strongest guarantee.
- **Funding**: user deposits USDC into their master Hyperliquid account from
  their own wallet (exchange-side USDC), then our agent trades that account.
- **Kill-switch**: we stop signing (client-side) + user can revoke the agent
  wallet on Hyperliquid directly (server-side truth).

### 1.2 Sui — dedicated trading wallet
```
1. "Connect Sui" → generate Sui address (Ed25519 keypair, address = blake2b
   of pubkey + scheme flag). Private key → exec_vault.
2. Show address + QR in the bot. User sends USDC (Sui-native) AND a small SUI
   gas buffer (≥ 0.1 SUI) to it.
3. We detect first incoming tx via Sui fullnode RPC (getCoins) → wallet
   "funded" state.
```
- Gas is paid in SUI; trades settle in USDC. DeepBook orders are on-chain txs.
- **Withdrawal**: user-signed transfer (we prepare the PTB, user approves in
  their wallet, or a signed "sweep to <main>" tx is prepared on demand).

### 1.3 Solana — dedicated SOL wallet
```
1. "Connect Solana" → generate Ed25519 keypair. Address = base58 pubkey.
   Private key → exec_vault.
2. Show address + QR. User sends USDC (SPL, mint EPjFWdd…) and SOL gas
   (≥ 0.01 SOL) to it.
3. Detect funding via Solana RPC (getTokenAccountsByOwner + lamports).
```
- Jupiter swap/perps = Solana txs signed by this wallet; priority fees in SOL.
- **Withdrawal**: user-signed transfer of USDC/SOL back to a main address;
  sweep-all available.

### 1.4 Common rules (all chains)
- **One trading wallet per bot per chain** (idempotent: reconnect returns the
  same address + key).
- Keys: Fernet-encrypted with `TG_EXEC_MASTER_KEY` (distinct from
  `TG_VAULT_MASTER_KEY`). Never in logs; masked `0x…a3f1` style.
- Rotation: "Rotate key" re-generates (HL requires re-approval; Sui/SOL just
  swap the key at the same address — Sui/SOL address IS the key's pubkey, so
  rotation = new address + re-fund, kept explicit).
- Addresses are public data — displayed freely in the bot.

---

## 2. DEPOSIT & WITHDRAWAL RAILS

### 2.1 Deposit matrix
| Chain | Asset | How | Confirm via | Min | Gas needs |
|---|---|---|---|---|---|
| Hyperliquid | USDC | deposit USDC on Hyperliquid from user's own wallet/bridge | HL RPC `userFills`/`userNonFundingLedgerUpdates` + `spotUserState` | $10 | 0 (HL gasless) |
| Sui | USDC (Sui) | direct transfer to trading address | `getCoins` (USDC coin type) | $5 | 0.1 SUI |
| Sui | SUI (native) | direct transfer | `getCoins` (SUI) | 0.5 SUI | — |
| Solana | USDC (SPL) | transfer to trading address | `getTokenAccountsByOwner` | $5 | 0.01 SOL |
| Solana | SOL (native) | transfer | `getBalance` | 0.05 SOL | — |

- **Native chain token (SUI/SOL) is REQUIRED as gas** even if trading USDC
  pairs; the bot warns "add 0.1 SUI for gas" if below buffer.
- Deposit detection loop (per wallet): poll every 30s; first confirmed
  funding flips wallet state → `funded` → Telegram push "💰 Deposit received
  +$50 USDC — wallet active".

### 2.2 Withdrawal (user-signed, on demand)
- HL: user withdraws from their own Hyperliquid account (we provide link +
  amounts). Agent cannot touch funds.
- Sui/SOL: user taps "Withdraw" → we build the transfer tx (trading wallet →
  user's main address, full balance minus gas) → user confirms the tx payload
  in their own wallet UI or approves an on-demand signed transfer with their
  main wallet. We never broadcast without the user's explicit action.

### 2.3 Address watch
- `watch_engine`: per bot × chain, subscribes/polls for:
  incoming transfers (funding), outgoing (withdrawals), order fills,
  position changes, funding payments (HL), liquidations.
- Sources: HL WS + REST, Sui fullnode (query_events / getCoins), Solana RPC
  (signature/account subscription or polling).

---

## 3. RPC & DATA SYNC ARCHITECTURE

### 3.1 RPC layer per chain
| Chain | Endpoint(s) | Purpose | Fallback |
|---|---|---|---|
| Hyperliquid | `api.hyperliquid.xyz/info` (POST JSON), WS `wss://api.hyperliquid.xyz/ws` | prices, candles, order book, account state, fills, funding | 2nd mirror (`api.hyperliquid.xyz` retry + cooldown, existing price_fetcher pattern) |
| Sui | public fullnode RPC (e.g. `https://fullnode.mainnet.sui.io:443`) via `@mysten/sui` client | getCoins, dryRun, executeTransactionBlock, query_events | configurable 2nd RPC via env |
| Solana | public RPC (e.g. `https://api.mainnet-beta.solana.com`) + Jupiter REST APIs | getBalance, token accounts, recent signatures, Jupiter quote/order | configurable dedicated RPC (Helius/Triton) via env |

### 3.2 Sync model (event + poll hybrid)
```
┌─ sync_engine ─────────────────────────────────────────────────┐
│  for each bot × chain:                                       │
│   ├─ poll 30s:  balances (USDC + native)        → chain_state │
│   ├─ poll 30s:  positions (HL perp / DB / Jup)  → chain_state │
│   ├─ poll 30s:  open orders                    → chain_state │
│   ├─ ws/poll:   fills/ledger updates           → ledger rows  │
│   ├─ poll 30s:  incoming txs (funding detect)  → deposit evt  │
│   └─ reconcile: on-chain snapshot vs local ledger → drift log │
└───────────────────────────────────────────────────────────────┘
```
- **Source of truth = on-chain**. Local DB (`chain_state`) is a cache with
  `synced_at`, `tx_hash` anchors; any discrepancy → re-pull + log drift.
- Every fill event updates: chain_state (position/balance), ledger (order→fill
  with tx hash), fee_ledger (venue fee + our fee), Telegram push.
- Cadence configurable per chain; HL uses WS pushes for fills (instant), Sui/
  Solana poll (30s) because they're slower.

### 3.3 Ledger schema (per bot × chain)
| Table | Key fields |
|---|---|
| `exec_wallets` | bot_id, chain, address, pubkey, key_enc, status(funded/active/revoked), created_at |
| `exec_deposits` | wallet_id, asset, amount, tx_hash, status(pending/confirmed), confirmed_at |
| `exec_orders` | idempotency_key, bot_id, chain, venue, symbol, side, qty, price, type, status, venue_order_id |
| `exec_fills` | order_id, price, qty, fee_venue, fee_usd, tx_hash, ts |
| `exec_positions` | bot_id, chain, symbol, side, qty, entry, leverage, liq, stop, take, synced_at |
| `fee_ledger` | bot_id, fill_id, fee_bps, fee_usd, kind(venue|platform), ts |
| `chain_state` | wallet_id, balances_json, positions_json, orders_json, synced_at |

---

## 4. FEE STRUCTURE (our own, per trade)

**Design: THE fee model is a flat 0.5% platform fee on every executed fill.
No other fee model exists (no performance fee, no tiers, no subscription).**
Venue fees (HL/DeepBook/Jupiter taker or maker) are passed through
transparently and shown separately.

```
total_fee = venue_fee (HL/DeepBook/Jupiter taker or maker) + platform_fee

platform_fee = fill_notional_usd × PLATFORM_FEE_BPS  (50 bps = 0.5%, fixed)
```
- Charged **on the user's proceeds at fill time** in the ledger (not on-chain —
  we're not custodying; the fee is a subscription-like debit against the
  user's trading wallet balance at settlement: ledger marks
  `fee_status = accrued`).
- Settlement options (v1): fee accrued in ledger, paid by keeping balance in
  the trading wallet; operator can sweep platform fees at end of cycle from
  trading wallets via user-authorized standing agreement — **needs explicit
  user opt-in at connect time** ("authorize platform fee deduction 0.1%/trade").
- Config: `PLATFORM_FEE_BPS = 50` (fixed; 0 allowed only in paper mode).
- Display: every fill push shows `fee $0.42 (venue) + $1.17 (platform)`.
- Fee ledger rows are immutable; daily fee summary in the digest.

**Why not on-chain fee tx per trade:** doubles gas + complexity on Sui/Solana;
accrual + sweep keeps it simple and auditable. HL gasless could do per-trade
transfer later.

---

## 5. TELEGRAM BOT INTEGRATION (user flows)

### 5.1 New "Wallet" button on dashboard
```
💼 Wallet — Neko Real Trading
🔗 Hyperliquid  · 0x1a3f…c9d2 · 🟢 ACTIVE
    USDC balance   $412.50
    Open positions 2 (BTC perp 5x, ETH perp)
[🔗 Connect HL]  [💳 Deposit]  [↻ Sync]

🔗 Solana  · 8xZq…kL2p · 🟡 FUNDING NEEDED (no gas SOL)
    USDC $0 · SOL 0.0001 (need 0.01 for gas)
[💳 Deposit]  [↻ Sync]

🔗 Sui  · 0x7b…e441 · ⚪ NOT CONNECTED
[🔗 Connect Sui]

💱 Forex on all chains — COMING SOON 🔜
```

### 5.2 Connect flow (per chain, wizard-style)
```
HL:  "1️⃣ We created your agent key. Approve it here (link) → 2️⃣ tap Done"
     → verify approval → "✅ Hyperliquid connected. Deposit USDC to trade."
SOL: "1️⃣ Generate wallet? [✅ Generate] → 2️⃣ address+QR shown → send USDC+SOL
      → 3️⃣ tap 'I sent it' → watch engine confirms → '✅ Wallet funded, active'"
SUI: same as SOL (USDC + 0.1 SUI gas).
```
- Every connect shows: risk copy (geofence, non-custodial, no withdrawals by
  us, leverage warning) with [✅ I understand] gate.

### 5.3 Deposit screen
```
💳 Deposit — Hyperliquid
  Deposit USDC on Hyperliquid from your wallet (min $10).
  [🔗 Open Hyperliquid]  [↻ Check]
  Status: ⏳ waiting for first deposit… (we auto-detect)
💳 Deposit — Solana trading wallet
  Address: 8xZq…kL2p  (USDC)  +  SOL for gas (0.01)
  [📋 Copy address]  [✅ I sent it]  [↻ Check]
  Status: 💰 +$50 USDC received 2m ago ✅ ACTIVE
```

### 5.4 Kill-switch
```
🛑 EMERGENCY — visible on dashboard + every wallet screen
  [⏸ Freeze Bot] → cancels open orders + flat positions on ALL chains + halts
```
Also `/freeze` command. Unfreeze requires 2-step confirm.

### 5.5 Push notifications for real trading
```
✅ FILL (HL): BUY BTC-PERP 0.01 @ $78,500 (5x) · fees $0.16+$0.39 · tx 0xabc…
💳 Deposit: +$50 USDC on Solana — wallet active ✅
🛑 STOP HIT (Jupiter): SOL-USDC closed -$4.10 · fees …
🛑 KILL-SWITCH ENGAGED: all positions flattened (reason: daily loss -3%)
```

### 5.6 Fees shown everywhere
- Fill push: `fees: venue $0.16 + platform $0.39 (0.1%)`
- Daily digest: `platform fees today $2.31`
- Settings: `💰 Fee rate: 0.1% per trade (you authorized at connect)`

---

## 6. SEQUENCE: full lifecycle example (Solana)

```
1. User taps Connect Solana → wallet generated (key → exec_vault)
2. Bot shows address/QR → user sends 0.02 SOL + $100 USDC
3. watch_engine detects tx (30s) → deposit row → push "💰 +$100 USDC — active"
4. AI agent intent: {sol, jup-perp, SOL, buy, 0.5, lev 3, stop 140, take 170}
5. risk_guard: notional $75 ≤ $500 cap ✓, exposure ≤30% ✓, lev 3 ≤ 3 ✓, stop ✓
6. sol_adapter: Jupiter quote → signed tx (trading key) → broadcast → tx hash
7. WS/poll: fill event → ledger row + chain_state update + fee_ledger
8. Push: "✅ FILL (Jupiter): BUY SOL 0.5 @ $149.2 (3x) · fees $0.11+$0.07"
9. Kill-switch anytime: cancel + flat; user withdraws via signed transfer
```

---

## 7. TRADE-OFFS (this deep dive)

| Decision | Chosen | Trade-off |
|---|---|---|
| Platform fee = flat 0.5%/trade (only model) | one number, simple to display and audit | high for churny strategies (2% round trip break-even); agent cadence and targets must respect it |
| Fee as accrual + sweep | simple, auditable, no per-trade gas | needs user opt-in at connect; sweep step |
| One wallet per bot per chain | clean isolation | more addresses to manage; key rotation = new address (Sui/SOL) |
| Poll+WS hybrid sync | HL instant, others 30s | Sui/SOL PnL lags ≤30s |
| Public RPC + env fallback | zero-cost start | dedicated RPC recommended at scale (env swap) |
| Gas buffer enforcement | native token required as buffer | user friction ("why do I need SUI/SOL?") — explained in copy |

## 8. REVISIT
- Per-trade on-chain fee payment (HL gasless) once volume justifies
- Hardware-wallet signing for withdrawals
- Auto gas-refill from USDC (swap-to-gas sweeper) to remove friction
- Cross-chain netting of platform fees