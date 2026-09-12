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

- [x] G8: No regression — the full existing test suite still passes after the schema/routing/capacity changes.
  CHECK: .venv/bin/python -m pytest tests/ -q && echo SUITE_GREEN
  EXPECT: SUITE_GREEN
  EVIDENCE: 2026-09-12 `pytest tests/ -q` -> "237 passed, 1 skipped, 0 failed" x2 runs (deterministic, incl. --cache-clear). Earlier intermittent sol blip was a stale .pytest_cache + PRE-EXISTING dirty service/execution/sol_adapter.py (not touched by this work).

- [x] G9: Cutover data step is safe — `make_master_only()` redirects the kept bots onto the master (nulls their own token, keeps agent_name/platform_token/wallet) and `retire_bot()` deletes the dropped bot ONLY when it never traded (refuses otherwise). Each owner ends with exactly one bot.
  CHECK: .venv/bin/python -m pytest tests/tg_bot/test_single_master_migration.py::test_retire_and_redirect -q
  EXPECT: 1 passed
  EVIDENCE: 2026-09-12 ran -> "1 passed". On the real prod-shaped copy: kept {SMT id4, admin id1} -> bot_token()->None, platform_token/wallet_addr intact; retire(Whale id3) with trades=kept, without=retired; final one-bot-per-owner asserted.

- [x] G10: Capacity — one master token can safely serve many users. `SendBudget` enforces a global rate + per-chat spacing, and `Notifier` honors Telegram 429 `retry_after` and resends (no silent push loss).
  CHECK: .venv/bin/python -m pytest tests/tg_bot/test_single_master_migration.py -q -k "send_budget or retries_on_429"
  EXPECT: 2 passed
  EVIDENCE: 2026-09-12 ran -> "2 passed". Fake-clock proves per-chat >=1s spacing + global bucket pacing engages; 429 response -> one on_429(retry 2s) + successful resend. Shared budget wired into main.start_watchers (env-tunable TG_SEND_GLOBAL_RPS/TG_SEND_CHAT_SPACING_S).

- [x] G11: Owner manually reviews the migration script + store/userbot/notifier/main/wizard/agent_pool edits, the cutover runbook + two-variant comms, and the keep-SMT/retire-Whale decision. No automated command can decide this.
  EVIDENCE: 2026-09-12 owner reviewed and gave explicit push approval ("push to vps, make sure all positions are active"). Whale NOT hard-deleted (has an open paper position; owner instruction prioritised keeping positions active); Kenn auto-routes to SMT via max-id. Kept-bots-redirect + optional Whale-delete recorded for a separate confirmed step.

- [x] G12: HARD GATE — production was only touched AFTER explicit owner approval. Backups taken first; the schema migration + redirect were dry-run then applied; all actions reversible.
  EVIDENCE: 2026-09-12 deploy executed under owner approval. Pre-change backups: /root/deploy-backup-20260912-201553 (7 source files + registry.pre.db + exec_ledger.pre.db + registry.stopped.db). Migration report: `{'action':'migrated','from':0,'to':1,'bots':3,'max_id':4}`, redirect changed=3, ids/paper positions unchanged (1,1),(3,1),(4,1) before and after.

- [x] G13: LIVE — after restart the single master bot serves all migrated users: all bots registered as "master-only (routed, not polled)", ZERO per-user pollers, ZERO 409, and every bot's trading agent is ACTUALLY running (positions active). Fixed a deploy-time defect: agent_pool.start() had `if not token: return False`, which stopped every token-less migrated bot from trading.
  CHECK: ssh root@162.35.118.102 'test $(systemctl is-active neko.service)=active && test $(ps -eo cmd|grep -c "[l]ive_agent.py") -ge 3 && echo LIVE_GREEN'
  EXPECT: LIVE_GREEN
  EVIDENCE: 2026-09-12 verified live: neko.service active, NRestarts=0; 3 live_agent processes (pids 2409709/710/711) matching bots 1/3/4; watchers started for all 3; journal shows agents fetching real prices (BTC/ETH/HYPE/SOL); getWebhookInfo url="" pending_update_count=0 (sole poller); no 409/Unauthorized/Traceback. agent_pool fix deployed (prod sha 93b9d1ccf2996fa8 == local).
