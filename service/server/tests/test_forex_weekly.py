"""Weekly forex planner: shape replay, sizing and endpoints."""

import importlib
import os
import sys
import tempfile
import unittest
from datetime import date
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
        rows = fw.replay_shape(steady_noise(), "long", 0.75, 1.5, cost_pct=0.04)
        self.assertGreater(len(rows), 60)
        for r in rows:
            self.assertGreaterEqual(r["gross"], -0.75 - 1e-9)
            self.assertLessEqual(r["gross"], 1.5 + 1e-9)
            if r["how"] == "stop":
                self.assertAlmostEqual(r["gross"], -0.75)
            if r["how"] == "target":
                self.assertAlmostEqual(r["gross"], 1.5)
            self.assertLess(r["net"], r["gross"])          # costs always charged

    def test_no_costs_by_default(self):
        rows = fw.replay_shape(steady_noise(), "long", 0.75, 1.5)
        self.assertTrue(all(r["net"] == r["gross"] and r["fees"] == 0 for r in rows))

    def test_long_and_short_mirror_when_the_week_runs_out(self):
        series = steady_noise()
        longs = fw.replay_shape(series, "long", 50, 50)     # levels never reached
        shorts = fw.replay_shape(series, "short", 50, 50)
        self.assertTrue(all(r["how"] == "week ended" for r in longs))
        for a, b in zip(longs, shorts):
            self.assertAlmostEqual(a["gross"], -b["gross"])

    def test_a_tight_target_wins_more_often_but_needs_more(self):
        series = steady_noise(2500, seed=9)
        tight = fw._summary(fw.replay_shape(series, "long", 1.5, 0.5), 1.0, 100.0)
        wide = fw._summary(fw.replay_shape(series, "long", 0.5, 1.5), 1.0, 100.0)
        self.assertGreater(tight["win_rate"], wide["win_rate"])
        self.assertGreater(tight["breakeven_win_rate"], wide["breakeven_win_rate"])

    def test_uptrend_makes_longs_win(self):
        idx = business_days(600)
        series = pd.Series(1.0 * np.exp(np.arange(600) * 0.002
                                        + np.random.default_rng(1).normal(0, 0.001, 600)), index=idx)
        s = fw._summary(fw.replay_shape(series, "long", 3, 3), 1.0, 100.0)
        self.assertGreater(s["win_rate"], 0.9)
        # results in pips are the percent results at the given pips per percent
        self.assertAlmostEqual(s["avg_result_pips"], s["avg_result_pct"] * 100.0)


class WeekInProgressTests(unittest.TestCase):
    """Weeks end with Friday's fixing; the week still being traded is not a week yet."""

    FRIDAY = date(2026, 11, 20)

    @staticmethod
    def through(last_day):
        days = pd.bdate_range("2024-01-01", last_day)
        rng = np.random.default_rng(4)
        return pd.Series(1.10 * np.exp(np.cumsum(rng.normal(0, 0.004, len(days)))), index=days)

    def test_a_week_is_in_progress_until_its_friday_fixing_or_the_friday_has_passed(self):
        cases = [
            # Friday label, last fixing, today, still in progress?
            (self.FRIDAY, date(2026, 11, 16), date(2026, 11, 16), True),    # Monday, one fixing in
            (self.FRIDAY, date(2026, 11, 19), date(2026, 11, 20), True),    # Friday before its fixing
            (self.FRIDAY, date(2026, 11, 20), date(2026, 11, 20), False),   # Friday after its fixing
            (self.FRIDAY, date(2026, 11, 19), date(2026, 11, 21), False),   # a holiday Friday, seen on Saturday
            (date(2026, 11, 13), date(2026, 11, 13), date(2026, 11, 15), False),   # an ordinary weekend
        ]
        for friday, last_fixing, today, expected in cases:
            self.assertEqual(fw.week_in_progress(friday, last_fixing, today), expected, (last_fixing, today))

    def test_monday_with_one_fixing_after_a_completed_friday(self):
        finished = fw.weekly_frame(self.through("2026-11-13"), today=date(2026, 11, 14))
        monday = fw.weekly_frame(self.through("2026-11-16"), today=date(2026, 11, 16))
        self.assertEqual(monday.index[-1].date(), date(2026, 11, 13))
        pd.testing.assert_frame_equal(monday, finished)          # Monday's fixing changes nothing measured per week

    def test_friday_before_and_after_its_fixing(self):
        before = fw.weekly_frame(self.through("2026-11-19"), today=self.FRIDAY)
        self.assertEqual(before.index[-1].date(), date(2026, 11, 13))
        after = fw.weekly_frame(self.through("2026-11-20"), today=self.FRIDAY)
        self.assertEqual(after.index[-1].date(), self.FRIDAY)
        self.assertEqual(len(after), len(before) + 1)

    def test_holiday_friday_seen_on_saturday_closes_the_week_at_thursdays_fixing(self):
        series = self.through("2026-11-19")                      # no fixing on Friday the 20th
        weeks = fw.weekly_frame(series, today=date(2026, 11, 21))
        self.assertEqual(weeks.index[-1].date(), self.FRIDAY)
        self.assertEqual(weeks["close"].iloc[-1], series.iloc[-1])

    def test_the_unfinished_week_is_not_scored_as_a_trade(self):
        with mock.patch.object(fw, "today_utc", lambda: date(2026, 11, 18)):
            midweek = fw.replay_shape(self.through("2026-11-18"), "long", 0.75, 1.5)
            friday = fw.replay_shape(self.through("2026-11-13"), "long", 0.75, 1.5)
        self.assertEqual(midweek, friday)
        with mock.patch.object(fw, "today_utc", lambda: self.FRIDAY):
            done = fw.replay_shape(self.through("2026-11-20"), "long", 0.75, 1.5)
        self.assertEqual(len(done), len(friday) + 1)
        self.assertEqual(done[:-1], friday)


class PipTests(unittest.TestCase):
    def test_a_pip_is_the_second_decimal_for_yen_pairs_and_the_fourth_for_the_rest(self):
        yen = {"USDJPY", "EURJPY", "GBPJPY", "AUDJPY"}
        self.assertEqual({pair for pair in fw.PAIRS if fw.pip_size(pair) == 0.01}, yen)
        self.assertTrue(all(fw.pip_size(pair) == 0.0001 for pair in set(fw.PAIRS) - yen))
        self.assertAlmostEqual(fw.pips_per_percent("EURUSD", 1.2), 120.0)      # 1% of 1.2000 is 120 pips
        self.assertAlmostEqual(fw.pips_per_percent("USDJPY", 150.0), 150.0)    # 1% of 150.00 is 150 pips

    def test_a_plan_request_is_checked_in_plain_words(self):
        ok = ("EURUSD", "long", 50, 100)
        self.assertIsNone(fw.plan_problem(*ok))
        self.assertIsNone(fw.plan_problem(*ok, 1.5))
        self.assertIsNone(fw.plan_problem("USDJPY", "short", 5000, 5000, 4999))
        self.assertEqual(fw.plan_problem("XXXYYY", "long", 50, 100), "That pair is not on the list.")
        self.assertEqual(fw.plan_problem("EURUSD", "up", 50, 100), "Choose long or short.")
        stop = "Enter the stop distance in pips: more than 0 and at most 5000."
        target = "Enter the target distance in pips: more than 0 and at most 5000."
        spread = "Enter your broker's spread in pips: 0 or more, and smaller than the stop."
        for bad in (0, -5, 5000.5, float("nan"), float("inf")):
            self.assertEqual(fw.plan_problem("EURUSD", "long", bad, 100), stop)
            self.assertEqual(fw.plan_problem("EURUSD", "long", 50, bad), target)
        for bad in (-0.1, 50, 80, float("nan")):
            self.assertEqual(fw.plan_problem("EURUSD", "long", 50, 100, bad), spread)


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
            self.assertEqual(r["pip_size"], fw.pip_size(r["pair"]))
            # a normal week in pips is the same distance as the percent, at the latest price
            self.assertAlmostEqual(r["normal_week_pips"] * r["pip_size"],
                                   r["price"] * r["normal_week_pct"] / 100)
            self.assertGreater(r["normal_week_pips"], 20)
            self.assertLess(r["normal_week_pips"], 600)
            self.assertEqual(r["last_week_pips"] > 0, r["last_week_pct"] > 0)
            # last week's move in pips is the move that happened, within the rounding of the percent
            self.assertAlmostEqual(r["last_week_pips"] * r["pip_size"] / r["price"] * 100,
                                   r["last_week_pct"], delta=abs(r["last_week_pct"]) * 0.1 + 1e-9)
            self.assertGreater(r["normal_week_pct"], 0.1)
            self.assertLess(r["range_low"], r["price"])
            self.assertGreater(r["range_high"], r["price"])
            self.assertGreater(r["range_held"], 0.6)       # about two weeks in three
            self.assertLess(r["range_held"], 0.8)

    def test_the_week_in_progress_is_reported_apart_from_last_week(self):
        friday = fw.overview()                                   # the table ends on Friday 13 November 2026
        self.assertEqual(friday["as_of"], "2026-11-13")
        for r in friday["pairs"]:
            self.assertIsNone(r["this_week_pct"])                # nothing has traded since that Friday
            self.assertIsNone(r["this_week_pips"])
        plan_friday = fw.plan("EURUSD", "long", 50, 100, 1000, 1)

        # Monday's fixing arrives: the euro is half a percent lower against everything
        monday = self.daily.iloc[[-1]].copy()
        monday.index = [pd.Timestamp("2026-11-16")]
        monday.index.name = self.daily.index.name
        monday = monday * 0.995
        with_monday = pd.concat([self.daily, monday])
        with mock.patch.object(fw, "daily_rates", lambda: with_monday), \
                mock.patch.object(fw, "today_utc", lambda: date(2026, 11, 16)):
            now = fw.overview()
            plan_monday = fw.plan("EURUSD", "long", 50, 100, 1000, 1)
        self.assertEqual(now["as_of"], "2026-11-16")
        before = {r["pair"]: r for r in friday["pairs"]}
        for r in now["pairs"]:
            was = before[r["pair"]]
            # everything measured per week still rests on the week that ended on Friday
            for key in ("last_week_pct", "last_week_pips", "normal_week_pct", "range_held", "trend_13w", "trend_52w"):
                self.assertEqual(r[key], was[key], (r["pair"], key))
            # and the new fixing shows as this week so far, against Friday's close
            self.assertAlmostEqual(r["this_week_pct"], (r["price"] / was["price"] - 1) * 100)
            self.assertAlmostEqual(r["this_week_pips"], (r["price"] - was["price"]) / r["pip_size"])
        eurusd = next(r for r in now["pairs"] if r["pair"] == "EURUSD")
        self.assertAlmostEqual(eurusd["this_week_pct"], -0.5)
        self.assertAlmostEqual(eurusd["this_week_pips"], -0.005 * before["EURUSD"]["price"] / 0.0001)
        eurgbp = next(r for r in now["pairs"] if r["pair"] == "EURGBP")
        self.assertAlmostEqual(eurgbp["this_week_pct"], -0.5)
        gbpusd = next(r for r in now["pairs"] if r["pair"] == "GBPUSD")
        self.assertAlmostEqual(gbpusd["this_week_pct"], 0.0)     # both legs moved together
        # the plan enters at Monday's fixing, and the unfinished week is not one of its past weeks
        self.assertEqual(plan_monday["price"], eurusd["price"])
        self.assertEqual(plan_monday["all"]["weeks"], plan_friday["all"]["weeks"])
        self.assertEqual(plan_monday["normal_week_pct"], plan_friday["normal_week_pct"])
        self.assertEqual(plan_monday["trend_13w"], plan_friday["trend_13w"])

    def test_plan_prices_and_sizing(self):
        p = fw.plan("EURUSD", "short", 50, 100, 1000, 1)
        self.assertAlmostEqual(p["stop_price"], p["price"] + 0.0050)     # a short's stop is 50 pips above
        self.assertAlmostEqual(p["target_price"], p["price"] - 0.0100)   # and its target 100 pips below
        self.assertEqual((p["pip_size"], p["stop_pips"], p["target_pips"], p["spread_pips"]), (0.0001, 50, 100, 0.0))
        self.assertAlmostEqual(p["stop_pct"], 0.005 / p["price"] * 100)
        self.assertAlmostEqual(p["normal_week_pips"] * 0.0001, p["price"] * p["normal_week_pct"] / 100)
        self.assertAlmostEqual(p["stop_in_weeks"], 50 / p["normal_week_pips"])
        self.assertAlmostEqual(p["sizing"]["loss_at_stop_usd"], 10.0, places=6)
        self.assertAlmostEqual(p["sizing"]["gain_at_target_usd"], 20.0, places=6)
        self.assertEqual(p["cost_pct"], 0.0)
        # a pip on a pair quoted in dollars is worth 10 dollars a lot, and the stop is pips times pip value
        self.assertAlmostEqual(p["sizing"]["pip_value_per_lot_usd"], 10.0)
        self.assertAlmostEqual(p["sizing"]["pip_value_usd"], 10.0 / 50)
        self.assertAlmostEqual(p["sizing"]["pip_value_usd"], p["sizing"]["lots"] * 10.0)
        self.assertEqual(p["sizing"]["loss_at_stop_usd"], 50 * p["sizing"]["pip_value_usd"])
        self.assertEqual(p["sizing"]["gain_at_target_usd"], 100 * p["sizing"]["pip_value_usd"])
        for part in ("all", "with_trend", "against_trend"):
            self.assertAlmostEqual(p[part]["avg_result_pips"] * 0.0001,
                                   p["price"] * p[part]["avg_result_pct"] / 100)
            self.assertAlmostEqual(p[part]["week_ended_avg_pips"] * 0.0001,
                                   p["price"] * p[part]["week_ended_avg_pct"] / 100)
        costly = fw.plan("EURUSD", "short", 50, 100, 1000, 1, spread_pips=2)
        self.assertLess(costly["all"]["avg_result_pct"], p["all"]["avg_result_pct"])
        self.assertLess(costly["all"]["avg_result_pips"], p["all"]["avg_result_pips"])
        # the spread is paid on top of the stop and taken off the target; the risk stays 1% of the account
        self.assertAlmostEqual(costly["sizing"]["loss_at_stop_usd"], 10.0, places=6)
        self.assertEqual(costly["sizing"]["loss_at_stop_usd"], 52 * costly["sizing"]["pip_value_usd"])
        self.assertEqual(costly["sizing"]["gain_at_target_usd"], 98 * costly["sizing"]["pip_value_usd"])
        self.assertAlmostEqual(costly["cost_pct"], 0.0002 / p["price"] * 100)
        self.assertAlmostEqual(p["sizing"]["units"] * fw.base_in_usd(self.daily, "EURUSD"),
                               p["sizing"]["position_usd"], places=4)
        self.assertGreater(p["all"]["weeks"], 1000)
        total = p["all"]["target_first"] + p["all"]["stop_first"] + p["all"]["week_ended"]
        self.assertAlmostEqual(total, 1.0)

    def test_usd_base_pair_sizes_in_dollars(self):
        p = fw.plan("USDJPY", "long", 100, 200, 500, 2)
        self.assertAlmostEqual(p["sizing"]["units"], p["sizing"]["position_usd"])

    def test_pip_value_of_a_yen_pair(self):
        p = fw.plan("USDJPY", "long", 100, 200, 500, 2)
        self.assertEqual(p["pip_size"], 0.01)
        self.assertAlmostEqual(p["stop_price"], p["price"] - 1.00)       # 100 yen pips is one yen
        # one pip on a lot is 1,000 yen, worth 1,000 / USDJPY dollars
        self.assertAlmostEqual(p["sizing"]["pip_value_per_lot_usd"], 1000 / p["price"])
        self.assertAlmostEqual(p["sizing"]["loss_at_stop_usd"], 10.0, places=6)
        self.assertEqual(p["sizing"]["loss_at_stop_usd"], 100 * p["sizing"]["pip_value_usd"])
        # a cross quoted in yen has the same pip value per lot: the pip is still 1,000 yen
        cross = fw.plan("GBPJPY", "short", 150, 300, 500, 2)
        self.assertAlmostEqual(cross["sizing"]["pip_value_per_lot_usd"], 1000 / p["price"])
        self.assertAlmostEqual(cross["stop_price"], cross["price"] + 1.50)

    def test_pip_value_of_a_pair_not_quoted_in_dollars(self):
        p = fw.plan("EURGBP", "long", 40, 80, 2000, 1, spread_pips=1.5)
        pound_in_usd = fw.base_in_usd(self.daily, "GBPUSD")
        # one pip on a lot is 10 pounds
        self.assertAlmostEqual(p["sizing"]["pip_value_per_lot_usd"], 10 * pound_in_usd)
        self.assertAlmostEqual(p["sizing"]["pip_value_usd"], p["sizing"]["lots"] * 10 * pound_in_usd)
        self.assertAlmostEqual(p["sizing"]["loss_at_stop_usd"], 20.0, places=6)
        self.assertEqual(p["sizing"]["loss_at_stop_usd"], 41.5 * p["sizing"]["pip_value_usd"])
        self.assertEqual(p["sizing"]["gain_at_target_usd"], 78.5 * p["sizing"]["pip_value_usd"])
        # the position is worth its units of euros in dollars
        self.assertAlmostEqual(p["sizing"]["units"] * fw.base_in_usd(self.daily, "EURGBP"),
                               p["sizing"]["position_usd"], places=4)


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
        body = {"pair": "GBPUSD", "direction": "long", "stop_pips": 60, "target_pips": 120,
                "account_usd": 100, "risk_pct": 1}
        ok = c.post("/api/web/weekly/plan", json=body)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["pair"], "GBPUSD")
        self.assertEqual((ok.json()["stop_pips"], ok.json()["spread_pips"]), (60, 0.0))
        with_spread = c.post("/api/web/weekly/plan", json={**body, "spread_pips": 1.2})
        self.assertEqual(with_spread.json()["spread_pips"], 1.2)

        def refused(change):
            resp = c.post("/api/web/weekly/plan", json={**body, **change})
            self.assertEqual(resp.status_code, 422)
            return resp.json()["detail"]
        self.assertEqual(refused({"pair": "XXXYYY"}), "That pair is not on the list.")
        self.assertEqual(refused({"direction": "up"}), "Choose long or short.")
        self.assertEqual(refused({"stop_pips": 0}), "Enter the stop distance in pips: more than 0 and at most 5000.")
        self.assertEqual(refused({"target_pips": 5001}),
                         "Enter the target distance in pips: more than 0 and at most 5000.")
        self.assertEqual(refused({"spread_pips": 60}),
                         "Enter your broker's spread in pips: 0 or more, and smaller than the stop.")
        refused({"account_usd": 0})


if __name__ == "__main__":
    unittest.main()
