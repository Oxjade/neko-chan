"""Web app: trade pairing, statistics, the evidence gate, and console access."""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import web_core  # noqa: E402


def _order(i, symbol, direction, side, qty, price, fee, ts=None):
    return {"id": i, "symbol": symbol, "direction": direction, "side": side, "qty": qty,
            "price": price, "fee": fee, "leverage": 5.0,
            "created_at": ts or f"2026-01-01T00:{i:02d}:00+00:00"}


class PairTradesTests(unittest.TestCase):
    def test_long_round_trip_is_net_of_both_fees(self):
        trades = web_core.pair_trades([
            _order(1, "BTC", "long", "buy", 2.0, 100.0, 1.0),
            _order(2, "BTC", "long", "sell", 2.0, 110.0, 1.5),
        ])
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["gross"], 20.0)
        self.assertAlmostEqual(trades[0]["fees"], 2.5)
        self.assertAlmostEqual(trades[0]["net"], 17.5)
        self.assertAlmostEqual(trades[0]["net_pct"], 17.5 / 200.0 * 100)

    def test_short_profits_when_price_falls(self):
        trades = web_core.pair_trades([
            _order(1, "ETH", "short", "short", 1.0, 100.0, 0.0),
            _order(2, "ETH", "short", "cover", 1.0, 90.0, 0.0),
        ])
        self.assertAlmostEqual(trades[0]["net"], 10.0)

    def test_partial_exits_make_one_trade(self):
        trades = web_core.pair_trades([
            _order(1, "BTC", "long", "buy", 2.0, 100.0, 2.0),
            _order(2, "BTC", "long", "sell", 1.0, 110.0, 1.0),
            _order(3, "BTC", "long", "sell", 1.0, 90.0, 1.0),
        ])
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["gross"], 0.0)
        self.assertAlmostEqual(trades[0]["fees"], 4.0)
        self.assertAlmostEqual(trades[0]["exit"], 100.0)

    def test_open_position_and_orphan_exit_are_not_trades(self):
        trades = web_core.pair_trades([
            _order(1, "SOL", "long", "sell", 1.0, 50.0, 0.1),   # exit with no entry
            _order(2, "BTC", "long", "buy", 1.0, 100.0, 0.1),   # still open
        ])
        self.assertEqual(trades, [])


class PerformanceTests(unittest.TestCase):
    def _trades(self, nets):
        return [{"net": n, "gross": n, "fees": 0.0, "closed_at": f"t{i:04d}"}
                for i, n in enumerate(nets)]

    def test_breakeven_follows_payoff(self):
        perf = web_core.performance(self._trades([20.0, -10.0, -10.0]), 1000.0)
        self.assertAlmostEqual(perf["win_rate"], 1 / 3)
        self.assertAlmostEqual(perf["breakeven_win_rate"], 1 / 3)  # 2:1 payoff
        self.assertAlmostEqual(perf["expectancy"], 0.0)

    def test_empty_record(self):
        perf = web_core.performance([], 1000.0)
        self.assertIsNone(perf["win_rate"])
        self.assertFalse(web_core.evidence_gate(perf)["passed"])

    def test_drawdown(self):
        perf = web_core.performance(self._trades([100.0, -220.0, 50.0]), 1000.0)
        self.assertAlmostEqual(perf["max_drawdown_pct"], 20.0)

    def test_wilson_interval_is_wide_for_small_samples(self):
        lo, hi = web_core.wilson_interval(7, 10)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.85)
        lo, hi = web_core.wilson_interval(700, 1000)
        self.assertGreater(lo, 0.67)

    def test_gate_rejects_a_lucky_streak(self):
        # 9 wins in 10 at 1:1 looks superb but is too few trades to pass.
        perf = web_core.performance(self._trades([10.0] * 9 + [-10.0]), 1000.0)
        gate = web_core.evidence_gate(perf)
        self.assertFalse(gate["passed"])
        self.assertFalse(next(c for c in gate["checks"] if c["key"] == "sample")["passed"])

    def test_gate_passes_a_large_real_edge(self):
        perf = web_core.performance(self._trades(([10.0] * 7 + [-10.0] * 3) * 20), 1000.0)
        self.assertTrue(web_core.evidence_gate(perf)["passed"])


class ConsoleAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.fernet import Fernet
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        cls.tmp = tempfile.TemporaryDirectory()
        cls._env = {k: os.environ.get(k) for k in
                    ("TG_REGISTRY_PATH", "WEB_OPERATOR_PASSWORD", "TG_VAULT_MASTER_KEY",
                     "WEB_MANAGE_AGENTS")}
        os.environ["TG_REGISTRY_PATH"] = str(Path(cls.tmp.name) / "registry.db")
        os.environ["WEB_OPERATOR_PASSWORD"] = "correct horse"
        os.environ["TG_VAULT_MASTER_KEY"] = Fernet.generate_key().decode()
        os.environ["WEB_MANAGE_AGENTS"] = "0"
        import routes_web
        cls.routes_web = importlib.reload(routes_web)
        app = FastAPI()
        cls.routes_web.register_web_routes(app)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()

    def test_console_needs_a_session(self):
        fresh = type(self.client)(self.client.app)
        self.assertEqual(fresh.get("/api/web/console/bots").status_code, 401)
        self.assertEqual(fresh.post("/api/web/console/bots", json={"name": "x"}).status_code, 401)
        self.assertEqual(fresh.get("/api/web/console/waitlist").status_code, 401)

    def test_wrong_password_is_refused(self):
        fresh = type(self.client)(self.client.app)
        self.assertEqual(fresh.post("/api/web/login", json={"password": "nope"}).status_code, 401)

    def test_create_publish_and_read_back(self):
        c = self.client
        self.assertEqual(c.post("/api/web/login", json={"password": "correct horse"}).status_code, 200)
        made = c.post("/api/web/console/bots", json={"name": "Momo", "watchlist": ["BTC"]})
        self.assertEqual(made.status_code, 200, made.text)
        bot = made.json()
        self.assertEqual((bot["mode"], bot["state"]), ("paper", "paused"))
        self.assertEqual(c.post("/api/web/console/bots",
                                json={"name": "Bad", "watchlist": ["DOGE"]}).status_code, 422)
        public = c.get("/api/web/public/track-record").json()
        self.assertEqual(public["bots"], 0)
        c.post(f"/api/web/console/bots/{bot['id']}/public", json={"is_public": True})
        public = c.get("/api/web/public/track-record").json()
        self.assertEqual(public["bots"], 1)
        self.assertEqual(public["performance"]["trades"], 0)
        detail = c.get(f"/api/web/console/bots/{bot['id']}").json()
        self.assertAlmostEqual(detail["report"]["account"]["equity"], 1000.0)

    def test_waitlist_validates_email(self):
        c = self.client
        self.assertEqual(c.post("/api/web/public/waitlist", json={"email": "nope"}).status_code, 422)
        self.assertEqual(c.post("/api/web/public/waitlist",
                                json={"email": "a@example.com"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
