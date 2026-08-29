"""Registry: users, api_keys, bots, events - SQLite, per-owner isolation."""

import json
import sqlite3
import threading
from datetime import datetime, timezone

from key_vault import KeyVault

_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    tg_username TEXT,
    status TEXT DEFAULT 'onboarding',
    accepted_disclaimer INTEGER DEFAULT 0,
    is_admin INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT,
    model TEXT,
    encrypted_key BLOB NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    validated_at TEXT,
    last_used_at TEXT,
    revoked_at TEXT,
    UNIQUE(tg_id, revoked_at)
);
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    bot_name TEXT NOT NULL,
    bot_token_enc BLOB NOT NULL,
    bot_token_hash TEXT NOT NULL UNIQUE,
    bot_username TEXT NOT NULL,
    agent_name TEXT NOT NULL UNIQUE,
    agent_id INTEGER,
    platform_token TEXT NOT NULL,
    symbols TEXT NOT NULL,          -- JSON {"perps":1,"spot":0,"us-stock":1,"forex":0}
    leverage REAL NOT NULL DEFAULT 1.0,
    interval_sec INTEGER NOT NULL DEFAULT 120,
    risk_profile TEXT NOT NULL,
    risk_caps TEXT NOT NULL,        -- JSON preset values
    is_running INTEGER DEFAULT 0,
    paused INTEGER DEFAULT 0,
    pid INTEGER,
    last_heartbeat TEXT,
    last_error TEXT,
    scheduled_deletion_at TEXT,
    trader_type TEXT DEFAULT 'scalp',
    chain TEXT DEFAULT 'sui',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref_id TEXT,
    payload TEXT,
    sent_at TEXT,
    UNIQUE(tg_id, kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_keys_owner ON api_keys(tg_id);
CREATE INDEX IF NOT EXISTS idx_bots_owner ON bots(tg_id);
CREATE INDEX IF NOT EXISTS idx_events_owner ON events(tg_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Registry:
    def __init__(self, path: str, vault: KeyVault):
        self.path = path
        self.vault = vault
        with _LOCK:
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            # migrations for pre-existing databases
            for stmt in (
                "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0",
                "ALTER TABLE bots ADD COLUMN agent_id INTEGER",
                "ALTER TABLE bots ADD COLUMN paused INTEGER DEFAULT 0",
                "ALTER TABLE bots ADD COLUMN scheduled_deletion_at TEXT",
                "ALTER TABLE bots ADD COLUMN trader_type TEXT DEFAULT 'scalp'",
                "ALTER TABLE bots ADD COLUMN chain TEXT DEFAULT 'sui'",
            ):
                try:
                    self._conn.execute(stmt)
                    self._conn.commit()
                except sqlite3.OperationalError:
                    pass
            try:
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_users_admin ON users(is_admin)")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass
            self._conn.commit()

    # ---------------- users ----------------

    def upsert_user(self, tg_id: int, tg_username: str | None = None) -> None:
        with _LOCK:
            self._conn.execute(
                "INSERT INTO users (tg_id, tg_username, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(tg_id) DO UPDATE SET tg_username = COALESCE(?, tg_username)",
                (tg_id, tg_username, utcnow(), tg_username),
            )
            self._conn.commit()

    def promote_first_user_to_admin(self, tg_id: int) -> bool:
        """The first person who talks to the master bot becomes its owner/admin.
        Returns True if the promotion happened."""
        with _LOCK:
            row = self._conn.execute(
                "SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1"
            ).fetchone()
            if row:
                return False
            self._conn.execute(
                "UPDATE users SET is_admin = 1 WHERE tg_id = ?", (tg_id,)
            )
            self._conn.commit()
            return True

    def is_admin(self, tg_id: int) -> bool:
        with _LOCK:
            row = self._conn.execute(
                "SELECT is_admin FROM users WHERE tg_id = ?", (tg_id,)
            ).fetchone()
            return bool(row and row["is_admin"])

    def get_user(self, tg_id: int) -> dict | None:
        with _LOCK:
            row = self._conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
            return dict(row) if row else None

    def accept_disclaimer(self, tg_id: int) -> None:
        with _LOCK:
            self._conn.execute("UPDATE users SET accepted_disclaimer = 1 WHERE tg_id = ?", (tg_id,))
            self._conn.commit()

    # ---------------- api keys ----------------

    def store_key(self, tg_id: int, provider: str, api_key: str, base_url: str | None,
                  model: str) -> dict:
        enc = self.vault.encrypt(api_key)
        h = self.vault.hash_key(api_key)
        now = utcnow()
        with _LOCK:
            dup = self._conn.execute(
                "SELECT tg_id FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (h,)
            ).fetchone()
            if dup and dup["tg_id"] != tg_id:
                raise ValueError("key already in use by another bot")
            self._conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE tg_id = ? AND revoked_at IS NULL",
                (now, tg_id),
            )
            cur = self._conn.execute(
                "INSERT INTO api_keys (tg_id, provider, base_url, model, encrypted_key, key_hash, validated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tg_id, provider, base_url, model, enc, h, now),
            )
            self._conn.commit()
            return {"id": cur.lastrowid, "provider": provider, "model": model}

    def get_active_key(self, tg_id: int) -> dict | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE tg_id = ? AND revoked_at IS NULL "
                "ORDER BY id DESC LIMIT 1", (tg_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["api_key"] = self.vault.decrypt(d.pop("encrypted_key"))
            return d

    def revoke_keys(self, tg_id: int) -> None:
        with _LOCK:
            self._conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE tg_id = ? AND revoked_at IS NULL",
                (utcnow(), tg_id),
            )
            self._conn.commit()

    # ---------------- bots ----------------

    def create_bot(self, tg_id: int, bot_name: str, bot_token: str, bot_username: str,
                   agent_name: str, platform_token: str, symbols: dict, leverage: float,
                   interval_sec: int, risk_profile: str, agent_id: int | None = None) -> dict:
        enc_token = self.vault.encrypt(bot_token)
        token_hash = self.vault.hash_key(bot_token)
        with _LOCK:
            dup = self._conn.execute(
                "SELECT id FROM bots WHERE bot_token_hash = ?", (token_hash,)
            ).fetchone()
            if dup:
                raise ValueError("this Telegram bot token is already registered")
            cur = self._conn.execute(
                "INSERT INTO bots (tg_id, bot_name, bot_token_enc, bot_token_hash, bot_username, agent_name, "
                "agent_id, platform_token, symbols, leverage, interval_sec, risk_profile, risk_caps, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tg_id, bot_name, enc_token, token_hash, bot_username, agent_name,
                 agent_id, platform_token, json.dumps(symbols), leverage, interval_sec,
                 risk_profile, json.dumps({}), utcnow()),
            )
            self._conn.commit()
            return self.get_bot(cur.lastrowid)

    def get_bot(self, bot_id: int) -> dict | None:
        with _LOCK:
            row = self._conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
            return dict(row) if row else None

    def bots_for(self, tg_id: int) -> list[dict]:
        with _LOCK:
            rows = self._conn.execute("SELECT * FROM bots WHERE tg_id = ? ORDER BY id", (tg_id,)).fetchall()
            return [dict(r) for r in rows]

    def all_bots(self) -> list[dict]:
        with _LOCK:
            rows = self._conn.execute("SELECT * FROM bots ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def bot_token(self, bot_id: int) -> str | None:
        with _LOCK:
            row = self._conn.execute("SELECT bot_token_enc FROM bots WHERE id = ?", (bot_id,)).fetchone()
            return self.vault.decrypt(row["bot_token_enc"]) if row else None

    def update_bot(self, bot_id: int, **fields) -> None:
        allowed = {"bot_name", "symbols", "leverage", "interval_sec", "risk_profile",
                   "is_running", "paused", "pid", "last_heartbeat", "last_error",
                   "trader_type", "chain"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        if "symbols" in sets:
            sets["symbols"] = json.dumps(sets["symbols"])
        if "risk_profile" in sets:
            sets["risk_caps"] = json.dumps({})  # refreshed on read
        cols = ", ".join(f"{k} = ?" for k in sets)
        with _LOCK:
            self._conn.execute(f"UPDATE bots SET {cols} WHERE id = ?", (*sets.values(), bot_id))
            self._conn.commit()

    def delete_bot(self, bot_id: int, tg_id: int) -> None:
        with _LOCK:
            self._conn.execute(
                "DELETE FROM bots WHERE id = ? AND tg_id = ?", (bot_id, tg_id)
            )
            self._conn.commit()

    # ---------------- scheduled deletion (unconfigured-bot cleanup) ----------------

    def schedule_bot_deletion(self, bot_id: int, at: str) -> None:
        """Mark a bot for removal at `at` (ISO UTC) unless cancelled before then."""
        with _LOCK:
            self._conn.execute(
                "UPDATE bots SET scheduled_deletion_at = ? WHERE id = ?", (at, bot_id)
            )
            self._conn.commit()

    def cancel_bot_deletion(self, bot_id: int) -> None:
        with _LOCK:
            self._conn.execute(
                "UPDATE bots SET scheduled_deletion_at = NULL WHERE id = ?", (bot_id,)
            )
            self._conn.commit()

    def pending_bot_deletions(self, cutoff_iso: str) -> list[dict]:
        """Bots scheduled for deletion whose deadline has passed."""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM bots WHERE scheduled_deletion_at IS NOT NULL "
                "AND scheduled_deletion_at <= ? ORDER BY scheduled_deletion_at ASC",
                (cutoff_iso,),
            ).fetchall()
            return [dict(r) for r in rows]

    def due_bot_deletions(self) -> list[dict]:
        from datetime import datetime, timezone
        return self.pending_bot_deletions(datetime.now(timezone.utc).isoformat())

    # ---------------- events (notification dedup) ----------------

    def event_seen(self, tg_id: int, kind: str, ref_id: str) -> bool:
        with _LOCK:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE tg_id = ? AND kind = ? AND ref_id = ?",
                (tg_id, kind, ref_id),
            ).fetchone()
            return row is not None

    def mark_event(self, tg_id: int, kind: str, ref_id: str, payload: dict | None = None) -> None:
        with _LOCK:
            self._conn.execute(
                "INSERT OR IGNORE INTO events (tg_id, kind, ref_id, payload, sent_at) VALUES (?, ?, ?, ?, ?)",
                (tg_id, kind, ref_id, json.dumps(payload) if payload else None, utcnow()),
            )
            self._conn.commit()

    def recent_events(self, tg_id: int, limit: int = 30) -> list[dict]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE tg_id = ? ORDER BY id DESC LIMIT ?", (tg_id, limit)
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["payload"] = json.loads(d["payload"]) if d["payload"] else None
                except Exception:
                    pass
                out.append(d)
            return out

    def close(self) -> None:
        with _LOCK:
            self._conn.close()