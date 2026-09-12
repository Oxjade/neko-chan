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

## 5. Boot under the master and smoke-test
```bash
systemctl start neko.service
journalctl -u neko.service -f    # watch for 'registered as master-only (routed, not polled)'
```
- As a migrated owner, open @Neko_tradesbot → `/bots` → Drive your bot →
  dashboard + wallet + positions render **in the master chat**.
- As a brand-new user → *Add My Bot* → name → no token asked.
- The 3 old @BotFather bots (`@Nekochanadminbot`, `@chainprizebot`, `@Nkofbot`)
  go idle; their tokens are still stored (harmless) and can be nulled later.

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

### Variant A — the 2 active owners (Ox_jade, Kenn124Y)
> 🐾 Quick heads-up, {first}: I've folded everything into this chat.
>
> **{bot_names} now live right here.** Same wallet, same positions, same P&L,
> same AI key — nothing moved, nothing was touched. Tap **My Bots → Drive** (or
> `/bots`) to open your dashboard.
>
> Your old standalone bot (@{old_usernames}) isn't used anymore — you can delete
> it in @BotFather to tidy up, or just ignore it. **Your funds are safe and
> fully under your control.** 💰
>
> *(Optional teaser, only if we actually ship it — no date promised:)*
> I'm also cooking up a higher-risk **Degen mode** for you. More soon.

### Variant B — the 16 who bounced (never finished)
> 🐾 You tried Neko a bit ago and got stuck at "paste a bot token" — that step is
> **gone**.
>
> Now it's one tap: tap **Add My Bot**, give your cat a name, pick a chain, and
> you're trading a **$1,000 paper** portfolio in ~30 seconds. No @BotFather, no
> key, no setup. Come try again? 👉 /start

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
