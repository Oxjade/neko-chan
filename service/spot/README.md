# Aftermath SPOT — integration plan & API reference

Isolated package for spot trading on Aftermath (Sui). Lives beside, not inside,
the perps execution stack (`service/execution/`).

## Why a separate package

| | Perps (`execution/`) | Spot (`spot/`) |
|---|---|---|
| Direction | long + short | **long only** (buy token / sell token) |
| Leverage | 2–20x, liq-safe clamp | none — wallet-balance bounded |
| Margin/collateral | Aftermath account + allocate | none — self-custody wallet |
| Liquidation risk | yes (MMR math) | none |
| Funding | ~0.1%/day | none |
| Entry | CCXT limit/market on the perp book | Router swap (market) or spot limit order (resting) |
| Platform fee | USDC sweep post-fill | **native `integratorFee`** on limit orders; sweep on router swaps |
| Risk guard | margin, liq, exposure | balance cap, slippage cap, min order, one-position-per-token |

## API endpoints used (docs verified 2026-09)

### Router (instant swaps)
- `GET  /api/router/supported-coins` — ~92k tradable coin types, **7.8 MB** → cache 24h
- `POST /api/router/trade-route` — `{coinInType, coinOutType, coinInAmount|coinOutAmount, slippage?}`
- `POST /api/router/transactions/trade` — serialized PTB (undocumented schema; prefer `add-trade` composition per docs)
- Aggregates Aftermath pools + Cetus + FlowX + 7k; user picks best route

### Spot limit orders (resting, on-chain)
- `POST /api/limit-orders/transactions/create-order` — `CreateLimitOrderRequest`:
  `walletAddress, allocateCoinType, allocateCoinAmount ("...n"), buyCoinType,
  expiryDurationMs, outputToInputExchangeRate, isSponsoredTx,
  integratorFee? {feeRecipient, feeBps}, outputToInputStopLossExchangeRate?`
  → bare JSON string = serialized tx (sign + broadcast on Sui)
- `POST /api/limit-orders/active` — signed: `{walletAddress, bytes, signature}`
- `POST /api/limit-orders/past` — unsigned
- `POST /api/limit-orders/cancel` — signed + `orderObjectIds: []`
- `POST /api/limit-orders/min-order-size-usd` — bodyless → USD number

### Signing (reusable terms signature)
Sign the EXACT bytes `b"Aftermath Terms and Conditions"` with the wallet's
Ed25519 key → `{walletAddress, bytes (b64), signature (b64)}`. Order IDs ride
as plain fields, never inside the signed payload.

### Integrator fee (the 0.5%)
- **Limit orders**: `integratorFee: {feeRecipient: AFTERMATH_FEE_ADDR, feeBps: 50}`
  — charged on-chain at fill. Capped per-account by builder-code config.
- **Router swaps**: no integrator field → existing post-fill USDC sweep applies.
- Builder codes: one global `integratorId` registration covers every market.

## Package layout
- `order_model.py` — SpotOrder schema (buy/sell only, quote = USDC)
- `risk.py` — SpotRiskGuard (notional caps, slippage ceiling, per-token uniqueness)
- `adapter.py` — AftermathSpotAdapter (router + limit-order API calls, terms signing)
- `gateway.py` — SpotGateway (risk + routing + fee wiring)

## Remaining build steps (in order)
1. **Address derivation**: reuse the perps SUIAdapter key import (bech32 → seed →
   blake2b-256(0x00‖pubkey)) — shared plumbing, not new crypto
2. **Broadcast step**: sign the serialized tx from `create-order`/`trade` and
   send via the same GraphQL `executeTransaction` the perps adapter uses
3. **Router swap-tx composition**: `transactions/trade` is undocumented — either
   use `add-trade` composition into a minimal PTB or the TS SDK's route object
4. **Symbol → coin-type resolution**: `/api/coins/verified` + metadata, cached
5. **Agent wiring**: spot strategies are long-only — a `spot` trader type with
   its own gates (no shorts, no funding edge), reusing the 4h momentum model
6. **Telegram UX**: separate "Spot" section on the dashboard; honest copy that
   spot = buy-and-hold tokens, no shorting

## Fee reality check (same as perps)
Spot swaps carry DEX fees + price impact on thin Sui books. The 0.5% platform
fee on spot has the identical economics question as perps — success-fee vs
notional-fee applies here too before users can be net-positive.
