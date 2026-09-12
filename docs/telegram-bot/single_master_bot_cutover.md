# Cutover runbook + migration comms — single master bot (@Neko_tradesbot)

Status: **DRAFT for manual review (G9).** Nothing here has been run against
production. Prod is `162.35.118.102:/root/app`, service `neko.service`. The
schema rebuild in `service/tg_bot/db_migrate.py` is guarded, idempotent and
fails closed; it also runs automatically on the next `Registry(...)` open, but
this runbook drives it **explicitly** so you can inspect every step.

---

## Why
Users were told to create a bot in @BotFather and paste a token. Funnel today:
**18 tapped /start, 4 accepted the disclaimer, only 2 ever created a bot.** The
token step is where they leave. Collapsing to one master bot removes it.

## What is preserved (do not panic)
- Every wallet, position, order and P&L lives in `exec_ledger.db` keyed on
  `bots.id`. The migration never renumbers `id` and never touches that file.
- Agent registrations, platform tokens, AI keys, risk profiles — all unchanged.
- The 3,800-line dashboard is reused verbatim: token-less bots are served by the
  master via `UserBotController.route()` (send identity = master token, never
  independently polled). Legacy bots that still carry their own token keep their
  dedicated poller during the transition.

---

## 0. Pre-flight (read-only, ~1 min)
```bash
ssh root@162.35.118.102
systemctl status neko.service --no-pager | head -6
cd /root/app
# confirm current shape (should show bot_token_enc NOT NULL, UNIQUE bot_token_hash)
python3 -c "import sqlite3;c=sqlite3.connect('service/tg_bot/registry.db');\
print([tuple(r[1:4]) for r in c.execute('PRAGMA table_info(bots)') if r[1].startswith('bot_')]);\
print('user_version', c.execute('PRAGMA user_version').fetchone()[0]);\
print('bots', c.execute('SELECT COUNT(*),COALESCE(SUM(id),0) FROM bots').fetchone())"
```
EXPECT: 3 bots, `SUM(id)=8` (ids 1,3,4), `user_version=0`, token cols NOT NULL.

## 1. Stop the service (kills all pollers + agent subprocesses)
```bash
systemctl stop neko.service
# verify no python still holds the DB
lsof /root/app/service/tg_bot/registry.db 2>/dev/null || echo "db free"
```

## 2. Backup (both DBs — WAL-safe)
```bash
cd /root/app
ts=$(date +%Y%m%d-%H%M%S)
python3 -c "import sqlite3;sqlite3.connect('service/tg_bot/registry.db')\
.backup(sqlite3.connect('/root/registry.pre-$ts.db'))"
cp service/tg_bot/exec_ledger.db /root/exec_ledger.pre-$ts.db   # not modified, but safe
ls -l /root/*.pre-$ts.db
```

## 3. Dry-run the migration (no writes)
```bash
cd /root/app
.venv/bin/python -c "import sqlite3;from service.tg_bot import db_migrate as m;\
c=sqlite3.connect('service/tg_bot/registry.db');print(m.migrate(c,dry_run=True))"
```
EXPECT: `{'action': 'would_rebuild', 'bots': 3, ...}`. If `action` is `noop`,
the DB is already migrated — skip to step 5.

## 4. Apply
```bash
cd /root/app
.venv/bin/python -c "import sqlite3;from service.tg_bot import db_migrate as m;\
c=sqlite3.connect('service/tg_bot/registry.db');\
print(m.migrate(c,backup_path='/root/registry.auto-mig.db'))"
```
This is transactional: any failed invariant rolls back and raises (DB unchanged,
still NOT NULL, so step 3 would re-offer the rebuild). Success prints
`{'action':'migrated','to':1,'bots':3,'max_id':4}`.

Verify shape + identity restored:
```bash
python3 -c "import sqlite3;c=sqlite3.connect('service/tg_bot/registry.db');\
print([tuple(r[1:4]) for r in c.execute('PRAGMA table_info(bots)') if r[1] in('bot_token_enc','bot_token_hash','bot_username')]);\
print('ids', [r[0] for r in c.execute('SELECT id FROM bots ORDER BY id')]);\
print('seq', c.execute(\"SELECT seq FROM sqlite_sequence WHERE name='bots'\").fetchone()[0]);\
print('version', c.execute('PRAGMA user_version').fetchone()[0])"
```
EXPECT: token_enc/hash notnull=0, bot_username notnull=1 default '',
ids `[1,3,4]`, seq `4`, version `1`.

## 4b. One bot per owner: redirect the kept bots, retire the extra

Decision (confirmed): **keep admin's `Nekoadmin` (id 1) + Kenn124Y's `SMT` (id
4); retire `Whale` (id 3)** — it never traded (0 orders/fills/signals), so the
delete is safe. Both kept bots are redirected onto the master (own token
cleared → served via the router, send identity = @Neko_tradesbot). Wallets,
agents, platform tokens and P&L are untouched (keyed on bot_id).

```bash
cd /root/app
.venv/bin/python -c "import sqlite3;from service.tg_bot import db_migrate as m;\
c=sqlite3.connect('service/tg_bot/registry.db');\
print('redirect:', m.make_master_only(c,[4,1]));\
o=c.execute('SELECT COUNT(*) FROM exec_orders').fetchone()[0] if c.execute(\"SELECT name FROM sqlite_master WHERE name='exec_orders'\").fetchone() else 0;\
print('retire:', m.retire_bot(c,3, has_trades=bool(o)))"
```
NOTE: `has_trades` MUST be computed against `exec_ledger.db` (the bot_id→orders
join), not registry.db. Before deleting, verify Whale has no exec history:
```bash
python3 -c "import sqlite3;c=sqlite3.connect('file:exec_ledger.db?mode=ro',uri=True);\
print('bot3 orders', c.execute('SELECT COUNT(*) FROM exec_orders WHERE bot_id=3').fetchone()[0])"
```
Expect `0`. If non-zero, DO NOT retire — keep it and relink it manually.

After this step: `SELECT id,tg_id FROM bots` → `1|6698272364`, `4|7488318868`.
Every owner has exactly one bot → the master routes straight to their dashboard
(no "My Bots", no switcher).

## 5. Boot under the master and smoke-test
```bash
systemctl start neko.service
journalctl -u neko.service -f    # watch for 'registered as master-only (routed, not polled)'
```
- As a migrated owner, open @Neko_tradesbot → `/start` → **your dashboard
  renders directly** (same P&L, wallet, positions, buttons as your old bot chat).
- As a brand-new user → *Add My Bot* → name → no token asked → dashboard.
- Capacity is automatic: all watcher pushes share one `SendBudget`
  (`TG_SEND_GLOBAL_RPS`, `TG_SEND_CHAT_SPACING_S`) so one master token stays
  inside Telegram's limits no matter how many users. Tune for real traffic
  before a big influx.
- The old @BotFather bots (`@Nekochanadminbot`, `@chainprizebot`, `@Nkofbot`)
  go idle; kept bots' tokens are now NULL (routed); `@Nkofbot`/`Whale`'s row is
  retired. Users can delete them in @BotFather (optional).

## Rollback (if anything is off)
```bash
systemctl stop neko.service
cp /root/registry.pre-$ts.db service/tg_bot/registry.db
rm -f service/tg_bot/registry.db-wal service/tg_bot/registry.db-shm
systemctl start neko.service
```
(The service auto-migrates on open — to roll back you MUST restore the pre-
backup AND the code must predate `Registry` auto-migration, or set the old NOT
NULL file. This is why step 2 backs up before step 4.)

---

## Migration comms — send AFTER step 5 passes (never before cutover)

Two audiences, two messages. Do NOT blast one to all 18.

### Variant A — the 2 active owners (Ox_jade → Nekoadmin, Kenn124Y → SMT)
> 🐾 Heads up {name} — **your Neko bot is now @Neko_tradesbot.**
>
> That's where {bot_name} lives now: same wallet, same positions, same P&L,
> same AI key. Nothing moved.
>
> Just open **@Neko_tradesbot** and send /start — you'll land on your dashboard. 💰
>
> *(Optional teaser, only if we actually ship it — no date promised:)*
> I'm also cooking up a higher-risk **Degen mode**. More soon.

Note: for Kenn124Y, `Whale` was retired and only `SMT` was kept — so name
SMT specifically, and mention that his second (idle) cat was folded away:
> "…you now have one cat here: **SMT** (your other idle bot was merged in —
> nothing was lost, funds untouched)."

### Variant B — the 16 who bounced (never finished, nothing to migrate)
> 🐾 You tried Neko a bit ago and got stuck pasting a bot token — **that step is gone.**
>
> Now your bot is just **@Neko_tradesbot**. Open it, /start, name your cat,
> pick a chain, and you're trading a **$1,000 paper** portfolio in ~30s. No
> @BotFather, no key. Come try again? 👉 @Neko_tradesbot

### On "Degen mode" — decision needed from you (blocks Variant A's last line)
The codebase has **no** "Degen mode" (only an unrelated `degenerate` comment in
`quant_strategy.py`). Options:
1. **Drop it** from the message — cleanest, zero over-promise.
2. **Keep as a no-date teaser** (shown above in parentheses) — only if we are
   genuinely building it; if it slips, expect DMs asking "where's degen?".
3. Build a stub first, then announce. My recommendation: **1 or 2**, not 3 for
   a migration notice.

### Send mechanics
The master can already DM every one of these chats (a `users` row exists because
they /start'd the master; the bot has `chat_id=tg_id`). Reuse the proven path in
`main.py` (`_notify_owner_via_master` → `sendMessage(chat_id=tg_id)`), looped
over `registry.users`, filtered by "has bots" (A) vs "no bots" (B). No token
needed — the master token sends it.
