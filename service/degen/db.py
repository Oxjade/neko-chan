"""DegenLedger: state for degen mode (docs/degen-mode.md §4).

SQLite, WAL, per-bot rows, idempotent fills — same conventions as
service/execution/ledger.py. Bundle wallet keys are Fernet-encrypted BLOBs
(ExecVault) stored in the owner's rows; slot uniqueness enforces the
"never mixed/reused" rule per bot.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

MAX_BUNDLE_WALLETS = 20


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_addr(a: str) -> str:
    a = str(a).strip()
    core = a[2:] if a.lower().startswith("0x") else a
    return "0x" + core.zfill(64).lower()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS degen_config (
    bot_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    ai_key_ok INTEGER NOT NULL DEFAULT 0,
    launchpads TEXT NOT NULL DEFAULT 'suipump',   -- suipump|blast|both
    budget_sui REAL NOT NULL DEFAULT 0.0,
    caps_json TEXT NOT NULL DEFAULT '{}',         -- per_order|daily_loss|max_open (SUI)
    allow_generic INTEGER NOT NULL DEFAULT 1,
    view INTEGER NOT NULL DEFAULT 0,                -- degen VIEW toggled on the dashboard
    chat_receipt TEXT NOT NULL DEFAULT 'public',    -- in-chat @neko receipts: public|private
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS degen_position (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    launchpad TEXT NOT NULL,
    curve_id TEXT NOT NULL,
    token_type TEXT NOT NULL,
    pool_id TEXT NOT NULL DEFAULT '',
    wallet TEXT NOT NULL,                         -- leg address (bundle slot or main)
    symbol TEXT NOT NULL DEFAULT '',
    venue TEXT NOT NULL,                          -- curve|graduating|pool
    entry_sui REAL NOT NULL,
    avg_entry_sui REAL NOT NULL,
    tokens REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',          -- open|closed|illiquid
    tp_pct REAL, sl_pct REAL,
    opened_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(bot_id, launchpad, curve_id, wallet)
);
CREATE TABLE IF NOT EXISTS degen_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    launchpad TEXT NOT NULL,
    curve_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',        -- manual|ai
    conviction REAL NOT NULL DEFAULT 0.0,
    thesis TEXT NOT NULL DEFAULT '',
    tp_pct REAL, sl_pct REAL,
    lane_shape_json TEXT NOT NULL DEFAULT '[]',   -- [{dip_pct,size_share}]
    invalidation_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL, ttl_at TEXT
);
CREATE TABLE IF NOT EXISTS degen_order (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    plan_id INTEGER,
    wallet TEXT NOT NULL DEFAULT '',              -- signing leg ('' = main)
    intent TEXT NOT NULL,                         -- buy|sell
    otype TEXT NOT NULL,                          -- market|limit|dca|tp|sl
    launchpad TEXT NOT NULL,
    curve_id TEXT NOT NULL DEFAULT '',
    token_type TEXT NOT NULL DEFAULT '',
    pool_id TEXT NOT NULL DEFAULT '',
    target_price REAL,                            -- trigger; '' = market now
    trigger_mode TEXT NOT NULL DEFAULT 'price',   -- price|graduating|pool_live|immediate
    qty_sui REAL NOT NULL DEFAULT 0.0,
    qty_tokens REAL NOT NULL DEFAULT 0.0,
    lane_index INTEGER NOT NULL DEFAULT -1,
    state TEXT NOT NULL DEFAULT 'armed',          -- armed|paused|fired|cancelled|failed|expired
    fired_at TEXT, tx_digest TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS degen_fill (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    tx_digest TEXT NOT NULL,
    checkpoint INTEGER NOT NULL DEFAULT 0,
    sui REAL NOT NULL, tokens REAL NOT NULL, price REAL NOT NULL,
    fee_sui REAL NOT NULL DEFAULT 0.0,
    leg_index INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bundle_wallet (
    bot_id INTEGER NOT NULL,
    slot INTEGER NOT NULL,                        -- 1..MAX_BUNDLE_WALLETS
    label TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL,
    key_enc BLOB NOT NULL,                        -- Fernet(ExecVault), owner's row only
    key_hash TEXT NOT NULL,
    gas_mist INTEGER NOT NULL DEFAULT 0,          -- cached top-up level
    state TEXT NOT NULL DEFAULT 'active',         -- active|sweeping|deleted
    created_at TEXT NOT NULL,
    PRIMARY KEY (bot_id, slot),
    UNIQUE (address), UNIQUE (key_hash)
);
CREATE TABLE IF NOT EXISTS snipe_watch (
    bot_id INTEGER NOT NULL,
    deployer TEXT NOT NULL,
    size_sui REAL NOT NULL DEFAULT 0.5,
    mode TEXT NOT NULL DEFAULT 'ask',             -- ask|auto
    spread_burst INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    launches INTEGER NOT NULL DEFAULT 0,
    fails INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (bot_id, deployer)
);
CREATE TABLE IF NOT EXISTS snipe_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    launchpad TEXT NOT NULL,
    curve_id TEXT NOT NULL,
    deployer TEXT NOT NULL,
    decision TEXT NOT NULL,                       -- fired|ask|rejected|skip
    reason TEXT NOT NULL DEFAULT '',
    lag_ms INTEGER, filled INTEGER NOT NULL DEFAULT 0,
    realized_gas_mist INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_watch (
    bot_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    size_pct REAL NOT NULL DEFAULT 25.0,
    mode TEXT NOT NULL DEFAULT 'notify',          -- notify|auto
    scope TEXT NOT NULL DEFAULT 'both',           -- suipump|blast|both
    buys_only INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (bot_id, target)
);
CREATE TABLE IF NOT EXISTS wallet_score (
    wallet TEXT PRIMARY KEY,
    win_rate REAL NOT NULL DEFAULT 0.0,
    realized_sui REAL NOT NULL DEFAULT 0.0,
    avg_hold_s REAL NOT NULL DEFAULT 0.0,
    trades INTEGER NOT NULL DEFAULT 0,
    rugs INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_cursor (
    name TEXT PRIMARY KEY,                        -- 'events:suipump' etc
    cursor TEXT NOT NULL DEFAULT '',
    checkpoint INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gas_pool (
    coin_id TEXT PRIMARY KEY,
    bot_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'gas',          -- gas|buy
    amount_mist INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'free',           -- free|inflight|refill
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS launchpad_state (
    id TEXT PRIMARY KEY,                          -- 'suipump'|'blast'
    live INTEGER NOT NULL DEFAULT 0,
    packages_json TEXT NOT NULL DEFAULT '[]',
    probe_done_at TEXT, notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS grad_fit (
    curve_id TEXT PRIMARY KEY,
    k REAL NOT NULL DEFAULT 0.0,                  -- fitted invariant (x*price-ish)
    fit_err REAL NOT NULL DEFAULT 1.0,
    observations INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS msg_lifecycle (
    bot_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                           -- card|confirm|receipt|input
    curve_id TEXT NOT NULL DEFAULT '',
    delete_after TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS ui_ref (
    ref TEXT PRIMARY KEY,                         -- Telegram callback_data ≤ 64 bytes
    bot_id INTEGER NOT NULL,                      -- so full addresses/curve ids never
    kind TEXT NOT NULL,                           -- ship inline (§ refs only)
    value TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_track (
    tg_uid INTEGER NOT NULL,                      -- subscriber (Telegram user id)
    chat_id INTEGER NOT NULL,                     -- where the alert is posted (group/DM)
    wallet TEXT NOT NULL,                         -- tracked Sui address (no bot needed)
    bot_id INTEGER,                               -- tagger's bot at subscribe time (buy button)
    username TEXT NOT NULL DEFAULT '',            -- tagger handle for @mentions
    buys INTEGER NOT NULL DEFAULT 3,              -- 1=buys, 2=sells, 3=both
    min_sui REAL NOT NULL DEFAULT 1.0,            -- ignore activity below this amount
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tg_uid, wallet)
);
"""


class DegenLedger:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        # additive migration for ledgers created before `view` existed
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(degen_config)")]
        if "view" not in cols:
            self._conn.execute(
                "ALTER TABLE degen_config ADD COLUMN view INTEGER NOT NULL DEFAULT 0")
        if "chat_receipt" not in cols:
            self._conn.execute(
                "ALTER TABLE degen_config ADD COLUMN chat_receipt TEXT NOT NULL DEFAULT 'public'")
        self._conn.commit()

    # ---------------- config ----------------

    def get_config(self, bot_id: int) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM degen_config WHERE bot_id=?", (bot_id,)).fetchone()
        if row:
            d = dict(row)
            d["caps"] = json.loads(d.pop("caps_json") or "{}")
            return d
        return {"bot_id": bot_id, "enabled": 0, "ai_key_ok": 0,
                "launchpads": "suipump", "budget_sui": 0.0, "caps": {},
                "allow_generic": 1, "view": 0, "chat_receipt": "public"}

    def set_config(self, bot_id: int, **fields) -> None:
        caps = fields.pop("caps", None)
        allowed = {"enabled", "ai_key_ok", "launchpads", "budget_sui",
                   "allow_generic", "view", "chat_receipt"}
        sets, vals = [], []
        for k in allowed:
            if k in fields:
                sets.append(f"{k}=?"); vals.append(fields[k])
        if caps is not None:
            sets.append("caps_json=?"); vals.append(json.dumps(caps))
        if not sets:
            return
        sets.append("updated_at=?"); vals.append(utcnow())
        vals.append(bot_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE degen_config SET {','.join(sets)} WHERE bot_id=?", vals)
            if cur.rowcount == 0:
                self._conn.execute(
                    """INSERT INTO degen_config (bot_id, enabled, ai_key_ok, launchpads,
                       budget_sui, caps_json, allow_generic, chat_receipt, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (bot_id, int(fields.get("enabled", 0)), int(fields.get("ai_key_ok", 0)),
                     fields.get("launchpads", "suipump"), fields.get("budget_sui", 0.0),
                     json.dumps(caps or {}), int(fields.get("allow_generic", 1)),
                     fields.get("chat_receipt", "public"), utcnow()))
            self._conn.commit()

    def enabled_bots(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM degen_config WHERE enabled=1 AND ai_key_ok=1").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["caps"] = json.loads(d.pop("caps_json") or "{}")
            out.append(d)
        return out

    # ---------------- wallets / keys ----------------

    def add_bundle_wallet(self, bot_id: int, label: str, address: str,
                          key_enc: bytes, key_hash: str) -> int:
        """Slot assignment; UNIQUE(address|key_hash) rejects any key reuse
        across bots/slots (the 'never mixed for no reason' rule)."""
        with self._lock:
            used = {r["slot"] for r in self._conn.execute(
                "SELECT slot FROM bundle_wallet WHERE bot_id=? AND state!='deleted'",
                (bot_id,))}
            slot = next((s for s in range(1, MAX_BUNDLE_WALLETS + 1) if s not in used), None)
            if slot is None:
                raise ValueError(f"bundle full ({MAX_BUNDLE_WALLETS} wallets max)")
            address = _norm_addr(address)
            try:
                self._conn.execute(
                    """INSERT INTO bundle_wallet (bot_id, slot, label, address, key_enc,
                       key_hash, created_at) VALUES (?,?,?,?,?,?,?)""",
                    (bot_id, slot, label, address, key_enc, key_hash, utcnow()))
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"keypair/ address already bound: {exc}") from exc
            return slot

    def bundle_wallets(self, bot_id: int, active_only: bool = True) -> list[dict]:
        q = "SELECT * FROM bundle_wallet WHERE bot_id=?"
        if active_only:
            q += " AND state='active'"
        q += " ORDER BY slot"
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, (bot_id,))]

    def set_bundle_wallet(self, bot_id: int, slot: int, **f) -> None:
        sets, vals = [], []
        for k in ("label", "gas_mist", "state"):
            if k in f:
                sets.append(f"{k}=?"); vals.append(f[k])
        if not sets:
            return
        vals += [bot_id, slot]
        with self._lock:
            self._conn.execute(
                f"UPDATE bundle_wallet SET {','.join(sets)} WHERE bot_id=? AND slot=?", vals)
            self._conn.commit()

    def wallet_by_address(self, bot_id: int, address: str,
                          include_deleted: bool = False) -> dict | None:
        q = "SELECT * FROM bundle_wallet WHERE bot_id=? AND address=?"
        if not include_deleted:
            q += " AND state!='deleted'"
        with self._lock:
            row = self._conn.execute(q, (bot_id, address)).fetchone()
        return dict(row) if row else None

    def spend_today(self, bot_id: int) -> float:
        with self._lock:
            row = self._conn.execute(
                """SELECT COALESCE(SUM(sui),0) s FROM degen_fill f
                   JOIN degen_order o ON o.id=f.order_id
                   WHERE o.bot_id=? AND f.ts > date('now')""", (bot_id,)).fetchone()
        return float(row["s"] or 0.0)

    def realized_pnl_today(self, bot_id: int) -> float:
        # sells (positive SUI out) minus buys (negative) for closed today; proxy.
        with self._lock:
            row = self._conn.execute(
                """SELECT
                     COALESCE(SUM(CASE WHEN o.intent='sell' THEN f.sui ELSE 0 END),0)
                   - COALESCE(SUM(CASE WHEN o.intent='buy'  THEN f.sui ELSE 0 END),0) AS n
                   FROM degen_fill f JOIN degen_order o ON o.id=f.order_id
                   WHERE o.bot_id=? AND f.ts > date('now')""", (bot_id,)).fetchone()
        return float(row["n"] or 0.0)

    # ---------------- positions ----------------

    def upsert_position(self, bot_id: int, launchpad: str, curve_id: str,
                        token_type: str, wallet: str, *, symbol: str = "",
                        pool_id: str = "", venue: str = "curve",
                        add_sui: float = 0.0, add_tokens: float = 0.0) -> int:
        now = utcnow()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM degen_position WHERE bot_id=? AND launchpad=? AND curve_id=? AND wallet=?",
                (bot_id, launchpad, curve_id, wallet)).fetchone()
            if row:
                r = dict(row)
                tot_sui = r["entry_sui"] + add_sui
                tot_tok = r["tokens"] + add_tokens
                # average entry in SUI per token (cost basis)
                avg = (tot_sui / tot_tok) if tot_tok > 0 else r["avg_entry_sui"]
                self._conn.execute(
                    """UPDATE degen_position SET entry_sui=?, avg_entry_sui=?, tokens=?,
                       status='open', pool_id=?, venue=?, symbol=?, updated_at=? WHERE id=?""",
                    (tot_sui, avg, tot_tok, pool_id or r["pool_id"], venue,
                     symbol or r["symbol"], now, r["id"]))
                self._conn.commit()
                return r["id"]
            cur = self._conn.execute(
                """INSERT INTO degen_position (bot_id, launchpad, curve_id, token_type, pool_id,
                   wallet, symbol, venue, entry_sui, avg_entry_sui, tokens, opened_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bot_id, launchpad, curve_id, token_type, pool_id, wallet, symbol, venue,
                 add_sui, (add_sui / add_tokens if add_tokens > 0 else 0.0), add_tokens, now, now))
            self._conn.commit()
            return cur.lastrowid

    def positions(self, bot_id: int, status: str = "open") -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM degen_position WHERE bot_id=? AND status=? ORDER BY updated_at DESC",
                (bot_id, status))]

    def set_position(self, pos_id: int, **f) -> None:
        sets, vals = [], []
        for k in ("venue", "pool_id", "status", "tp_pct", "sl_pct", "tokens",
                  "entry_sui", "avg_entry_sui", "symbol"):
            if k in f:
                sets.append(f"{k}=?"); vals.append(f[k])
        if not sets:
            return
        sets.append("updated_at=?"); vals.append(utcnow()); vals.append(pos_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE degen_position SET {','.join(sets)} WHERE id=?", vals)
            self._conn.commit()

    def position_by_curve(self, bot_id: int, curve_id: str, wallet: str = "") -> dict | None:
        q = "SELECT * FROM degen_position WHERE bot_id=? AND curve_id=? AND status='open'"
        args = [bot_id, curve_id]
        if wallet:
            q += " AND wallet=?"; args.append(wallet)
        with self._lock:
            row = self._conn.execute(q, args).fetchone()
        return dict(row) if row else None

    # ---------------- orders / plans / fills ----------------

    def add_plan(self, bot_id: int, launchpad: str, curve_id: str, *, source: str,
                 conviction: float = 0.0, thesis: str = "", tp_pct=None, sl_pct=None,
                 lanes: list | None = None, invalidations: list | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO degen_plan (bot_id, launchpad, curve_id, source, conviction,
                   thesis, tp_pct, sl_pct, lane_shape_json, invalidation_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (bot_id, launchpad, curve_id, source, conviction, thesis, tp_pct, sl_pct,
                 json.dumps(lanes or []), json.dumps(invalidations or []), utcnow()))
            self._conn.commit()
            return cur.lastrowid

    def add_order(self, bot_id: int, *, wallet: str, intent: str, otype: str,
                  launchpad: str, idempotency_key: str, curve_id: str = "",
                  token_type: str = "", pool_id: str = "", target_price: float | None = None,
                  trigger_mode: str = "price", qty_sui: float = 0.0,
                  qty_tokens: float = 0.0, lane_index: int = -1,
                  plan_id: int | None = None, state: str = "armed") -> int:
        """Insert an order row. Returns the row id, or -1 if the idempotency key
        already exists. `state` lets the write choke-point RESERVE the key with a
        non-fired, non-armed state (e.g. 'pending') before broadcasting, so a
        redelivered message can never double-spend (TBP-03)."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    """INSERT INTO degen_order (bot_id, plan_id, wallet, intent, otype,
                       launchpad, curve_id, token_type, pool_id, target_price, trigger_mode,
                       qty_sui, qty_tokens, lane_index, idempotency_key, created_at, state)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bot_id, plan_id, wallet, intent, otype, launchpad, curve_id,
                     token_type, pool_id, target_price, trigger_mode, qty_sui,
                     qty_tokens, lane_index, idempotency_key, utcnow(), state))
                self._conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return -1  # duplicate idempotency key

    def armed_orders(self, bot_id: int | None = None) -> list[dict]:
        q = "SELECT * FROM degen_order WHERE state IN ('armed','paused')"
        args: list = []
        if bot_id is not None:
            q += " AND bot_id=?"; args.append(bot_id)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args)]

    def set_order(self, order_id: int, state: str, *, tx_digest: str = "",
                  error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE degen_order SET state=?, tx_digest=COALESCE(NULLIF(?, ''), tx_digest),
                   fired_at=?, error=? WHERE id=?""",
                (state, tx_digest, utcnow(), error, order_id))
            self._conn.commit()

    def record_fill(self, order_id: int, tx_digest: str, *, sui: float, tokens: float,
                    price: float, fee_sui: float = 0.0, checkpoint: int = 0,
                    leg_index: int = 0) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO degen_fill (order_id, tx_digest, checkpoint, sui, tokens,
                   price, fee_sui, leg_index, ts) VALUES (?,?,?,?,?,?,?,?,?)""",
                (order_id, tx_digest, checkpoint, sui, tokens, price, fee_sui,
                 leg_index, utcnow()))
            self._conn.commit()
            return cur.lastrowid

    def fills_for(self, order_id: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM degen_fill WHERE order_id=?", (order_id,))]

    # ---------------- sniper / copy ----------------

    def watch_deployer(self, bot_id: int, deployer: str, size_sui: float = 0.5,
                       mode: str = "ask", spread_burst: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO snipe_watch (bot_id, deployer, size_sui, mode, spread_burst, created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(bot_id, deployer) DO UPDATE SET
                     size_sui=excluded.size_sui, mode=excluded.mode,
                     spread_burst=excluded.spread_burst, enabled=1""",
                (bot_id, deployer.lower(), size_sui, mode, int(spread_burst), utcnow()))
            self._conn.commit()

    def watched_deployers(self, bot_id: int | None = None) -> list[dict]:
        q = "SELECT * FROM snipe_watch WHERE enabled=1"
        args: list = []
        if bot_id is not None:
            q += " AND bot_id=?"; args.append(bot_id)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args)]

    def set_watch(self, bot_id: int, deployer: str, **f) -> None:
        sets, vals = [], []
        for k in ("enabled", "mode", "size_sui", "spread_burst", "launches", "fails"):
            if k in f:
                sets.append(f"{k}=?"); vals.append(f[k])
        if not sets:
            return
        vals += [bot_id, deployer.lower()]
        with self._lock:
            self._conn.execute(
                f"UPDATE snipe_watch SET {','.join(sets)} WHERE bot_id=? AND deployer=?", vals)
            self._conn.commit()

    def log_snipe(self, bot_id: int, launchpad: str, curve_id: str, deployer: str,
                  decision: str, reason: str = "", lag_ms: int | None = None,
                  filled: bool = False, realized_gas_mist: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO snipe_log (bot_id, launchpad, curve_id, deployer, decision,
                   reason, lag_ms, filled, realized_gas_mist, ts) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (bot_id, launchpad, curve_id, deployer.lower(), decision, reason,
                 lag_ms, int(filled), realized_gas_mist, utcnow()))
            self._conn.commit()

    def snipe_stats(self, bot_id: int, days: int = 6) -> dict:
        with self._lock:
            rows = self._conn.execute(
                """SELECT decision, filled, lag_ms FROM snipe_log
                   WHERE bot_id=? AND ts > datetime('now', ?)""",
                (bot_id, f"-{days} days")).fetchall()
        fired = [r for r in rows if r["decision"] == "fired"]
        lags = sorted(r["lag_ms"] for r in fired if r["lag_ms"] is not None)
        return {"fired": len(fired), "total": len(rows),
                "filled": sum(1 for r in fired if r["filled"]),
                "median_lag_ms": lags[len(lags) // 2] if lags else None}

    def track_wallet(self, bot_id: int, target: str, **policy) -> None:
        vals = {**{"size_pct": 25.0, "mode": "notify", "scope": "both",
                   "buys_only": 1}, **policy}
        with self._lock:
            self._conn.execute(
                """INSERT INTO wallet_watch (bot_id, target, size_pct, mode, scope,
                   buys_only, enabled, created_at) VALUES (?,?,?,?,?,?,1,?)
                   ON CONFLICT(bot_id, target) DO UPDATE SET
                     size_pct=?, mode=?, scope=?, buys_only=?, enabled=1""",
                (bot_id, target.lower(), vals["size_pct"], vals["mode"], vals["scope"],
                 vals["buys_only"], utcnow(), vals["size_pct"], vals["mode"],
                 vals["scope"], vals["buys_only"]))
            self._conn.commit()

    def tracked_wallets(self, bot_id: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM wallet_watch WHERE bot_id=? AND enabled=1", (bot_id,))]

    def upsert_wallet_score(self, wallet: str, **f) -> None:
        keys = ("win_rate", "realized_sui", "avg_hold_s", "trades", "rugs")
        with self._lock:
            self._conn.execute(
                """INSERT INTO wallet_score (wallet, win_rate, realized_sui, avg_hold_s,
                   trades, rugs, updated_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(wallet) DO UPDATE SET win_rate=?, realized_sui=?,
                     avg_hold_s=?, trades=?, rugs=?, updated_at=?""",
                (wallet.lower(), f.get("win_rate", 0.0), f.get("realized_sui", 0.0),
                 f.get("avg_hold_s", 0.0), f.get("trades", 0), f.get("rugs", 0), utcnow(),
                 f.get("win_rate", 0.0), f.get("realized_sui", 0.0),
                 f.get("avg_hold_s", 0.0), f.get("trades", 0), f.get("rugs", 0), utcnow()))
            self._conn.commit()

    # ---------------- in-chat wallet tracking ----------------

    def chat_track_count(self, tg_uid: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM chat_track WHERE tg_uid=? AND enabled=1",
                (tg_uid,)).fetchone()
        return int(row["c"]) if row else 0

    def chat_track_add(self, tg_uid: int, chat_id: int, wallet: str,
                       bot_id: int | None = None, username: str = "",
                       buys: int = 3, min_sui: float = 1.0) -> bool:
        """Upsert one tracked wallet. Returns True on success."""
        if buys not in (1, 2, 3):
            buys = 3
        min_sui = max(0.0, float(min_sui))
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO chat_track (tg_uid, chat_id, wallet, bot_id, username,
                       buys, min_sui, enabled, created_at) VALUES (?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(tg_uid, wallet) DO UPDATE SET
                         chat_id=excluded.chat_id, bot_id=excluded.bot_id,
                         username=excluded.username, buys=excluded.buys,
                         min_sui=excluded.min_sui, enabled=1""",
                    (tg_uid, chat_id, _norm_addr(wallet), bot_id, username,
                     buys, min_sui, utcnow()))
                self._conn.commit()
            return True
        except Exception:
            return False

    def chat_track_remove(self, tg_uid: int, wallet: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE chat_track SET enabled=0 WHERE tg_uid=? AND wallet=? AND enabled=1",
                (tg_uid, _norm_addr(wallet)))
            self._conn.commit()
        return cur.rowcount > 0

    def chat_track_rows(self, tg_uid: int) -> list[dict]:
        q = "SELECT * FROM chat_track WHERE tg_uid=? AND enabled=1 ORDER BY created_at"
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, (tg_uid,))]

    def chat_track_all(self) -> list[dict]:
        q = "SELECT * FROM chat_track WHERE enabled=1 ORDER BY created_at"
        with self._lock:
            return [dict(r) for r in self._conn.execute(q)]

    # ---------------- streamer cursors / gas pool / launchpad state ----------------

    def get_cursor(self, name: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM stream_cursor WHERE name=?", (name,)).fetchone()
        return row["cursor"] if row else ""

    def get_cursor_age(self, name: str) -> float | None:
        """Seconds since this cursor was last written, or None if never set."""
        with self._lock:
            row = self._conn.execute(
                "SELECT updated_at FROM stream_cursor WHERE name=?", (name,)).fetchone()
        if not row or not row["updated_at"]:
            return None
        try:
            ts = datetime.fromisoformat(str(row["updated_at"]))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())

    def set_cursor(self, name: str, cursor: str, checkpoint: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO stream_cursor (name, cursor, checkpoint, updated_at)
                   VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET cursor=?, checkpoint=?, updated_at=?""",
                (name, cursor, checkpoint, utcnow(), cursor, checkpoint, utcnow()))
            self._conn.commit()

    def pool_coins(self, bot_id: int, purpose: str = "gas", state: str = "free") -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM gas_pool WHERE bot_id=? AND purpose=? AND state=?",
                (bot_id, purpose, state))]

    def claim_pool_coin(self, bot_id: int, purpose: str = "gas") -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM gas_pool WHERE bot_id=? AND purpose=? AND state='free'
                   ORDER BY amount_mist DESC LIMIT 1""", (bot_id, purpose)).fetchone()
            if not row:
                return None
            self._conn.execute("UPDATE gas_pool SET state='inflight', updated_at=? WHERE coin_id=?",
                               (utcnow(), row["coin_id"]))
            self._conn.commit()
            return dict(row)

    def release_pool_coin(self, coin_id: str, state: str = "refill",
                          amount_mist: int | None = None) -> None:
        with self._lock:
            if amount_mist is None:
                self._conn.execute(
                    "UPDATE gas_pool SET state=?, updated_at=? WHERE coin_id=?",
                    (state, utcnow(), coin_id))
            else:
                self._conn.execute(
                    "UPDATE gas_pool SET state=?, amount_mist=?, updated_at=? WHERE coin_id=?",
                    (state, amount_mist, utcnow(), coin_id))
            self._conn.commit()

    def add_pool_coin(self, bot_id: int, wallet: str, coin_id: str, amount_mist: int,
                      purpose: str = "gas") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO gas_pool (coin_id, bot_id, wallet, purpose, amount_mist,
                   state, updated_at) VALUES (?,?,?,?,?, 'free', ?)
                   ON CONFLICT(coin_id) DO UPDATE SET amount_mist=?, state='free', updated_at=?""",
                (coin_id, bot_id, wallet, purpose, amount_mist, utcnow(), amount_mist, utcnow()))
            self._conn.commit()

    def launchpad_state(self, lp_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM launchpad_state WHERE id=?", (lp_id,)).fetchone()
        if not row:
            return {"id": lp_id, "live": 0, "packages": [], "probe_done_at": None}
        d = dict(row)
        d["packages"] = json.loads(d.pop("packages_json") or "[]")
        return d

    def set_launchpad_state(self, lp_id: str, *, live: bool, packages: list[str],
                            notes: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO launchpad_state (id, live, packages_json, probe_done_at, notes)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET live=?, packages_json=?, probe_done_at=?, notes=?""",
                (lp_id, int(live), json.dumps(packages), utcnow(), notes,
                 int(live), json.dumps(packages), utcnow(), notes))
            self._conn.commit()

    def save_fit(self, curve_id: str, k: float, fit_err: float, observations: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO grad_fit (curve_id, k, fit_err, observations, updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(curve_id) DO UPDATE SET
                     k=?, fit_err=?, observations=?, updated_at=?""",
                (curve_id, k, fit_err, observations, utcnow(), k, fit_err, observations, utcnow()))
            self._conn.commit()

    def get_fit(self, curve_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM grad_fit WHERE curve_id=?", (curve_id,)).fetchone()
        return dict(row) if row else None

    # ---------------- card lifecycle ----------------

    def schedule_delete(self, bot_id: int, chat_id: int, message_id: int, kind: str,
                        seconds: int = 180, curve_id: str = "") -> None:
        due = datetime.now(timezone.utc).timestamp() + seconds
        with self._lock:
            self._conn.execute(
                """INSERT INTO msg_lifecycle (bot_id, chat_id, message_id, kind, curve_id, delete_after)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(chat_id, message_id) DO UPDATE SET
                     delete_after=?, kind=excluded.kind""",
                (bot_id, chat_id, message_id, kind, curve_id,
                 datetime.fromtimestamp(due, timezone.utc).isoformat(),
                 datetime.fromtimestamp(due, timezone.utc).isoformat()))
            self._conn.commit()

    def due_deletes(self, now_iso: str | None = None) -> list[dict]:
        # normalize 'T' vs sqlite's space separator so iso/`datetime()` both compare
        now = (now_iso or utcnow()).replace("T", " ")
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM msg_lifecycle WHERE replace(delete_after,'T',' ') <= ?",
                (now,))]

    def clear_delete(self, chat_id: int, message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM msg_lifecycle WHERE chat_id=? AND message_id=?",
                (chat_id, message_id))
            self._conn.commit()

    # ---------------- ui refs (callback_data ≤ 64 bytes) ----------------

    def make_ref(self, bot_id: int, kind: str, value: str) -> str:
        import secrets
        ref = secrets.token_hex(4)
        with self._lock:
            self._conn.execute(
                "INSERT INTO ui_ref (ref, bot_id, kind, value, created_at) VALUES (?,?,?,?,?)",
                (ref, bot_id, kind, value, utcnow()))
            self._conn.commit()
        return ref

    def resolve_ref(self, ref: str, bot_id: int | None = None) -> tuple[str, str] | None:
        q, args = "SELECT kind, value FROM ui_ref WHERE ref=?", [ref]
        if bot_id is not None:
            q += " AND bot_id=?"; args.append(bot_id)
        with self._lock:
            row = self._conn.execute(q, args).fetchone()
        return (row["kind"], row["value"]) if row else None
