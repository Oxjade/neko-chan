"""DB migration: collapse per-user Telegram bots onto the single master bot.

Legacy `bots` had three columns that made a token-less (master-only) onboarding
impossible:

    bot_token_enc   BLOB  NOT NULL     <- blocks a NULL (no BotFather token)
    bot_token_hash  TEXT  NOT NULL UNIQUE
    bot_username    TEXT  NOT NULL     <- no @BotFather username anymore

SQLite cannot drop NOT NULL in place, so this performs a ONE-TIME, GUARDED
table rebuild. It is deliberately fail-closed and idempotent:

  * `bots.id` values are copied VERBATIM. `exec_ledger.db` (exec_wallets.bot_id,
    exec_orders.bot_id) and the paper store reference bots.id, so renumbering
    would silently orphan every wallet / position / order. The rebuild asserts
    COUNT(*) and SUM(id) are unchanged.
  * `sqlite_sequence` is restored to MAX(id) after the rename, so the next
    INSERT gets id = MAX+1 instead of restarting at 1 and colliding with the
    still-present agent_name UNIQUE.
  * Guarded by PRAGMA user_version so re-running (or a re-deploy) is a no-op,
    and re-checked against the actual notnull flags so an already-migrated DB is
    never rebuilt twice.
  * Wrapped in BEGIN IMMEDIATE; any failed assertion or integrity/foreign_key
    check raises, which ROLLBACKs, so a half-migrated DB is never served.

Fresh installs get the identical shape from BOTS_TABLE_DDL (imported by
store.py), so a brand-new DB and a migrated legacy DB can never drift.
"""

from __future__ import annotations

import sqlite3

# One-time schema version. store.py opens with the SAME constant so a fresh DB
# is stamped to TARGET_VERSION and never re-migrated.
TARGET_VERSION = 1

# Canonical `bots` DDL — single source of truth shared by a fresh install
# (store.py._SCHEMA) and the migration target. Keeping it here means the two can
# never drift; G1 (schema parity) is therefore structural, not coincidental.
BOTS_TABLE_DDL = """
CREATE TABLE bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    bot_name TEXT NOT NULL,
    bot_token_enc BLOB,               -- nullable: master-only bots have no token
    bot_token_hash TEXT UNIQUE,       -- nullable; SQLite allows many NULLs in UNIQUE
    bot_username TEXT NOT NULL DEFAULT '', -- synthesized handle (was @BotFather username)
    agent_name TEXT NOT NULL UNIQUE,
    agent_id INTEGER,
    platform_token TEXT NOT NULL,
    symbols TEXT NOT NULL,
    leverage REAL NOT NULL DEFAULT 1.0,
    interval_sec INTEGER NOT NULL DEFAULT 120,
    risk_profile TEXT NOT NULL,
    risk_caps TEXT NOT NULL,
    is_running INTEGER DEFAULT 0,
    paused INTEGER DEFAULT 0,
    pid INTEGER,
    last_heartbeat TEXT,
    last_error TEXT,
    scheduled_deletion_at TEXT,
    trader_type TEXT DEFAULT 'scalp',
    chain TEXT DEFAULT 'sui',
    network TEXT DEFAULT 'testnet',
    watchlist TEXT DEFAULT '',
    onboarding_complete INTEGER DEFAULT 0,
    wallet_addr TEXT DEFAULT '',
    trading_mode TEXT DEFAULT 'paper',
    priority_watch TEXT DEFAULT '',
    created_at TEXT NOT NULL
)
"""

# columns present in every bots table (legacy or new), used for the copy-SELECT.
# The migration reads the live column list and only copies the intersection, so
# a legacy DB missing trading_mode/priority_watch (added later by store.py's
# ALTER loop) still migrates cleanly.
_ORDERED_NEW_COLS = [
    "id", "tg_id", "bot_name", "bot_token_enc", "bot_token_hash", "bot_username",
    "agent_name", "agent_id", "platform_token", "symbols", "leverage",
    "interval_sec", "risk_profile", "risk_caps", "is_running", "paused", "pid",
    "last_heartbeat", "last_error", "scheduled_deletion_at", "trader_type",
    "chain", "network", "watchlist", "onboarding_complete", "wallet_addr",
    "trading_mode", "priority_watch", "created_at",
]


class MigrationError(Exception):
    """Raised when a pre/post-migration invariant fails. The transaction is
    rolled back and NOT committed, so the DB stays at its old version."""


def _table_info(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["name"]: r for r in conn.execute("PRAGMA table_info(bots)")}


def _is_migrated(conn: sqlite3.Connection) -> bool:
    """True if the token columns have been relaxed to nullable. Only
    bot_token_enc / bot_token_hash change nullability (bot_username stays
    NOT NULL but gains a DEFAULT, so it is not a discriminator)."""
    info = _table_info(conn)
    for col in ("bot_token_enc", "bot_token_hash"):
        if col not in info:
            return False
        if int(info[col]["notnull"] or 0) != 0:
            return False
    return True


def needs_migration(conn: sqlite3.Connection) -> bool:
    """Whether the bots table still carries the legacy NOT NULL token columns."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bots'"
    ).fetchone() is None:
        return False  # nothing to migrate (fresh DB uses _SCHEMA)
    return not _is_migrated(conn)


def backup_to(conn: sqlite3.Connection, dest_path: str) -> None:
    """Consistent snapshot of a WAL database via the online backup API.

    `cp` of a live -wal db is torn; VACUUM INTO is fine but takes a read txn
    that can hit 'database is locked' against agent subprocesses. conn.backup()
    streams page-by-page and is the WAL-safe method."""
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            conn.backup(dst)
    finally:
        dst.close()


def migrate(conn: sqlite3.Connection, *, dry_run: bool = False,
            backup_path: str | None = None) -> dict:
    """Run the guarded rebuild. Returns a report dict. Raises MigrationError
    (transaction rolled back) if any invariant fails.

    Idempotent: a second call on an already-migrated DB is a no-op."""
    conn.row_factory = sqlite3.Row
    cur_ver = conn.execute("PRAGMA user_version").fetchone()[0]

    if not needs_migration(conn):
        # Already the new shape (idempotent): never rebuild, just make sure the
        # version stamp is set so store.py's guard agrees, then return.
        if int(cur_ver) < TARGET_VERSION and not dry_run:
            conn.execute(f"PRAGMA user_version = {TARGET_VERSION}")
            conn.commit()
        return {"action": "noop", "from": int(cur_ver), "to": TARGET_VERSION}

    # snapshot identity BEFORE the swap (measured, not trusted).
    before_count = conn.execute("SELECT COUNT(*) c FROM bots").fetchone()["c"]
    before_sum = conn.execute("SELECT COALESCE(SUM(id),0) s FROM bots").fetchone()["s"]

    live_cols = [r["name"] for r in conn.execute("PRAGMA table_info(bots)")]
    copy_cols = [c for c in _ORDERED_NEW_COLS if c in live_cols]
    # created_at is NOT NULL and must be copied; guard against a schema lacking it.
    if "created_at" not in copy_cols:
        raise MigrationError("bots table has no created_at column; refusing to migrate")
    collist = ", ".join(copy_cols)

    if dry_run:
        return {"action": "would_rebuild", "bots": before_count,
                "copy_cols": copy_cols}

    if backup_path:
        backup_to(conn, backup_path)

    # BEGIN IMMEDIATE grabs the write lock up front so no agent subprocess can
    # interleave a write between our reads and the swap.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS bots_new")
        conn.execute(BOTS_TABLE_DDL.replace("CREATE TABLE bots",
                                            "CREATE TABLE bots_new", 1))
        conn.execute(
            f"INSERT INTO bots_new ({collist}) SELECT {collist} FROM bots")
        conn.execute("DROP TABLE bots")
        conn.execute("ALTER TABLE bots_new RENAME TO bots")
        # restore AUTOINCREMENT high-water so the next insert is MAX(id)+1
        conn.execute(
            "INSERT OR REPLACE INTO sqlite_sequence (name, seq) "
            "SELECT 'bots', COALESCE(MAX(id),0) FROM bots")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bots_owner ON bots(tg_id)")

        # ---- post-swap invariants: any failure ROLLs the whole thing back ----
        after_count = conn.execute("SELECT COUNT(*) c FROM bots").fetchone()["c"]
        after_sum = conn.execute("SELECT COALESCE(SUM(id),0) s FROM bots").fetchone()["s"]
        if after_count != before_count:
            raise MigrationError(f"row count changed {before_count} -> {after_count}")
        if after_sum != before_sum:
            raise MigrationError(f"id checksum changed {before_sum} -> {after_sum}")
        if not _is_migrated(conn):
            raise MigrationError("token columns still NOT NULL after rebuild")
        # AUTOINCREMENT high-water must be >= MAX(id) so the next insert does
        # not restart and collide with an existing agent_name.
        max_id = conn.execute("SELECT COALESCE(MAX(id),0) m FROM bots").fetchone()["m"]
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='bots'").fetchone()
        if not seq or int(seq["seq"]) < int(max_id):
            raise MigrationError(
                f"sqlite_sequence not restored: seq={seq['seq'] if seq else None} max_id={max_id}")

        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            raise MigrationError(f"integrity_check failed: {ic}")
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise MigrationError(f"foreign_key_check failed: {[dict(r) for r in fk]}")

        conn.execute(f"PRAGMA user_version = {TARGET_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"action": "migrated", "from": int(cur_ver), "to": TARGET_VERSION,
            "bots": after_count, "max_id": int(max_id)}
