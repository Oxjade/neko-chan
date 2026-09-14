# service/degen — Sui degen mode (Suipump mainnet)

P0 degen engine behind one runtime factory. Dark by default: nothing imports this
package's services unless the userbot mounts it with `DEGEN_ENABLED=1`.

Design source of truth: `docs/degen-mode.md` (v6, verified mainnet).

## Module map

| Module | Responsibility |
|---|---|
| `constants.py` | Mainnet package/object IDs, decimals, fee constants (0.5% exit, flat 5 SUI burst, gas budgets) |
| `chain.py` | Sui GraphQL client (`graphql.mainnet.sui.io`) + Blockvision `suix_getCoins` fallback; `Balance` scalar; coin pre-split |
| `db.py` | `DegenLedger` — SQLite: config, orders, fills, positions, watches, bundle_wallet (20 slots), `ui_ref`, msg_lifecycle, deletes |
| `launchpad.py` | `resolve_input` for every CA form (0x/digest/id) → `AssetState`; `SuipumpLaunchpad`/`BlastLaunchpad` adapters (Blast = COMING_SOON) |
| `metrics.py` | Keyless price / market-cap / graduation fit (Fraction-exact curve math) |
| `validate.py` | `curve_validator` hot-path forged-curve check; `dev_screen` (§5.4); honeypot dry-run (§5.3a, needs funded sender, fails safe to "untested") |
| `ptb.py` | Byte-correct Sui `TransactionData` builders: buy / sell / fee leg / split-fund (address padding 0x20) |
| `executor.py` | `DegenExecutor` — cap/kill/daily-loss gates, buy (curve-only, min-out floor, real fill via balance delta), sell (exit fee on SUI leg), `spread_burst` (5 SUI fee → `NEKO_FEE_WALLET` once, first-leg abort), buy_post_grad (honest pending) |
| `orders.py` | `OrderEvaluator` — plan → rungs, graduated → venue flip ordering |
| `sniper.py` | Deployer-watch sniper — cooldown/auto-unwatch, curve re-validation + dev-screen at fire time |
| `bundle.py` | `BundleManager` — own wallets only (≤20), keys via `exec_vault.generate_key_material("sui")`, Fernet per slot |
| `streamer.py` | `EventBus` + `SuipumpStreamer` — GraphQL polling 2s, cursor-resumed, dedup (gRPC streaming = P1) |
| `ui.py` | Telegram-first surface: hub / venue tile / TokenCard / sheet / 3-min lifecycle / callback-data via `ui_ref` (≤64B) |
| `runtime.py` | `DegenRuntime` singleton — wiring + env gate; **mainnet-only** (perps testnet never leaks in) |

## Runtime input / env keys

- `DEGEN_ENABLED=1` — unmounts the feature (dark default).
- `DEGEN_LEDGER_PATH` — SQLite path (default `degen_ledger.db`).
- `NEKO_FEE_ADDR` — fee recipient (`executor` fee_recipient; also on `spread_burst`).
- `$DEGEN_SENDER` — funded main wallet for the honeypot dry-run (see P1).
- Main wallet keys never touch this package: the userbot injects
  `runtime.set_wallet_adapter_factory(factory)` (a) for the bot's main wallet via
  `ExecGateway.adapter_for_bot(bot_id, "sui", "mainnet")`, (b) for bundle legs via
  the `DegenLedger` bundle_wallet slots. Same call decrypts at signing time.

## Honest P1 stubs (do not present these as finished)

- Aftermath market swap: `service/spot/AftermathSpotAdapter.swap_tx` is
  NotImplementedError upstream — sign-and-submit a real PTB instead. Post-grad
  `buy_post_grad` currently returns `pending` (never a fake ok).
- Streamer upgrades to gRPC (now GraphQL poll ~2s).
- Copy-Trade ingestion, reprice lanes, honeypot v2 calibration, first
  funded-wallet calibration of `simulateTransaction` command casing.
- Blast.fun (`💣` tile) is COMING_SOON; on-chain probe must pass before enable.

## Tests

`pytest tests/degen/` — 37 pass (mock Chain/adapters, no network).
`pytest tests/` — 285 passed, 1 skipped (baseline 248 + degen).