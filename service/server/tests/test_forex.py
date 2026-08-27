import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import database
from routes import create_app
from price_fetcher import _get_yfinance_forex_price, _normalize_forex_symbol
from routes_shared import is_forex_market_open, normalize_market, validate_executed_at


class ForexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        database.DATABASE_URL = ""
        database._SQLITE_DB_PATH = os.path.join(self.tmp.name, "test.db")
        database.init_database()
        now = "2026-08-26T12:00:00Z"
        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agents (name, token, points, cash, created_at, updated_at) "
            "VALUES ('fx-agent', 'token-fx', 0, 100000.0, ?, ?)",
            (now, now),
        )
        conn.commit()
        self.agent_id = cursor.lastrowid
        conn.close()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_normalize_forex_symbol(self) -> None:
        self.assertEqual(_normalize_forex_symbol("EURUSD"), "EURUSD=X")
        self.assertEqual(_normalize_forex_symbol("usd/jpy"), "USDJPY=X")
        self.assertEqual(_normalize_forex_symbol("gbp-usd"), "GBPUSD=X")
        self.assertIsNone(_normalize_forex_symbol("BTC"))
        self.assertIsNone(_normalize_forex_symbol("USD"))

    def test_forex_market_normalization(self) -> None:
        self.assertEqual(normalize_market("forex"), "forex")
        self.assertEqual(normalize_market("fx"), "forex")
        self.assertEqual(normalize_market("currency"), "forex")

    def test_forex_market_hours(self) -> None:
        class FakeNow(datetime):
            """datetime subclass with a fixed 'now' (converts to the requested tz)."""

            @classmethod
            def now(cls, tz=None):
                value = cls.now_value
                return value.astimezone(tz) if tz else value

        def expect(now_utc: datetime, expected: bool) -> None:
            FakeNow.now_value = now_utc
            with patch("routes_shared.datetime", FakeNow):
                self.assertEqual(is_forex_market_open(), expected)

        # Friday 19:00 ET (23:00 UTC): closed (weekend closes 17:00 ET Friday)
        expect(datetime(2026, 8, 21, 23, 0, tzinfo=timezone.utc), False)
        # Saturday any time: closed
        expect(datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), False)
        # Monday 03:00 UTC: open
        expect(datetime(2026, 8, 24, 3, 0, tzinfo=timezone.utc), True)
        # Sunday 16:00 ET (20:00 UTC): still closed
        expect(datetime(2026, 8, 23, 20, 0, tzinfo=timezone.utc), False)
        # Sunday 18:00 ET (22:00 UTC): open
        expect(datetime(2026, 8, 23, 22, 0, tzinfo=timezone.utc), True)

    def test_executed_at_rejects_forex_weekend(self) -> None:
        ok, _ = validate_executed_at("2026-08-24T12:00:00Z", "forex")  # Monday 12:00 UTC
        self.assertTrue(ok)
        # Friday-evening fills are rejected (weekend close + time-travel guard both fire)
        ok, msg = validate_executed_at("2026-08-21T22:00:00Z", "forex")  # Friday 18:00 ET
        self.assertFalse(ok)
        self.assertIn("past", msg)
        ok, msg = validate_executed_at("2026-08-22T12:00:00Z", "forex")  # Saturday
        self.assertFalse(ok)
        self.assertIn("past", msg)  # time-travel guard fires first for >72h-old fills

    def test_forex_trade_fills_at_server_price(self) -> None:
        with patch("routes_signals.is_market_open", return_value=True), \
             patch("price_fetcher.get_price_from_market", return_value=1.0850):
            response = self.client.post(
                "/api/signals/realtime",
                headers={"Authorization": "Bearer token-fx"},
                json={
                    "market": "forex",
                    "symbol": "EURUSD",
                    "action": "buy",
                    "quantity": 10000,
                    "price": 0,
                    "executed_at": "now",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["market"], "forex")
        self.assertAlmostEqual(response.json()["price"], 1.0850)

        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT entry_price, quantity FROM positions WHERE agent_id = ?", (self.agent_id,))
        pos = cursor.fetchone()
        cursor.execute("SELECT cash FROM agents WHERE id = ?", (self.agent_id,))
        cash = float(cursor.fetchone()["cash"])
        conn.close()
        self.assertAlmostEqual(float(pos["entry_price"]), 1.0850)
        self.assertAlmostEqual(float(pos["quantity"]), 10000.0)
        self.assertAlmostEqual(cash, 100000.0 - 10850.0 * 1.001)

    def test_forex_trade_rejects_unknown_pair(self) -> None:
        with patch("routes_signals.is_market_open", return_value=True), \
             patch("price_fetcher.get_price_from_market", return_value=None):
            response = self.client.post(
                "/api/signals/realtime",
                headers={"Authorization": "Bearer token-fx"},
                json={
                    "market": "forex",
                    "symbol": "ABCDEF",
                    "action": "buy",
                    "quantity": 10000,
                    "price": 0,
                    "executed_at": "now",
                },
            )
        self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()