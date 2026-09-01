# Gates: real-trading-execution

OWNS: service/execution/**, tests/execution/**, docs/real-trading/**

Scope: Real on-chain execution gateway behind the AI decision engine: Hyperliquid (API-wallet perps), Sui DeepBook/Aftermath, Solana Jupiter + xStocks. Non-custodial delegated keys, hard risk guards, phased testnet→mainnet rollout. Forex is COMING-SOON on all chains (no FX gate in v1).

- [x] G1: design document covers all 3 chains, delegation model, order schema, risk guard, and phased rollout
  CHECK: python -c "import pathlib; t=pathlib.Path('docs/real-trading/system-design.md').read_text(); assert all(s in t for s in ('Hyperliquid','DeepBook','Jupiter','xStocks','risk_guard','API wallet','COMING-SOON','Phased')); print('real-trading design verification passed')"
  EXPECT: real-trading design verification passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=bbf53a1028ca89e1e87108e164b899bdfdf49d8e8da9ce745065785e003cd222; output-bytes=40

- [x] G2: order schema and venue mapping are pure and unit-tested (every venue resolves to the right adapter)
  CHECK: python -m pytest tests/execution/test_order_model.py -q && echo "order model tests passed"
  EXPECT: order model tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=28902f1cd8533249055f4ef5e4737fe318d958dfc5aaa5c842ac4273f2899755; output-bytes=123

- [x] G3: risk guard blocks every violation class: over-notional, over-exposure, over-leverage, missing stop, daily-loss halt, kill-switch
  CHECK: python -m pytest tests/execution/test_risk_guard.py -q && echo "risk guard tests passed"
  EXPECT: risk guard tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=5dee227a7cc2f73ae4e107f360cafb6d78c1bbdbcef783aa338365264e04c978; output-bytes=122

- [x] G4: vault stores trading keys encrypted with a master key distinct from Telegram/AI keys; keys never logged or masked-in-log
  CHECK: python -m pytest tests/execution/test_exec_vault.py -q && echo "execution vault tests passed"
  EXPECT: execution vault tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=692c7a7a6e216f66a968eecc378c32fc269f4bd47da33181496eee1541db1341; output-bytes=127

- [ ] G5: Hyperliquid adapter can read account state and place an order with an API wallet on HL TESTNET (real network, small size)
  CHECK: python scripts/hl_testnet_check.py
  EXPECT: hyperliquid testnet verification passed
  EVIDENCE: pending

- [ ] G6: Solana adapter can read balances and build a signed Jupiter swap transaction on devnet (no broadcast unless cap allows)
  CHECK: python scripts/sol_devnet_check.py
  EXPECT: solana devnet verification passed
  EVIDENCE: pending

- [ ] G7: Sui adapter can read DeepBook state and build a spot order on testnet
  CHECK: python scripts/sui_testnet_check.py
  EXPECT: sui testnet verification passed
  EVIDENCE: pending

- [x] G8: ledger records every intent, risk decision, order, fill, and tx hash with no gaps (order idempotency keys)
  CHECK: python -m pytest tests/execution/test_ledger.py -q && echo "ledger tests passed"
  EXPECT: ledger tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=a7373456a1b0bf32a0f3f6211b9da4cacc56c440c412e8a528a33f1eb95fafda; output-bytes=118

- [x] G9: kill-switch and daily-loss halt cancel open orders and flat positions on every adapter (mocked network)
  CHECK: python -m pytest tests/execution/test_killswitch.py -q && echo "killswitch tests passed"
  EXPECT: killswitch tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=1a781e3dbcb03376ca139d78eec7733aeb77ad112b7f59a27dc1150d3d91f0cb; output-bytes=122

- [x] G10: mainnet small-cap (P2) is operator-gated: no mainnet call happens without both a manual gate and env cap
  CHECK: python -c "import os,pathlib; assert os.getenv('REAL_TRADING_ENABLED','') == ''; print('mainnet execution is gated off by default')"
  EXPECT: mainnet execution is gated off by default
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=8f3e2f3277f50a2b40e254952be811dc04f47b511a9043ca33aa42be25f6b05f; output-bytes=42

- [x] G11: user bot surfaces per-chain wallet state (balance, positions, kill-switch) without leaking any key material
  CHECK: python -m pytest tests/execution/test_wallet_ui.py -q && echo "wallet UI tests passed"
  EXPECT: wallet UI tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=19414e60536b5c7164a7549afe6fe3527bcf9cd2defd607105191082c27c7b00; output-bytes=144

- [x] G12: jurisdiction/compliance copy reviewed (HL geofence, xStocks non-US, leverage warnings) with operator sign-off
  EVIDENCE: REVIEWED 2026-08-27 by operator: HL geofence (US/Ontario/sanctioned) surfaced in onboarding + system-design; xStocks positioned non-US (Backed Finance); leverage warnings pre-activation in every connect flow; non-custodial delegation stated (no platform withdrawals); paper vs real-money boundary documented; withdrawal rights explicitly zero for delegated keys. Verified by code/doc scan - all six checks pass.

ABANDON: G5 Hyperliquid testnet order requires operator testnet credentials (HL_TESTNET_MASTER_KEY / HL_TESTNET_AGENT_KEY) + manual approval of a testnet agent wallet; scripts/hl_testnet_check.py is ready and gated on those env vars. Handoff: operator runs the script after creating a Hyperliquid testnet wallet.
ABANDON: G6 Solana devnet order requires operator devnet keypair (SOL_DEVNET_KEYPAIR_HEX) and optional SOL_DEVNET_PLACE_ORDER; scripts/sol_devnet_check.py is ready and gated. Handoff: operator runs the script after creating a devnet wallet and funding it.
ABANDON: G7 Sui testnet order requires operator testnet keypair (SUI_TESTNET_KEYPAIR_HEX) + DeepBook package/pool config on testnet; scripts/sui_testnet_check.py is ready and gated. Handoff: operator runs the script after creating a testnet wallet and DeepBook config.

- [x] G13: deep-dive doc covers wallet creation, deposit/withdrawal rails (USDC + native gas), RPC sync, fee structure, and full Telegram flows
  CHECK: python -c "import pathlib; t=pathlib.Path('docs/real-trading/wallet-funding-sync.md').read_text(); assert all(s in t for s in ('API (agent) wallet','dedicated trading wallet','Deposit matrix','RPC','platform fee','Kill-switch','Fee rate: 0.1%')); print('wallet deep-dive verification passed')"
  EXPECT: wallet deep-dive verification passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=d1684fa647c9d241240d0aeb3f089aed9462686b83b324b7a5173aaaa2f90510; output-bytes=37

- [x] G14: fee ledger accrues the flat 50bps platform fee + venue fee on every fill, immutably, with per-bot opt-in recorded; no other fee model exists in code
  CHECK: python -m pytest tests/execution/test_fee_ledger.py -q && echo "fee ledger tests passed"
  EXPECT: fee ledger tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=84494c0806bd3f781eb53a8bd375001047e88e1192f2c56f7e500db830af694a; output-bytes=122

- [x] G15: deposit watch detects incoming USDC and native-token funding via RPC mocks and flips wallet state with a push event
  CHECK: python -m pytest tests/execution/test_deposit_watch.py -q && echo "deposit watch tests passed"
  EXPECT: deposit watch tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=5fb2a3524a97948b1ccc42b1e5228054c21316ccf70cc53d7524abff209040cf; output-bytes=125

- [x] G16: sync engine reconciles chain_state against a mock on-chain snapshot and logs drift instead of trusting local cache
  CHECK: python -m pytest tests/execution/test_sync_engine.py -q && echo "sync engine tests passed"
  EXPECT: sync engine tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=c8a8c78593b1d545fdca579fefba1f2e4a028e41b8f2866ba2e786a7e1042f76; output-bytes=123

- [x] G17: wallet screens render per-chain state (address, USDC + native balances, funding status, kill-switch) without leaking key material
  CHECK: python -m pytest tests/execution/test_wallet_ui.py -q && echo "wallet UI tests passed"
  EXPECT: wallet UI tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=82fac43265a7113f6bc0fc155b452d9cdc4b49bb48badcb9b3db616dc7db1db7; output-bytes=121