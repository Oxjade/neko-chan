"""Weekly forex planner: shape replay, sizing and endpoints."""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import forex_weekly as fw  # noqa: E402


def business_days(n, start="2015-01-05"):
    return pd.bdate_range(start, periods=n)


def steady_noise(n=600, seed=3):
    rng = np.random.default_rng(seed)
    return pd.Series(1.10 * np.exp(np.cumsum(rng.normal(0, 0.004, n))), index=business_days(n))


class ReplayShapeTests(unittest.TestCase):
    def test_outcomes_are_bounded_by_the_stop_and_target(self):
        rows = fw.replay_shape(steady_noise(), "long", 0.75, 1.5)
        self.assertGreater(len(rows), 60)
        for r in rows:
            self.assertGreaterEqual(r["gross"], -0.75 - 1e-9)
            self.assertLessEqual(r["gross"], 1.5 + 1e-9)
            if r["how"] == "stop":
                self.assertAlmostEqual(r["gross"], -0.75)
            if r["how"] == "target":
                self.assertAlmostEqual(r["gross"], 1.5)
            self.assertLess(r["net"], r["gross"])          # costs always charged

    def test_long_and_short_mirror_when_the_week_runs_out(self):
        series = steady_noise()
        longs = fw.replay_shape(series, "long", 50, 50)     # levels never reached
        shorts = fw.replay_shape(series, "short", 50, 50)
        self.assertTrue(all(r["how"] == "week ended" for r in longs))
        for a, b in zip(longs, shorts):
            self.assertAlmostEqual(a["gross"], -b["gross"])

    def test_a_tight_target_wins_more_often_but_needs_more(self):
        series = steady_noise(2500, seed=9)
        tight = fw._summary(fw.replay_shape(series, "long", 1.5, 0.5), 1.0)
        wide = fw._summary(fw.replay_shape(series, "long", 0.5, 1.5), 1.0)
        self.assertGreater(tight["win_rate"], wide["win_rate"])
        self.assertGreater(tight["breakeven_win_rate"], wide["breakeven_win_rate"])

    def test_uptrend_makes_longs_win(self):
        idx = business_days(600)
        series = pd.Series(1.0 * np.exp(np.arange(600) * 0.002
                                        + np.random.default_rng(1).normal(0, 0.001, 600)), index=idx)
        s = fw._summary(fw.replay_shape(series, "long", 3, 3), 1.0)
        self.assertGreater(s["win_rate"], 0.9)


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daily = fw._read(fw.REPO_COPY)
        cls.patch = mock.patch.object(fw, "daily_rates", lambda: cls.daily)
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()

    def test_overview_covers_every_pair_with_a_calibrated_range(self):
        ov = fw.overview()
        self.assertEqual({r["pair"] for r in ov["pairs"]}, set(fw.PAIRS))
        for r in ov["pairs"]:
            self.assertGreater(r["normal_week_pct"], 0.1)
            self.assertLess(r["range_low"], r["price"])
            self.assertGreater(r["range_high"], r["price"])
            self.assertGreater(r["range_held"], 0.6)       # about two weeks in three
            self.assertLess(r["range_held"], 0.8)

    def test_plan_prices_and_sizing(self):
        p = fw.plan("EURUSD", "short", 0.5, 1.0, 1000, 1)
        self.assertGreater(p["stop_price"], p["price"])     # a short's stop is above
        self.assertLess(p["target_price"], p["price"])
        self.assertAlmostEqual(p["sizing"]["loss_at_stop_usd"], 10.0, places=6)
        self.assertAlmostEqual(p["sizing"]["units"] * fw.base_in_usd(self.daily, "EURUSD"),
                               p["sizing"]["position_usd"], places=4)
        self.assertGreater(p["all"]["weeks"], 1000)
        total = p["all"]["target_first"] + p["all"]["stop_first"] + p["all"]["week_ended"]
        self.assertAlmostEqual(total, 1.0)

    def test_usd_base_pair_sizes_in_dollars(self):
        p = fw.plan("USDJPY", "long", 1.0, 2.0, 500, 2)
        self.assertAlmostEqual(p["sizing"]["units"], p["sizing"]["position_usd"])


class WeeklyEndpointTests(unittest.TestCase):
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
        daily = fw._read(fw.REPO_COPY)
        cls.patch = mock.patch.object(fw, "daily_rates", lambda: daily)
        cls.patch.start()
        import routes_web
        cls.routes_web = importlib.reload(routes_web)
        app = FastAPI()
        cls.routes_web.register_web_routes(app)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def test_needs_a_session(self):
        fresh = type(self.client)(self.client.app)
        self.assertEqual(fresh.get("/api/web/weekly").status_code, 401)

    def test_overview_and_plan(self):
        c = self.client
        c.post("/api/web/login", json={"password": "correct horse"})
        self.assertEqual(len(c.get("/api/web/weekly").json()["pairs"]), len(fw.PAIRS))
        body = {"pair": "GBPUSD", "direction": "long", "stop_pct": 0.5, "target_pct": 1.0,
                "account_usd": 100, "risk_pct": 1}
        ok = c.post("/api/web/weekly/plan", json=body)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["pair"], "GBPUSD")
        self.assertEqual(c.post("/api/web/weekly/plan", json={**body, "pair": "XXXYYY"}).status_code, 422)
        self.assertEqual(c.post("/api/web/weekly/plan", json={**body, "direction": "up"}).status_code, 422)
        self.assertEqual(c.post("/api/web/weekly/plan", json={**body, "stop_pct": 0}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
