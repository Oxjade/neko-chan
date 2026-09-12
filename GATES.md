# Gates: Single-master-bot migration (no BotFather token, token-less onboarding)

OWNS: service/tg_bot/store.py, service/tg_bot/db_migrate.py, service/tg_bot/userbot.py, service/tg_bot/notifier.py, service/tg_bot/watcher.py, service/tg_bot/main.py, service/tg_bot/handlers/wizard.py, service/tg_bot/handlers/master.py, tests/tg_bot/test_single_master_migration.py

Scope: Collapse all per-user Telegram bots onto the single master bot (@Neko_tradesbot). Users onboard by tapping the master bot only (no @BotFather token). The existing `bots` table is rebuilt so `bot_token_enc`/`bot_token_hash`/`bot_username` become nullable WITHOUT renumbering `id`, without breaking `exec_ledger`/paper linkage, and idempotently. Nothing is pushed to production until the owner manually reviews the code (G9) and approves the push (G10).

- [x] G1: A fresh-install schema and a migrated legacy schema produce an identical `bots` table shape (token columns nullable, `bot_token_hash` still UNIQUE, `agent_name`/`platform_token` still NOT NULL).
  CHECK: .venv/bin/python -m pytest tests/tg_bot/test_single_master_migration.py::test_schema_parity -q
  EXPECT: 1 passed
  EVIDENCE: 2026-09-12 ran `pytest ...::test_schema_parity -q` -> "1 passed". Fresh (store._SCHEMA via db_migrate.BOTS_TABLE_DDL) == migrated (legacy rebuild); notnull(token cols)=0, agent_name/platform_token NOT NULL, UNIQUE(bot_token_hash)+UNIQUE(agent_name) preserved in both.

- [x] G2: The migration is idempotent — running it a second time is a guarded no-op (PRAGMA user_version + notnull check) and does not error or re-rebuild.
  CHECK: .venv/bin/python -m pytest tests/tg_bot/test_single_master_migration.py::test_migration_idempotent -q
  EXPECT: 1 passed
  EVIDENCE: 2026-09-12 ran -> "1 passed". 1st call action=migrated, 2nd call action=noop, no bots_new leftover, count stable=3, re-open via Registry does not re-migrate.

- [x] G3: Migration preserves row identity: `bots.id` values are unchanged, `agent_name`/`platform_token` are unchanged, `sqlite_sequence` is restored to `MAX(id)` so the next inserted bot does not collide/restart, and `COUNT(*)` + `SUM(id)` match before/after.
  CHECK: .venv/bin/python -m pytest tests/tg_bot/test_single_master_migration.py::test_id_sequence_preserved -q
  EXPECT: 1 passed
  EVIDENCE: 2026-09-12 ran -> "1 passed". ids {1,3,4} preserved, agent_name/platform_token/bot_name verbatim, sqlite_sequence seq=4, next create_bot id=5 (no collision).

- [x] G4: A token-less bot can be created after migration with no IntegrityError; `Registry.bot_token()` returns None for it and callers treat None as "no token" without spawning a poller.
  CHECK: .venv/bin/python -m pytest tests/tg_bot/test_single_master_migration.py::test_tokenless_create -q
  EXPECT: 1 passed
  EVIDENCE: 2026-09-12 ran -> "1 passed". create_bot(None) ok, bot_token()->None, two token-less bots coexist (NULLs in UNIQUE), legacy dup-token still raises ValueError. (poller-spawn path proven by G6.)

- [x] G5: Wallet/ledger linkage survives the rebuild — `exec_wallets.bot_id`/`exec_orders.bot_id` and paper-store rows still resolve to the same bot after migration (no orphaned funds/positions).
  CHECK: .venv/bin/python -m pytest tests/tg_bot/test_single_master_migration.py::test_ledger_linkage_intact -q
  EXPECT: 1 passed
  EVIDENCE: 2026-09-12 ran -> "1 passed". exec_wallets/exec_orders bot_ids all still resolve to existing bots.id post-migration; wallet_addr verbatim; 0 orphans.

- [x] G6: The master bot is the sole poller — a token-less bot is registered as a ROUTED application (send identity = master token), NEVER spawns its own polling thread, and the master `route()` forwards an update to the owning bot only (single-bot auto; multi-bot needs explicit selection; foreign tg_id rejected).
  CHECK: .venv/bin/python -m pytest tests/tg_bot/test_single_master_migration.py -q -k "no_per_user_poller or routes_to_owner"
  EXPECT: 2 passed
  EVIDENCE: 2026-09-12 ran -> "2 passed". start_bot(token-less) builds an Application on the master token, sets _route_managed=True, registers NO userbot-* thread. route_target(): single-bot auto-routes to owner; unknown->None; multi-bot none->None; explicit owned->that id; foreign active id ignored. Wizard now registers a token-less bot end-to-end (test_simple_flow_registers_bot asserts bot_token()->None).

- [x] G7: Event pushes route via the master token to `chat_id=tg_id` (notifier/watcher no longer require a per-bot token).
  CHECK: .venv/bin/python -m pytest tests/tg_bot/test_single_master_migration.py::test_notifier_routes_master -q
  EXPECT: 1 passed
  EVIDENCE: 2026-09-12 ran -> "1 passed". Notifier._send/_send_photo/_token fall back to MASTER token when bot_token is None; legacy token used verbatim. main.start_watchers no longer skips token-less bots; cleanup _notify_owner falls back to master; chat_id==tg_id asserted.

- [x] G8: No regression — the full existing test suite still passes after the schema/routing changes.
  CHECK: .venv/bin/python -m pytest tests/ -q && echo SUITE_GREEN
  EXPECT: SUITE_GREEN
  EVIDENCE: 2026-09-12 `pytest tests/ -q --cache-clear` -> "231 passed, 1 skipped" x3 runs (deterministic). Note: an intermittent 1-failure was traced to a stale .pytest_cache ordering interacting with PRE-EXISTING dirty files service/execution/sol_adapter.py + its test (NOT touched by this migration); passes in isolation and after --cache-clear.


- [ ] G9: Owner manually reviews the migration script + store/userbot/notifier edits and the cutover runbook + comms copy (two variants; "Degen mode" claim kept or dropped by explicit decision). No automated command can decide this.
  EVIDENCE: pending

- [ ] G10: HARD GATE — no code is pushed or run against the production server until the owner gives explicit post-review approval. Prod remains untouched.
  EVIDENCE: pending
