"""Live terminal: model position state, the advice it gives, and recommendations."""

import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import market_data  # noqa: E402
import terminal  # noqa: E402


def short_eth(**over):
    row = {"coin": "ETH", "name": "Ether", "side": "short", "leverage": 3, "entry": 2000.0,
           "stop_price": 2180.0, "target_price": 1500.0, "liquidation_price": 2620.0,
           "normal_week_pct": 9.0, "price": 2000.0}
    row.update(over)
    return row


def long_btc(**over):
    row = {"coin": "BTC", "name": "Bitcoin", "side": "long", "leverage": 3, "entry": 100.0,
           "stop_price": 94.0, "target_price": 118.0, "liquidation_price": 68.0,
           "normal_week_pct": 6.0, "price": 100.0}
    row.update(over)
    return row


WEEK = {"week_of": "2026-11-09", "next_call": "2026-11-16", "preview": None,
        "coins": [{"coin": "ETH", "name": "Ether", "side": "short", "skipped": False, "signal": "short",
                   "mood_greedy": False, "four_week_change_pct": -16.0, "leverage": 3},
                  {"coin": "BTC", "name": "Bitcoin", "side": None, "skipped": True, "signal": "short",
                   "mood_greedy": False, "four_week_change_pct": -10.0, "leverage": None}]}


def at(day, hour=12):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


class PositionStateTests(unittest.TestCase):
    def test_open_short_in_profit(self):
        p = terminal.position_state(short_eth(), [{"high": 2050.0, "low": 1780.0}], 1800.0, 9.0)
        self.assertEqual(p["status"], "open")
        self.assertAlmostEqual(p["move_pct"], 10.0)
        self.assertAlmostEqual(p["pnl_usd"], 9.0 * 3 * (0.10 - 0.0009))
        self.assertAlmostEqual(p["stop_distance_pct"], (2180 - 1800) / 1800 * 100)
        self.assertEqual(p["stop_room_left"], 1.5)                 # well clear of the stop
        self.assertFalse(p["target_seen"])

    def test_short_is_stopped_when_a_day_trades_through_the_stop(self):
        days = [{"high": 2100.0, "low": 1950.0}, {"high": 2200.0, "low": 2000.0}]
        p = terminal.position_state(short_eth(), days, 2050.0, 9.0)
        self.assertEqual(p["status"], "stopped")
        self.assertAlmostEqual(p["move_pct"], -9.0)                # closed at the stop, not at today's price
        self.assertLess(p["pnl_usd"], 0)
        self.assertNotIn("stop_distance_pct", p)

    def test_long_stop_liquidation_and_target(self):
        stopped = terminal.position_state(long_btc(), [{"high": 101.0, "low": 93.0}], 97.0, 9.0)
        self.assertEqual(stopped["status"], "stopped")
        gone = terminal.position_state(long_btc(stop_price=None), [{"high": 101.0, "low": 60.0}], 70.0, 9.0)
        self.assertEqual((gone["status"], gone["pnl_usd"]), ("liquidated", -9.0))
        seen = terminal.position_state(long_btc(), [{"high": 119.0, "low": 99.0}], 110.0, 9.0)
        self.assertEqual((seen["status"], seen["target_seen"]), ("open", True))

    def test_near_the_stop(self):
        p = terminal.position_state(long_btc(), [{"high": 101.0, "low": 95.0}], 95.0, 9.0)
        self.assertEqual(p["status"], "open")
        self.assertLess(p["stop_room_left"], terminal.NEAR_STOP)


class AdviceTests(unittest.TestCase):
    def advice(self, position, when=None, week=WEEK):
        return terminal.advice([position], week, when or at(11))

    def keys(self, items):
        return [i["key"] for i in items]

    def test_in_profit_says_hold_and_never_tighten(self):
        p = terminal.position_state(short_eth(), [{"high": 2050.0, "low": 1780.0}], 1800.0, 9.0)
        items = self.advice(p)
        top = items[0]
        self.assertEqual((top["level"], top["key"]), ("good", "profit:ETH"))
        self.assertIn("Hold to Monday", top["text"])
        self.assertIn("made no difference", top["text"])

    def test_stop_hit_is_the_most_urgent(self):
        p = terminal.position_state(short_eth(), [{"high": 2200.0, "low": 2000.0}], 2050.0, 9.0)
        items = self.advice(p)
        self.assertEqual((items[0]["level"], items[0]["key"]), ("critical", "stop:ETH"))
        self.assertIn("Do not re-enter", items[0]["text"])
        self.assertNotIn("close-soon", self.keys(items))          # nothing left open to close

    def test_near_stop_warns_to_leave_it(self):
        p = terminal.position_state(long_btc(), [{"high": 101.0, "low": 95.0}], 95.0, 9.0)
        week = {**WEEK, "coins": []}
        top = terminal.advice([p], week, at(10))[0]
        self.assertEqual((top["level"], top["key"]), ("warn", "near:BTC"))
        self.assertIn("Leave the stop where it is", top["text"])

    def test_small_loss_is_no_action(self):
        p = terminal.position_state(short_eth(), [{"high": 2060.0, "low": 1990.0}], 2040.0, 9.0)
        top = self.advice(p)[0]
        self.assertEqual(top["key"], "down:ETH")
        self.assertIn("No action", top["text"])

    def test_close_reminder_late_entry_and_stay_out(self):
        p = terminal.position_state(short_eth(), [{"high": 2050.0, "low": 1900.0}], 1950.0, 9.0)
        items = self.advice(p, when=at(15, 21))                  # three hours before Monday
        keys = self.keys(items)
        self.assertIn("close-soon", keys)
        soon = next(i for i in items if i["key"] == "close-soon")
        self.assertEqual(soon["level"], "warn")
        self.assertIn("in 3h 00m", soon["text"])
        self.assertIn("late", keys)
        self.assertIn("out:BTC", keys)
        early = self.keys(self.advice(p, when=at(9, 6)))         # Monday morning: nothing to nag about
        self.assertNotIn("close-soon", early)
        self.assertNotIn("late", early)

    def test_preview_appears_near_monday(self):
        week = {**WEEK, "preview": {"coins": [{"coin": "ETH", "name": "Ether", "side": "short", "leverage": 3}]}}
        p = terminal.position_state(short_eth(), [], 1990.0, 9.0)
        late = terminal.advice([p], week, at(15, 12))
        self.assertIn("short Ether at 3x", next(i for i in late if i["key"] == "preview")["text"])
        self.assertNotIn("preview", self.keys(terminal.advice([p], week, at(10))))


class RecommendationTests(unittest.TestCase):
    def test_called_skipped_and_uncovered_markets(self):
        called = terminal.recommendation("ETH", {"trend": "down"}, WEEK)
        self.assertEqual((called["verdict"], called["strength"], called["of"]), ("SHORT", 3, 3))
        self.assertEqual(called["lean"], -1.0)
        skipped = terminal.recommendation("BTC", {"trend": "up"}, WEEK)
        self.assertEqual(skipped["verdict"], "STAY OUT")
        self.assertAlmostEqual(skipped["lean"], -1 / 3)
        other = terminal.recommendation("xyz:NVDA", {"trend": "up"}, WEEK)
        self.assertEqual(other["verdict"], "NO CALL")
        self.assertIn("No rule tested has an edge", other["detail"])


class ChartTests(unittest.TestCase):
    def test_averages_line_up_with_the_candles(self):
        candles = [{"t": i, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i, "v": 1.0} for i in range(300)]
        with mock.patch.object(market_data, "chart_candles", lambda symbol, interval, bars: candles):
            out = terminal.chart("ETH", "1d")
        self.assertEqual(len(out["candles"]), terminal.CHART_BARS["1d"])
        for series in out["emas"].values():
            self.assertEqual(len(series), len(out["candles"]))
        self.assertLess(out["emas"]["40d"][-1], out["emas"]["10d"][-1])     # rising prices: fast above slow
        self.assertLess(out["emas"]["10d"][-1], out["candles"][-1]["c"])

    def test_short_history_pads_instead_of_failing(self):
        candles = [{"t": i, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0} for i in range(30)]
        with mock.patch.object(market_data, "chart_candles", lambda symbol, interval, bars: candles):
            out = terminal.chart("ETH", "1d")
        self.assertEqual(len(out["candles"]), 30)
        self.assertNotIn("40d", out["emas"])
        self.assertEqual(len(out["emas"]["10d"]), 30)


class TerminalEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.fernet import Fernet
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {
            "TG_REGISTRY_PATH": str(Path(cls.tmp.name) / "registry.db"),
            "WEB_OPERATOR_PASSWORD": "correct horse", "WEB_MANAGE_AGENTS": "0",
            "TG_VAULT_MASTER_KEY": Fernet.generate_key().decode()})
        cls.env.start()
        import routes_web
        cls.routes_web = importlib.reload(routes_web)
        app = FastAPI()
        cls.routes_web.register_web_routes(app)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def test_terminal_needs_a_session_and_validates(self):
        fresh = type(self.client)(self.client.app)
        self.assertEqual(fresh.get("/api/web/terminal").status_code, 401)
        self.assertEqual(fresh.get("/api/web/terminal/chart?symbol=ETH").status_code, 401)
        c = self.client
        c.post("/api/web/login", json={"password": "correct horse"})
        with mock.patch.object(terminal, "snapshot", lambda stake: {"stake": stake}):
            self.assertEqual(c.get("/api/web/terminal?stake=25").json(), {"stake": 25.0})
        self.assertEqual(c.get("/api/web/terminal?stake=0").status_code, 422)
        self.assertEqual(c.get("/api/web/terminal/chart?symbol=ETH&interval=5m").status_code, 422)
        with mock.patch.object(market_data, "catalogue", lambda *a, **k: {"ETH": {}}), \
                mock.patch.object(terminal, "chart", lambda symbol, interval: {"symbol": symbol, "i": interval}):
            self.assertEqual(c.get("/api/web/terminal/chart?symbol=ETH&interval=1h").json(),
                             {"symbol": "ETH", "i": "1h"})
            self.assertEqual(c.get("/api/web/terminal/chart?symbol=NOPE").status_code, 422)
        self.assertEqual(c.get("/terminal").status_code, 200)


if __name__ == "__main__":
    unittest.main()
