import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import database
from routes import create_app
from routes_shared import utc_now_iso_z
from tasks import _execute_risk_close, _liquidation_price, _risk_exit_for


def _recent_market_hour_utc() -> str:
    """Most recent weekday 15:00 UTC (11:00 ET, US market open), strictly in the past."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    candidate = now - timedelta(hours=2)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    candidate = candidate.replace(hour=15, minute=0, second=0, microsecond=0)
    if candidate >= now:  # between 00:00-02:00 UTC a 'today 15:00' would be future
        candidate -= timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        candidate = candidate.replace(hour=15, minute=0, second=0, microsecond=0)
    return candidate.strftime('%Y-%m-%dT%H:%M:%SZ')


class RiskManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        database.DATABASE_URL = ""
        database._SQLITE_DB_PATH = os.path.join(self.tmp.name, "test.db")
        database.init_database()
        now = utc_now_iso_z()
        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agents (name, token, points, cash, created_at, updated_at) "
            "VALUES ('risk-agent', 'token-risk', 0, 100000.0, ?, ?)",
            (now, now),
        )
        self.agent_id = cursor.lastrowid
        conn.commit()
        conn.close()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _open_position(self, side="long", qty=10.0, entry=100.0, stop=None, take=None) -> int:
        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO positions (agent_id, symbol, market, side, quantity, entry_price, "
            "current_price, stop_loss, take_profit, opened_at) "
            "VALUES (?, 'BTC', 'crypto', ?, ?, ?, ?, ?, ?, ?)",
            (self.agent_id, side, qty if side == "long" else -qty, entry, entry, stop, take, utc_now_iso_z()),
        )
        pos_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return pos_id

    def _trade_payload(self, **overrides) -> dict:
        payload = {
            "market": "us-stock",
            "symbol": "TSLA",
            "action": "buy",
            "quantity": 10,
            "price": 10,
            "executed_at": _recent_market_hour_utc(),
        }
        payload.update(overrides)
        return payload

    def test_trigger_logic_long_and_short(self) -> None:
        pos = {"side": "long", "quantity": 10.0, "stop_loss": 90.0, "take_profit": 120.0}
        self.assertEqual(_risk_exit_for(pos, 89.9), (90.0, "stop_loss"))
        self.assertEqual(_risk_exit_for(pos, 120.0), (120.0, "take_profit"))
        self.assertEqual(_risk_exit_for(pos, 100.0), (None, None))

        short = {"side": "short", "quantity": -10.0, "stop_loss": 110.0, "take_profit": 80.0}
        self.assertEqual(_risk_exit_for(short, 110.5), (110.0, "stop_loss"))
        self.assertEqual(_risk_exit_for(short, 79.0), (80.0, "take_profit"))
        self.assertEqual(_risk_exit_for(short, 100.0), (None, None))

    def test_api_sets_stop_and_take_levels_on_long(self) -> None:
        with patch("routes_signals.is_market_open", return_value=True), \
             patch("price_fetcher.get_price_from_market", return_value=100.0):
            response = self.client.post(
                "/api/signals/realtime",
                headers={"Authorization": "Bearer token-risk"},
                json=self._trade_payload(stop_loss_pct=10, take_profit_pct=20),
            )
        self.assertEqual(response.status_code, 200, response.text)

        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT stop_loss, take_profit FROM positions WHERE agent_id = ?", (self.agent_id,))
        row = cursor.fetchone()
        conn.close()
        self.assertAlmostEqual(row["stop_loss"], 90.0)
        self.assertAlmostEqual(row["take_profit"], 120.0)

    def test_api_sets_stop_and_take_levels_on_short(self) -> None:
        with patch("routes_signals.is_market_open", return_value=True), \
             patch("price_fetcher.get_price_from_market", return_value=100.0):
            response = self.client.post(
                "/api/signals/realtime",
                headers={"Authorization": "Bearer token-risk"},
                json=self._trade_payload(action="short", stop_loss_pct=10, take_profit_pct=20),
            )
        self.assertEqual(response.status_code, 200, response.text)

        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT side, stop_loss, take_profit FROM positions WHERE agent_id = ?", (self.agent_id,))
        row = cursor.fetchone()
        conn.close()
        self.assertEqual(row["side"], "short")
        self.assertAlmostEqual(row["stop_loss"], 110.0)
        self.assertAlmostEqual(row["take_profit"], 80.0)

    def test_api_rejects_invalid_stop_pct(self) -> None:
        for bad in (-5, 0, 150):
            with patch("routes_signals.is_market_open", return_value=True), \
                 patch("price_fetcher.get_price_from_market", return_value=100.0):
                response = self.client.post(
                    "/api/signals/realtime",
                    headers={"Authorization": "Bearer token-risk"},
                    json=self._trade_payload(stop_loss_pct=bad),
                )
            self.assertEqual(response.status_code, 400, response.text)

    def test_execute_risk_close_long_charges_fee_and_updates_cash(self) -> None:
        pos_id = self._open_position(side="long", qty=10.0, entry=100.0, stop=90.0)
        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM positions WHERE id = ?", (pos_id,))
        pos = dict(cursor.fetchone())
        conn.close()

        result = _execute_risk_close(pos, 90.0, "stop_loss", utc_now_iso_z())
        self.assertTrue(result["closed"])
        self.assertEqual(result["reason"], "stop_loss")

        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT cash FROM agents WHERE id = ?", (self.agent_id,))
        cash = float(cursor.fetchone()["cash"])
        cursor.execute("SELECT COUNT(*) AS c FROM positions WHERE id = ?", (pos_id,))
        pos_count = int(cursor.fetchone()["c"])
        cursor.execute("SELECT COUNT(*) AS c FROM signals WHERE agent_id = ? AND content LIKE '%stop_loss%'", (self.agent_id,))
        signal_count = int(cursor.fetchone()["c"])
        conn.close()

        expected_cash = 100000.0 + 10.0 * 90.0 * (1 - 0.001)
        self.assertAlmostEqual(cash, expected_cash)
        self.assertEqual(pos_count, 0)
        self.assertEqual(signal_count, 1)

    def test_execute_risk_close_short_cover_credits_short_pnl(self) -> None:
        pos_id = self._open_position(side="short", qty=10.0, entry=100.0, take=80.0)
        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM positions WHERE id = ?", (pos_id,))
        pos = dict(cursor.fetchone())
        conn.close()

        result = _execute_risk_close(pos, 80.0, "take_profit", utc_now_iso_z())
        self.assertTrue(result["closed"])
        self.assertEqual(result["reason"], "take_profit")

        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT cash FROM agents WHERE id = ?", (self.agent_id,))
        cash = float(cursor.fetchone()["cash"])
        cursor.execute("SELECT COUNT(*) AS c FROM positions WHERE id = ?", (pos_id,))
        pos_count = int(cursor.fetchone()["c"])
        conn.close()

        # cover credit = (2*entry - exit)*qty - fee = (200 - 80)*10 - 80*10*0.001
        expected_cash = 100000.0 + (2 * 100.0 - 80.0) * 10.0 - 80.0 * 10.0 * 0.001
        self.assertAlmostEqual(cash, expected_cash)
        self.assertEqual(pos_count, 0)


class PerpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        database.DATABASE_URL = ""
        database._SQLITE_DB_PATH = os.path.join(self.tmp.name, "test.db")
        database.init_database()
        now = utc_now_iso_z()
        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agents (name, token, points, cash, created_at, updated_at) "
            "VALUES ('perp-agent', 'token-perp', 0, 100000.0, ?, ?)",
            (now, now),
        )
        self.agent_id = cursor.lastrowid
        conn.commit()
        conn.close()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _open_perp(self, side="long", qty=10.0, entry=100.0, lev=5.0) -> int:
        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO positions (agent_id, symbol, market, side, quantity, entry_price, "
            "current_price, leverage, opened_at) "
            "VALUES (?, 'BTC', 'crypto', ?, ?, ?, ?, ?, ?)",
            (self.agent_id, side, qty if side == "long" else -qty, entry, entry, lev, utc_now_iso_z()),
        )
        pos_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return pos_id

    def test_api_accepts_leverage_and_deducts_margin(self) -> None:
        with patch("routes_signals.is_market_open", return_value=True), \
             patch("price_fetcher.get_price_from_market", return_value=100.0):
            response = self.client.post(
                "/api/signals/realtime",
                headers={"Authorization": "Bearer token-perp"},
                json={"market": "crypto", "symbol": "BTC", "action": "buy",
                      "quantity": 10, "price": 0, "executed_at": "now", "leverage": 5},
            )
        self.assertEqual(response.status_code, 200, response.text)

        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT leverage, quantity FROM positions WHERE agent_id = ?", (self.agent_id,))
        pos = cursor.fetchone()
        cursor.execute("SELECT cash FROM agents WHERE id = ?", (self.agent_id,))
        cash = float(cursor.fetchone()["cash"])
        conn.close()
        self.assertAlmostEqual(float(pos["leverage"]), 5.0)
        # margin = notional/lev + fee = 1000/5 + 1.0
        self.assertAlmostEqual(cash, 100000.0 - 1000.0 / 5.0 - 1.0)

    def test_api_rejects_invalid_leverage(self) -> None:
        for bad in (0.5, 11, 0, -3):
            with patch("routes_signals.is_market_open", return_value=True), \
                 patch("price_fetcher.get_price_from_market", return_value=100.0):
                response = self.client.post(
                    "/api/signals/realtime",
                    headers={"Authorization": "Bearer token-perp"},
                    json={"market": "crypto", "symbol": "BTC", "action": "buy",
                          "quantity": 10, "price": 0, "executed_at": "now", "leverage": bad},
                )
            self.assertEqual(response.status_code, 400, response.text)

    def test_api_rejects_leverage_on_us_stock(self) -> None:
        with patch("routes_signals.is_market_open", return_value=True), \
             patch("price_fetcher.get_price_from_market", return_value=100.0):
            response = self.client.post(
                "/api/signals/realtime",
                headers={"Authorization": "Bearer token-perp"},
                json={"market": "us-stock", "symbol": "TSLA", "action": "buy",
                      "quantity": 10, "price": 0, "executed_at": "now", "leverage": 5},
            )
        self.assertEqual(response.status_code, 400, response.text)

    def test_liquidation_prices(self) -> None:
        self.assertAlmostEqual(_liquidation_price({"entry_price": 100, "leverage": 5, "side": "long"}), 80.5)
        self.assertAlmostEqual(_liquidation_price({"entry_price": 100, "leverage": 5, "side": "short"}), 119.5)
        self.assertAlmostEqual(_liquidation_price({"entry_price": 100, "leverage": 10, "side": "long"}), 90.5)
        self.assertIsNone(_liquidation_price({"entry_price": 100, "leverage": 1, "side": "long"}))

    def test_risk_close_leveraged_long_credits_pnl(self) -> None:
        pos_id = self._open_perp(side="long", qty=10.0, entry=100.0, lev=5.0)
        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM positions WHERE id = ?", (pos_id,))
        pos = dict(cursor.fetchone())
        conn.close()

        result = _execute_risk_close(pos, 80.5, "liquidation", utc_now_iso_z())
        self.assertTrue(result["closed"])
        self.assertEqual(result["reason"], "liquidation")

        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT cash FROM agents WHERE id = ?", (self.agent_id,))
        cash = float(cursor.fetchone()["cash"])
        cursor.execute("SELECT COUNT(*) AS c FROM positions WHERE id = ?", (pos_id,))
        pos_count = int(cursor.fetchone()["c"])
        conn.close()

        # leveraged long liquidation: credit = qty*(entry/lev + liq - entry) - fee
        # = 10*(20 + 80.5 - 100) - 80.5*10*0.001 = 5 - 0.805
        expected_cash = 100000.0 + 10.0 * (100.0 / 5.0 + 80.5 - 100.0) - 80.5 * 10.0 * 0.001
        self.assertAlmostEqual(cash, expected_cash)
        self.assertEqual(pos_count, 0)

    def test_api_leveraged_close_returns_unused_margin_plus_pnl(self) -> None:
        with patch("routes_signals.is_market_open", return_value=True), \
             patch("price_fetcher.get_price_from_market", return_value=100.0):
            response = self.client.post(
                "/api/signals/realtime",
                headers={"Authorization": "Bearer token-perp"},
                json={"market": "crypto", "symbol": "BTC", "action": "buy",
                      "quantity": 10, "price": 0, "executed_at": "now", "leverage": 5},
            )
        self.assertEqual(response.status_code, 200, response.text)

        with patch("routes_signals.is_market_open", return_value=True), \
             patch("price_fetcher.get_price_from_market", return_value=110.0):
            response = self.client.post(
                "/api/signals/realtime",
                headers={"Authorization": "Bearer token-perp"},
                json={"market": "crypto", "symbol": "BTC", "action": "sell",
                      "quantity": 10, "price": 0, "executed_at": "now"},
            )
        self.assertEqual(response.status_code, 200, response.text)

        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT cash FROM agents WHERE id = ?", (self.agent_id,))
        cash = float(cursor.fetchone()["cash"])
        cursor.execute("SELECT COUNT(*) AS c FROM positions WHERE agent_id = ?", (self.agent_id,))
        pos_count = int(cursor.fetchone()["c"])
        conn.close()

        # open: -margin - fee = -(1000/5) - 1.0
        # close: +qty*(entry/lev + price - entry) - fee = 10*(20 + 110 - 100) - 110*10*0.001
        expected = 100000.0 - 1000.0 / 5.0 - 1.0 + 10.0 * (100.0 / 5.0 + 110.0 - 100.0) - 110.0 * 10.0 * 0.001
        self.assertAlmostEqual(cash, expected)
        self.assertEqual(pos_count, 0)


if __name__ == "__main__":
    unittest.main()