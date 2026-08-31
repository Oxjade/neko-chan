"""Execution ledger: wallets, deposits, orders, fills, positions, fees, chain state.

SQLite, per-bot rows, idempotency keys, immutable fee rows.
PLATFORM_FEE_BPS = 50 (flat 0.5% company fee per trade - the only fee model).
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

PLATFORM_FEE_BPS = 50  # 0.5% flat - sole fee model, per owner decision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    pubkey TEXT,
    key_enc BLOB NOT NULL,
    key_hash TEXT NOT NULL,
    status TEXT DEFAULT 'created',   -- created|funded|active|revoked
    created_at TEXT NOT NULL,
    UNIQUE(bot_id, chain)
);
CREATE TABLE IF NOT EXISTS exec_deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER NOT NULL,
    asset TEXT NOT NULL,
    amount REAL NOT NULL,
    tx_hash TEXT NOT NULL,
    status TEXT DEFAULT 'pending',   -- pending|confirmed
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS exec_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    bot_id INTEGER NOT NULL,
    chain TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL,
    order_type TEXT NOT NULL,
    leverage REAL NOT NULL DEFAULT 1.0,
    stop_loss REAL,
    take_profit REAL,
    status TEXT NOT NULL,             -- proposed|submitted|filled|cancelled|rejected
    venue_order_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exec_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    price REAL NOT NULL,
    qty REAL NOT NULL,
    fee_venue REAL NOT NULL DEFAULT 0.0,
    fee_platform REAL NOT NULL DEFAULT 0.0,
    tx_hash TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exec_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    chain TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry REAL NOT NULL,
    leverage REAL NOT NULL DEFAULT 1.0,
    liq_price REAL,
    stop_loss REAL,
    take_profit REAL,
    opened_at TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fee_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    fill_id INTEGER NOT NULL,
    fee_bps INTEGER NOT NULL,
    fee_usd REAL NOT NULL,
    kind TEXT NOT NULL,               -- venue|platform
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chain_state (
    wallet_id INTEGER PRIMARY KEY,
    balances_json TEXT NOT NULL,
    positions_json TEXT NOT NULL,
    orders_json TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecLedger:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        with self._lock:
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------------- wallets ----------------

    def upsert_wallet(self, bot_id: int, chain: str, address: str, pubkey: str,
                      key_enc: bytes, key_hash: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO exec_wallets (bot_id, chain, address, pubkey, key_enc, key_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bot_id, chain) DO UPDATE SET address=?, pubkey=?, key_enc=?, key_hash=?,
                       status='created'""",
                (bot_id, chain, address, pubkey, key_enc, key_hash, utcnow(),
                 address, pubkey, key_enc, key_hash),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id FROM exec_wallets WHERE bot_id=? AND chain=?", (bot_id, chain)
            ).fetchone()
            return row["id"]

    def wallet(self, wallet_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM exec_wallets WHERE id=?", (wallet_id,)).fetchone()
            return dict(row) if row else None

    def wallet_by_bot_chain(self, bot_id: int, chain: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM exec_wallets WHERE bot_id=? AND chain=?", (bot_id, chain)
            ).fetchone()
            return dict(row) if row else None

    def set_wallet_status(self, wallet_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE exec_wallets SET status=? WHERE id=?", (status, wallet_id))
            self._conn.commit()

    # ---------------- deposits ----------------

    def record_deposit(self, wallet_id: int, asset: str, amount: float, tx_hash: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO exec_deposits (wallet_id, asset, amount, tx_hash) VALUES (?, ?, ?, ?)",
                (wallet_id, asset, amount, tx_hash),
            )
            self._conn.commit()
            return cur.lastrowid

    def confirm_deposit(self, deposit_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE exec_deposits SET status='confirmed', confirmed_at=? WHERE id=?",
                (utcnow(), deposit_id),
            )
            self._conn.commit()

    # ---------------- orders / fills ----------------

    def create_order(self, intent, bot_id: int) -> int:
        with self._lock:
            # Atomic duplicate-guard: the UNIQUE(idempotency_key) constraint is
            # the source of truth. If another thread inserted the same key
            # between our check and this insert, IntegrityError fires and we
            # return the EXISTING order id instead of raising (fixes TOCTOU).
            try:
                cur = self._conn.execute(
                    """INSERT INTO exec_orders (idempotency_key, bot_id, chain, venue, symbol, side, qty,
                                                price, order_type, leverage, stop_loss, take_profit, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
                    (intent.idempotency_key, bot_id, intent.chain, intent.venue, intent.symbol,
                     intent.side, intent.qty, intent.limit_price, intent.order_type,
                     intent.leverage, intent.stop_loss, intent.take_profit, utcnow()),
                )
                self._conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT id FROM exec_orders WHERE idempotency_key=?",
                    (intent.idempotency_key,),
                ).fetchone()
                self._conn.commit()
                return row["id"] if row else -1

    def set_order_status(self, order_id: int, status: str, venue_order_id: str | None = None) -> None:
        with self._lock:
            if venue_order_id:
                self._conn.execute(
                    "UPDATE exec_orders SET status=?, venue_order_id=? WHERE id=?",
                    (status, venue_order_id, order_id),
                )
            else:
                self._conn.execute("UPDATE exec_orders SET status=? WHERE id=?", (status, order_id))
            self._conn.commit()

    def order_exists(self, idempotency_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM exec_orders WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            return row is not None

    def record_fill(self, order_id: int, price: float, qty: float,
                    fee_venue: float, tx_hash: str, bot_id: int) -> int:
        with self._lock:
            notional = price * qty
            fee_platform = round(notional * PLATFORM_FEE_BPS / 10000, 6)
            cur = self._conn.execute(
                """INSERT INTO exec_fills (order_id, price, qty, fee_venue, fee_platform, tx_hash, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (order_id, price, qty, fee_venue, fee_platform, tx_hash, utcnow()),
            )
            fill_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO fee_ledger (bot_id, fill_id, fee_bps, fee_usd, kind, ts) "
                "VALUES (?, ?, ?, ?, 'platform', ?)",
                (bot_id, fill_id, PLATFORM_FEE_BPS, fee_platform, utcnow()),
            )
            if fee_venue > 0:
                self._conn.execute(
                    "INSERT INTO fee_ledger (bot_id, fill_id, fee_bps, fee_usd, kind, ts) "
                    "VALUES (?, ?, 0, ?, 'venue', ?)",
                    (bot_id, fill_id, fee_venue, utcnow()),
                )
            self._conn.commit()
            return fill_id

    def fees_for_bot(self, bot_id: int, kind: str | None = None) -> float:
        with self._lock:
            if kind:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(fee_usd), 0) AS s FROM fee_ledger WHERE bot_id=? AND kind=?",
                    (bot_id, kind),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(fee_usd), 0) AS s FROM fee_ledger WHERE bot_id=?",
                    (bot_id,),
                ).fetchone()
            return float(row["s"])

    # ---------------- positions ----------------

    def upsert_position(self, bot_id: int, chain: str, symbol: str, side: str, qty: float,
                        entry: float, leverage: float, liq_price: float | None,
                        stop_loss: float | None, take_profit: float | None) -> None:
        now = utcnow()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE exec_positions SET qty=?, entry=?, leverage=?, liq_price=?, stop_loss=?,
                                             take_profit=?, side=?, synced_at=? 
                   WHERE bot_id=? AND chain=? AND symbol=?""",
                (qty, entry, leverage, liq_price, stop_loss, take_profit, side, now,
                 bot_id, chain, symbol),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    """INSERT INTO exec_positions (bot_id, chain, symbol, side, qty, entry, leverage,
                                                    liq_price, stop_loss, take_profit, opened_at, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (bot_id, chain, symbol, side, qty, entry, leverage, liq_price,
                     stop_loss, take_profit, now, now),
                )
            self._conn.commit()

    def delete_position(self, bot_id: int, chain: str, symbol: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM exec_positions WHERE bot_id=? AND chain=? AND symbol=?",
                (bot_id, chain, symbol),
            )
            self._conn.commit()

    # ---------------- chain state ----------------

    def save_chain_state(self, wallet_id: int, balances: dict, positions: list,
                         orders: list) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO chain_state (wallet_id, balances_json, positions_json, orders_json, synced_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(wallet_id) DO UPDATE SET balances_json=?, positions_json=?,
                       orders_json=?, synced_at=?""",
                (wallet_id, json.dumps(balances), json.dumps(positions), json.dumps(orders),
                 utcnow(), json.dumps(balances), json.dumps(positions), json.dumps(orders), utcnow()),
            )
            self._conn.commit()

    def load_chain_state(self, wallet_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM chain_state WHERE wallet_id=?", (wallet_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            for k in ("balances_json", "positions_json", "orders_json"):
                try:
                    d[k.replace("_json", "")] = json.loads(d[k])
                except Exception:
                    pass
            return d

    def close(self) -> None:
        with self._lock:
            self._conn.close()