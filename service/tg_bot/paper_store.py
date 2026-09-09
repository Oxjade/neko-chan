"""Paper trading store: virtual portfolios, positions, and orders.

PER BOT, PER MODE. Every new bot starts in paper mode with $1,000 virtual
USDC. Paper fills use real market prices plus a realistic cost model (venue
fee + platform fee + slippage) so users learn the TRUE economics of the
strategies - not a fantasy win rate.

Tables live in the same registry.db (single DB = simple ops), namespaced
`paper_*`. Real-money execution remains in exec_ledger.db and is untouched.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from tg_config import PAPER_START_USD

_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_portfolios (
    bot_id INTEGER PRIMARY KEY,
    cash REAL NOT NULL,             -- free virtual USDC
    starting REAL NOT NULL,         -- baseline for P&L (reset on reset)
    realized_pnl REAL NOT NULL DEFAULT 0.0,
    fees_paid REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,        -- long | short
    qty REAL NOT NULL,              -- base-token quantity
    entry_price REAL NOT NULL,
    leverage REAL NOT NULL DEFAULT 1.0,
    stop_loss REAL,
    take_profit REAL,
    peak_price REAL,                -- for trailing-stop management
    opened_at TEXT NOT NULL,
    UNIQUE(bot_id, symbol)
);
CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    side TEXT NOT NULL,             -- entry verb: buy | short | sell | cover
    qty REAL NOT NULL,
    price REAL NOT NULL,            -- fill price (market: ref+slippage)
    fee REAL NOT NULL,              -- total fees charged on the fill
    status TEXT NOT NULL,           -- filled
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_positions_bot ON paper_positions(bot_id);
CREATE INDEX IF NOT EXISTS idx_paper_orders_bot ON paper_orders(bot_id);
CREATE TABLE IF NOT EXISTS paper_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,        -- long | short
    qty REAL NOT NULL,
    limit_price REAL NOT NULL,      -- the resting limit
    leverage REAL NOT NULL DEFAULT 1.0,
    stop_loss REAL,
    take_profit REAL,
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE(bot_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_paper_pending_bot ON paper_pending(bot_id);
CREATE TABLE IF NOT EXISTS paper_decisions (
    bot_id INTEGER NOT NULL,
    decision_key TEXT PRIMARY KEY,  -- agent-generated unique key
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    leverage REAL NOT NULL DEFAULT 1.0,
    stop_loss REAL,
    take_profit REAL,
    reasoning TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|taken|rejected|expired
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_decisions_bot ON paper_decisions(bot_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperStore:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------- portfolio
    def ensure_portfolio(self, bot_id: int) -> dict:
        """Get-or-create the $1,000 paper portfolio for a bot."""
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM paper_portfolios WHERE bot_id=?", (bot_id,)).fetchone()
            if row:
                return dict(row)
            now = utcnow()
            self._conn.execute(
                "INSERT INTO paper_portfolios (bot_id, cash, starting, realized_pnl, "
                "fees_paid, created_at, updated_at) VALUES (?, ?, ?, 0, 0, ?, ?)",
                (bot_id, PAPER_START_USD, PAPER_START_USD, now, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM paper_portfolios WHERE bot_id=?", (bot_id,)).fetchone()
            return dict(row)

    def portfolio(self, bot_id: int) -> Optional[dict]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM paper_portfolios WHERE bot_id=?", (bot_id,)).fetchone()
            return dict(row) if row else None

    def _adjust_cash(self, bot_id: int, delta: float, fee: float) -> None:
        self._conn.execute(
            "UPDATE paper_portfolios SET cash = cash + ?, fees_paid = fees_paid + ?, "
            "updated_at = ? WHERE bot_id=?",
            (delta, fee, utcnow(), bot_id))

    def reset(self, bot_id: int) -> dict:
        """Wipe positions/orders and restore the starting balance ($1,000)."""
        with _LOCK:
            p = self.ensure_portfolio(bot_id)
            self._conn.execute("DELETE FROM paper_positions WHERE bot_id=?", (bot_id,))
            self._conn.execute("DELETE FROM paper_orders WHERE bot_id=?", (bot_id,))
            self._conn.execute(
                "UPDATE paper_portfolios SET cash=?, starting=?, realized_pnl=0, "
                "fees_paid=0, updated_at=? WHERE bot_id=?",
                (PAPER_START_USD, PAPER_START_USD, utcnow(), bot_id))
            self._conn.commit()
            return self.portfolio(bot_id)

    # ---------------------------------------------------------- positions
    def open_position(self, bot_id: int, symbol: str, direction: str, qty: float,
                      entry_price: float, leverage: float,
                      stop_loss: Optional[float], take_profit: Optional[float]) -> None:
        now = utcnow()
        with _LOCK:
            self._conn.execute(
                "INSERT INTO paper_positions (bot_id, symbol, direction, qty, entry_price, "
                "leverage, stop_loss, take_profit, peak_price, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(bot_id, symbol) DO UPDATE SET qty=?, direction=?, "
                "entry_price=?, leverage=?, stop_loss=?, take_profit=?, peak_price=?, "
                "opened_at=?",
                (bot_id, symbol, direction, qty, entry_price, leverage,
                 stop_loss, take_profit, entry_price, now,
                 qty, direction, entry_price, leverage, stop_loss, take_profit,
                 entry_price, now))
            self._conn.commit()

    def close_position(self, bot_id: int, symbol: str) -> Optional[dict]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM paper_positions WHERE bot_id=? AND symbol=?",
                (bot_id, symbol)).fetchone()
            if not row:
                return None
            self._conn.execute(
                "DELETE FROM paper_positions WHERE bot_id=? AND symbol=?",
                (bot_id, symbol))
            self._conn.commit()
            return dict(row)

    def positions(self, bot_id: int) -> list[dict]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM paper_positions WHERE bot_id=? ORDER BY id", (bot_id,)).fetchall()
            return [dict(r) for r in rows]

    def update_protect_levels(self, bot_id: int, symbol: str,
                              stop_loss: Optional[float] = None,
                              peak_price: Optional[float] = None) -> None:
        with _LOCK:
            if stop_loss is not None:
                self._conn.execute(
                    "UPDATE paper_positions SET stop_loss=? WHERE bot_id=? AND symbol=?",
                    (stop_loss, bot_id, symbol))
            if peak_price is not None:
                self._conn.execute(
                    "UPDATE paper_positions SET peak_price=MAX(COALESCE(peak_price, ?), ?) "
                    "WHERE bot_id=? AND symbol=?",
                    (peak_price, peak_price, bot_id, symbol))
            self._conn.commit()

    # ---------------------------------------------------------- orders
    def record_order(self, bot_id: int, symbol: str, direction: str, side: str,
                     qty: float, price: float, fee: float,
                     idempotency_key: str) -> int:
        with _LOCK:
            cur = self._conn.execute(
                "INSERT INTO paper_orders (bot_id, symbol, direction, side, qty, price, "
                "fee, status, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?)",
                (bot_id, symbol, direction, side, qty, price, fee,
                 idempotency_key, utcnow()))
            self._conn.commit()
            return cur.lastrowid

    def orders_today(self, bot_id: int) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with _LOCK:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM paper_orders WHERE bot_id=? AND substr(created_at,1,10)=?",
                (bot_id, today)).fetchone()
            return row["c"]

    def filled_symbols_today(self, bot_id: int) -> set[str]:
        """Symbols with an ENTRY fill today (buy/short) - feeds the one-trade
        discipline so paper mode honors the same rules as live."""
        today = datetime.now(timezone.utc).date().isoformat()
        with _LOCK:
            rows = self._conn.execute(
                "SELECT DISTINCT symbol FROM paper_orders WHERE bot_id=? "
                "AND substr(created_at,1,10)=? AND side IN ('buy','short')",
                (bot_id, today)).fetchall()
            return {r["symbol"] for r in rows}

    def dashboard_account(self, bot_id: int, prices: dict | None = None) -> dict:
        """Build an account dict compatible with render_production_dashboard().

        Returns the same shape as _exec_account(): balances (USDC, native,
        realized_pnl), positions list, wallet_address.  USDC = paper cash +
        unrealized P&L at mark prices = total paper equity.  If `prices` is
        given (symbol -> float) the positions include mark prices and unrealized
        PnL; otherwise entry price is shown as the mark."""
        p = self.ensure_portfolio(bot_id)
        pos_rows = self.positions(bot_id)
        positions = []
        for pos in pos_rows:
            mark = (prices or {}).get(pos["symbol"], pos["entry_price"])
            if pos["direction"] == "long":
                unrealized = pos["qty"] * (mark - pos["entry_price"])
            else:
                unrealized = pos["qty"] * (pos["entry_price"] - mark)
            positions.append({
                "symbol": pos["symbol"],
                "side": pos["direction"],
                "qty": pos["qty"],
                "entry": pos["entry_price"],
                "mark_price": mark,
                "pnl": unrealized,
                "leverage": pos.get("leverage"),
                "stop": pos.get("stop_loss"),
                "target": pos.get("take_profit"),
            })
        # Paper USDC = starting + realized_pnl + (cash - starting)
        # which simplifies to cash (since cash = starting + pnl_delta - fees)
        # But for the dashboard we show equity = cash + unrealized P&L
        equity = p["cash"]
        for pos in pos_rows:
            mark = (prices or {}).get(pos["symbol"], pos["entry_price"])
            if pos["direction"] == "long":
                equity += pos["qty"] * (mark - pos["entry_price"])
            else:
                equity += pos["qty"] * (pos["entry_price"] - mark)
        return {
            "balances": {
                "USDC": round(equity, 6),
                "native": 0.0,
                "realized_pnl": round(p["realized_pnl"], 6),
            },
            "positions": positions,
            "wallet_address": "",
        }

    # ------------------------------------------------ pending limit orders
    def pending_orders(self, bot_id: int) -> list[dict]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM paper_pending WHERE bot_id=? ORDER BY id",
                (bot_id,)).fetchall()
            return [dict(r) for r in rows]

    def upsert_pending(self, bot_id: int, symbol: str, direction: str, qty: float,
                       limit_price: float, leverage: float,
                       stop_loss: Optional[float], take_profit: Optional[float],
                       idempotency_key: str) -> None:
        """Place (or replace) a resting limit order. One per symbol — the
        one-position-per-symbol discipline applies to pending orders too."""
        with _LOCK:
            self._conn.execute(
                "INSERT INTO paper_pending (bot_id, symbol, direction, qty, limit_price, "
                "leverage, stop_loss, take_profit, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(bot_id, symbol) DO UPDATE SET qty=excluded.qty, "
                "limit_price=excluded.limit_price, leverage=excluded.leverage, "
                "stop_loss=excluded.stop_loss, take_profit=excluded.take_profit, "
                "idempotency_key=excluded.idempotency_key",
                (bot_id, symbol, direction, qty, limit_price, max(leverage, 1.0),
                 stop_loss, take_profit, idempotency_key, utcnow()))
            self._conn.commit()

    def remove_pending(self, bot_id: int, symbol: str) -> Optional[dict]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM paper_pending WHERE bot_id=? AND symbol=?",
                (bot_id, symbol)).fetchone()
            if row:
                self._conn.execute(
                    "DELETE FROM paper_pending WHERE bot_id=? AND symbol=?",
                    (bot_id, symbol))
                self._conn.commit()
            return dict(row) if row else None

    # -------------------------------------------------- approval decisions
    def put_decision(self, bot_id: int, decision_key: str, symbol: str,
                     direction: str, qty: float, price: float, leverage: float,
                     stop_loss: Optional[float], take_profit: Optional[float],
                     reasoning: str = "") -> None:
        with _LOCK:
            now = utcnow()
            self._conn.execute(
                "INSERT INTO paper_decisions (bot_id, decision_key, symbol, direction, "
                "qty, price, leverage, stop_loss, take_profit, reasoning, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'pending', ?, ?) "
                "ON CONFLICT(decision_key) DO UPDATE SET status='pending', "
                "qty=excluded.qty, price=excluded.price, reasoning=excluded.reasoning, "
                "updated_at=excluded.updated_at",
                (bot_id, decision_key, symbol, direction, qty, price, leverage,
                 stop_loss, take_profit, reasoning, now, now))
            self._conn.commit()

    def get_decision(self, decision_key: str) -> Optional[dict]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM paper_decisions WHERE decision_key=?",
                (decision_key,)).fetchone()
            return dict(row) if row else None

    def set_decision_status(self, decision_key: str, status: str) -> None:
        with _LOCK:
            self._conn.execute(
                "UPDATE paper_decisions SET status=?, updated_at=? WHERE decision_key=?",
                (status, utcnow(), decision_key))
            self._conn.commit()

    # ---------------------------------------------------------- accounting
    def settle(self, bot_id: int, realized_pnl_delta: float, fee: float) -> None:
        """Realized PnL accrues to cash on close (fees already deducted by
        the caller inside realized_pnl_delta or passed separately)."""
        with _LOCK:
            self._conn.execute(
                "UPDATE paper_portfolios SET cash = cash + ?, realized_pnl = "
                "realized_pnl + ?, fees_paid = fees_paid + ?, updated_at=? "
                "WHERE bot_id=?",
                (realized_pnl_delta, realized_pnl_delta, fee, utcnow(), bot_id))
            self._conn.commit()
