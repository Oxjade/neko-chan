# Plan: Real-Trading Execution Gateway — Depth Tree

Ledger: `docs/real-trading/GATES.md` · Design: `docs/real-trading/system-design.md`

## Deliverable tree

```
real-trading-execution (G1, G10)
├── leaf-1  core (protocol-agnostic)          → G2, G3, G4, G8, G9
│   ├── order_model.py     intent schema + venue mapping
│   ├── risk_guard.py      hard caps (notional/exposure/lev/stop/loss-halt/kill)
│   ├── exec_vault.py      per-bot trading keys, separate Fernet master
│   ├── ledger.py          idempotent order/fill/tx audit
│   └── killswitch.py      flat + cancel-all on every adapter (mock-tested)
├── leaf-2  hyperliquid adapter               → G5
│   ├── hl_adapter.py      REST/WS, agent-wallet approval + signed orders
│   └── scripts/hl_testnet_check.py
├── leaf-3  solana adapter (Jupiter + xStocks)→ G6
│   ├── sol_adapter.py     Jupiter Swap/LO/Perps, xStocks tokens
│   └── scripts/sol_devnet_check.py
├── leaf-4  sui adapter (DeepBook/Bluefin)    → G7
│   ├── sui_adapter.py     DeepBook spot/margin, Bluefin perps
│   └── scripts/sui_testnet_check.py
└── leaf-5  user bot integration              → G11, G12
    ├── wallet screens (balance/positions/kill-switch per chain)
    └── compliance copy (geofence, non-US, leverage warnings)
```

## Dependencies
- leaf-2/3/4 ← leaf-1 (order model + risk + vault + ledger)
- leaf-5 ← all adapters
- G10 (mainnet gate) is a deployment gate, not a build gate.

## Waves
- Wave 1: leaf-1 (core, fully testable offline)
- Wave 2: leaf-2 (HL testnet) — P2 rollout target
- Wave 3: leaf-3, leaf-4 (parallel; disjoint files)
- Wave 4: leaf-5 (UI + compliance)
- Integration: G1/G10 after each wave; P2 mainnet only after G5 + operator
  approval with $50–500 caps.

## Verification rules
- Unit tests offline (mocked venues) for G2/G3/G4/G8/G9.
- Real-network gates (G5/G6/G7) run against testnets/devnet with tiny size,
  and require operator env vars (never committed).
- Mainnet execution is impossible by default (G10): code asserts
  `REAL_TRADING_ENABLED` unset unless the operator sets it deliberately.
- Manual gate G12 reviewed by the operator before any mainnet capital.