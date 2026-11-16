"""Advanced terminal: the dense panels are computed from live rows and saved tables."""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import market_data  # noqa: E402
import terminal  # noqa: E402
import terminal_adv  # noqa: E402
import weekly_calls  # noqa: E402


def market(symbol, cls, change, volume=1_000_000.0, **over):
    row = {"symbol": symbol, "label": symbol.split(":")[-1], "class": cls, "price": 100.0,
           "day_change_pct": change, "day_volume_usd": volume, "funding_hourly": 0.0000125,
           "open_interest_usd": 5_000_000.0, "max_leverage": 20}
    row.update(over)
    return row


def live(*rows):
    return {r["symbol"]: r for r in rows}


def side(allowed, suggested, liquidated, right=(0.6, 0.55)):
    return {"allowed": allowed, "suggested_leverage": suggested,
            "first_half": {"right": right[0], "avg_pct": 2.0, "coin_weeks": 50},
            "second_half": {"right": right[1], "avg_pct": 1.0, "coin_weeks": 60},
            "by_leverage": {f"{lev}x": {"liquidated": rate, "avg_per_week": 0.01 * lev}
                            for lev, rate in liquidated.items()}}


CONFIG = {"BTC": {"long": side(True, 3, {1: 0.0, 3: 0.0, 10: 0.16}),
                  "short": side(False, 3, {1: 0.0, 3: 0.01, 10: 0.2}, right=(0.52, 0.48))},
          "ETH": {"long": side(True, 3, {1: 0.0, 3: 0.01, 10: 0.32}),
                  "short": side(True, 1, {1: 0.0, 3: 0.05})}}


class MoversAndBreadthTests(unittest.TestCase):
    def test_movers_rank_within_a_class_and_skip_thin_markets(self):
        rows = live(market("A", "crypto", 5.0), market("B", "crypto", 2.0), market("C", "crypto", -1.0),
                    market("D", "crypto", -4.0), market("E", "crypto", 9.0, volume=10_000.0),
                    market("xyz:N", "stocks", 3.0))
        out = terminal_adv.movers(rows)
        self.assertEqual([m["symbol"] for m in out["crypto"]["up"]], ["A", "B"])
        self.assertEqual([m["symbol"] for m in out["crypto"]["down"]], ["D", "C"])
        self.assertEqual(out["crypto"]["count"], 4)                   # the thin market is not counted

    def test_a_market_never_shows_as_both_up_and_down(self):
        out = terminal_adv.movers(live(market("xyz:EUR", "forex", 0.2), market("xyz:JPY", "forex", 0.1)))
        self.assertEqual(len(out["forex"]["up"]), 2)
        self.assertEqual(out["forex"]["down"], [])
        self.assertEqual(out["stocks"], {"up": [], "down": [], "count": 0})

    def test_breadth_counts_small_moves_as_flat(self):
        rows = live(market("A", "crypto", 1.0), market("B", "crypto", 0.05), market("C", "crypto", -0.3),
                    market("D", "crypto", None))
        self.assertEqual(terminal_adv.breadth(rows)["crypto"], {"up": 1, "down": 1, "flat": 1})


class LiveReferenceTests(unittest.TestCase):
    """What the screens need to work figures out again from a live price."""

    def test_universe_lists_every_market_with_what_counting_it_again_needs(self):
        rows = terminal_adv.universe(live(market("BTC", "crypto", 1.0, prev_day_price=99.0),
                                          market("xyz:EUR", "forex", -0.2, volume=10.0)))
        self.assertEqual([r["symbol"] for r in rows], ["BTC", "xyz:EUR"])
        self.assertEqual(list(rows[0]), ["symbol", "label", "class", "price", "prev_day_price", "day_volume_usd"])
        self.assertEqual(rows[0]["prev_day_price"], 99.0)
        self.assertIsNone(rows[1]["prev_day_price"])          # a market without one is still listed
        self.assertEqual(rows[1]["day_volume_usd"], 10.0)

    def test_the_catalogue_carries_the_price_today_is_measured_from(self):
        market_data._cache.clear()
        answers = {None: [{"universe": [{"name": "ETH", "maxLeverage": 25}]},
                          [{"markPx": "1818.0", "prevDayPx": "1800.0", "funding": "0.00001", "openInterest": "10",
                            "dayNtlVlm": "5"}]],
                   "xyz": [{"universe": []}, []]}
        with mock.patch.object(market_data, "_post", lambda payload: answers[payload.get("dex")]):
            eth = market_data.catalogue()["ETH"]
        market_data._cache.clear()
        self.assertEqual(eth["prev_day_price"], 1800.0)
        self.assertAlmostEqual(eth["day_change_pct"], (eth["price"] / eth["prev_day_price"] - 1) * 100)


class RiskTests(unittest.TestCase):
    def position(self, **over):
        p = {"coin": "ETH", "side": "short", "status": "open", "leverage": 3, "share": 4.5,
             "position_usd": 13.5, "pnl_usd": 1.2, "entry": 2000.0, "last": 1800.0, "stop": 2180.0,
             "liquidation": 2620.0, "stop_distance_pct": 21.1, "normal_week_pct": 9.0}
        p.update(over)
        return p

    def test_open_position_exposure_and_loss_at_the_stop(self):
        out = terminal_adv.risk([self.position()], 18.0)
        row = out["rows"][0]
        self.assertAlmostEqual(row["loss_at_stop_usd"], 4.5 * 3 * (0.09 + 2 * weekly_calls.FEE_PER_SIDE))
        self.assertAlmostEqual(row["liquidation_distance_pct"], (2620 - 1800) / 1800 * 100)
        self.assertAlmostEqual(out["exposure_usd"], 13.5)
        self.assertAlmostEqual(out["exposure_of_stake"], 0.75)
        self.assertAlmostEqual(out["worst_case_usd"], -row["loss_at_stop_usd"])
        self.assertAlmostEqual(out["in_cash_usd"], 13.5)

    def test_loss_at_the_stop_is_capped_at_the_stake(self):
        far = terminal_adv.risk([self.position(leverage=20, stop=2400.0)], 18.0)["rows"][0]
        self.assertEqual(far["loss_at_stop_usd"], 4.5)
        no_stop = terminal_adv.risk([self.position(stop=None)], 18.0)["rows"][0]
        self.assertEqual(no_stop["loss_at_stop_usd"], 4.5)

    def test_closed_positions_carry_no_exposure(self):
        out = terminal_adv.risk([self.position(status="stopped", pnl_usd=-1.2)], 18.0)
        self.assertEqual((out["exposure_usd"], out["worst_case_usd"], out["in_cash_usd"]), (0.0, 0.0, 18.0))
        self.assertIsNone(out["rows"][0]["liquidation_distance_pct"])
        self.assertEqual(terminal_adv.risk([], 18.0)["rows"], [])


class SavedTableTests(unittest.TestCase):
    def test_leverage_table_lists_only_called_sides(self):
        with mock.patch.object(weekly_calls, "calls_config", lambda: CONFIG):
            out = terminal_adv.leverage_table()
        self.assertEqual(out["levels"], [1, 3, 10])
        self.assertEqual([(r["coin"], r["side"]) for r in out["rows"]],
                         [("BTC", "long"), ("ETH", "long"), ("ETH", "short")])
        btc, _, eth_short = out["rows"]
        self.assertEqual((btc["suggested"], btc["liquidated"]["10"], btc["avg"]["3"]), (3, 0.16, 0.03))
        self.assertIsNone(eth_short["liquidated"]["10"])              # a level that was never tested

    def test_signal_matrix_marks_active_and_skipped_sides(self):
        week = {"coins": [{"coin": "BTC", "side": None, "signal": "short"},
                          {"coin": "ETH", "side": "short", "signal": "short"}]}
        with mock.patch.object(weekly_calls, "calls_config", lambda: CONFIG):
            rows = {(r["coin"], r["side"]): r for r in terminal_adv.signal_matrix(week)}
        self.assertEqual(len(rows), 4)
        skipped = rows[("BTC", "short")]
        self.assertEqual((skipped["allowed"], skipped["active"], skipped["pointed"]), (False, False, True))
        active = rows[("ETH", "short")]
        self.assertEqual((active["allowed"], active["active"], active["leverage"]), (True, True, 1))
        self.assertEqual((active["right_first"], active["right_second"], active["weeks"]), (0.6, 0.55, 110))

    def test_funding_is_annualised_and_sorted_by_size(self):
        rows = live(market("BTC", "crypto", 1.0, funding_hourly=0.00001),
                    market("SUI", "crypto", 1.0, funding_hourly=-0.00002),
                    market("xyz:NVDA", "stocks", 1.0, funding_hourly=0.001))
        out = terminal_adv.funding(rows)
        self.assertEqual([r["symbol"] for r in out], ["SUI", "BTC"])
        self.assertAlmostEqual(out[0]["funding_apr"], -0.00002 * 24 * 365 * 100)


class SeriesTests(unittest.TestCase):
    def setUp(self):
        market_data._cache.clear()

    def bars(self, close):
        close = np.asarray(close, dtype=float)
        return {"t": np.arange(len(close)) * 14_400_000, "close": close}

    def test_performance_is_rebased_to_100(self):
        ramp = np.linspace(50.0, 100.0, 30 * terminal_adv.BARS_PER_DAY + 1)
        with mock.patch.object(market_data, "candles", lambda symbol: self.bars(ramp)):
            out = terminal_adv.performance()
        self.assertEqual(set(out), {market_data.label(s) for s in terminal_adv.PERFORMANCE})
        series = out["BTC"]
        self.assertEqual(len(series), 61)
        self.assertAlmostEqual(series[0], 100.0)
        self.assertAlmostEqual(series[-1], 200.0)
        # where each line starts, so a screen can carry it on to the live price
        with mock.patch.object(market_data, "candles", lambda symbol: self.bars(ramp)):
            start = terminal_adv.performance_start()
        self.assertEqual(set(start), set(out))
        self.assertEqual(start["BTC"], {"symbol": "BTC", "price": 50.0})
        self.assertAlmostEqual(75.0 / start["BTC"]["price"] * 100, 150.0)     # a live price of 75 puts the line at 150

    def test_worksheet_figures_carry_the_prices_they_are_measured_from(self):
        close = np.linspace(80.0, 120.0, 400)
        row = {"label": "BTC", "class": "crypto", "trend": "up", "trend_days": 3.0, "atr_pct": 1.0,
               "signal": {"direction": "long", "state": "active"}, "record": {"wins": 1, "trades": 2, "expectancy": 0.1}}
        with mock.patch.object(market_data, "candles", lambda symbol: self.bars(close)), \
                mock.patch.object(terminal_adv.desk, "market_signal", lambda symbol: row):
            stats = terminal_adv._stats("BTC")
        week, month = 7 * terminal_adv.BARS_PER_DAY, 30 * terminal_adv.BARS_PER_DAY
        self.assertEqual(stats["week_ref_price"], close[-week - 1])
        self.assertEqual(stats["month_ref_price"], close[-month - 1])
        self.assertAlmostEqual((close[-1] / stats["week_ref_price"] - 1) * 100, stats["week_pct"])
        self.assertAlmostEqual((close[-1] / stats["month_ref_price"] - 1) * 100, stats["month_pct"])
        short = self.bars(close[:20])
        with mock.patch.object(market_data, "candles", lambda symbol: short), \
                mock.patch.object(terminal_adv.desk, "market_signal", lambda symbol: row):
            self.assertIsNone(terminal_adv._stats("BTC")["week_ref_price"])

    def test_performance_skips_markets_with_too_little_history(self):
        def candles(symbol):
            if symbol == "BTC":
                raise market_data.DataError("down")
            return self.bars(np.linspace(1.0, 2.0, 40))
        with mock.patch.object(market_data, "candles", candles):
            self.assertEqual(terminal_adv.performance(), {})

    def test_correlation_of_markets_that_move_together_and_apart(self):
        rng = np.random.default_rng(7)
        base = np.exp(np.cumsum(rng.normal(0, 0.01, 200)))

        def candles(symbol):
            if symbol == "ETH":
                return self.bars(base * 3)                           # same moves, different price
            if symbol == "SOL":
                return self.bars(1 / base)                           # exactly opposite
            return self.bars(base)
        with mock.patch.object(market_data, "candles", candles):
            out = terminal_adv.correlation()
        i = {label: n for n, label in enumerate(out["labels"])}
        self.assertAlmostEqual(out["matrix"][i["BTC"]][i["ETH"]], 1.0)
        self.assertAlmostEqual(out["matrix"][i["BTC"]][i["SOL"]], -1.0)
        self.assertEqual(out["bars"], 30 * terminal_adv.BARS_PER_DAY + 1)

    def test_correlation_is_empty_without_shared_history(self):
        with mock.patch.object(market_data, "candles", lambda symbol: self.bars(np.linspace(1, 2, 10))):
            self.assertEqual(terminal_adv.correlation(), {"labels": [], "matrix": []})

    def test_order_book_parses_levels_and_limits_depth(self):
        raw = {"time": 5, "levels": [[{"px": str(100 - i), "sz": "2", "n": 1} for i in range(20)],
                                     [{"px": str(101 + i), "sz": "3", "n": 1} for i in range(20)]]}
        with mock.patch.object(market_data, "_post", lambda payload: raw):
            book = market_data.order_book("ETH", depth=5)
        self.assertEqual((len(book["bids"]), len(book["asks"])), (5, 5))
        self.assertEqual((book["bids"][0], book["asks"][0]), ({"px": 100.0, "sz": 2.0}, {"px": 101.0, "sz": 3.0}))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        market_data._cache.clear()

    def test_one_broken_panel_does_not_take_the_page_down(self):
        rows = live(market("BTC", "crypto", 1.0), market("ETH", "crypto", -2.0))
        week = {"coins": [{"coin": "BTC", "side": "long", "signal": "long"},
                          {"coin": "ETH", "side": None, "signal": None}]}

        def broken(*args, **kwargs):
            raise RuntimeError("source is down")
        with mock.patch.object(market_data, "catalogue", lambda *a, **k: rows), \
                mock.patch.object(terminal, "snapshot", lambda stake, now: {"positions": []}), \
                mock.patch.object(weekly_calls, "this_week", lambda: week), \
                mock.patch.object(weekly_calls, "calls_config", lambda: CONFIG), \
                mock.patch.object(weekly_calls, "scoreboard", broken), \
                mock.patch.object(weekly_calls, "exit_rule", broken), \
                mock.patch.object(terminal_adv, "worksheet", broken), \
                mock.patch.object(terminal_adv, "performance", broken), \
                mock.patch.object(terminal_adv, "correlation", broken), \
                mock.patch.object(terminal_adv.forex_weekly, "overview", broken), \
                mock.patch.object(terminal_adv.crypto_mood, "history", broken):
            out = terminal_adv.snapshot(18.0)
        self.assertEqual(out["worksheet"], [])
        self.assertEqual(out["performance"], {})
        self.assertEqual(out["correlation"], {"labels": [], "matrix": []})
        self.assertIsNone(out["scoreboard"])
        self.assertIsNone(out["exits"])
        self.assertEqual((out["forex"], out["mood_history"]), ([], []))
        # the panels whose sources are up still arrive
        self.assertEqual(out["breadth"]["crypto"], {"up": 1, "down": 1, "flat": 0})
        self.assertEqual(len(out["signals"]), 4)
        self.assertEqual(out["risk"]["in_cash_usd"], 18.0)
        self.assertEqual(out["markets"], 2)


class AdvancedEndpointTests(unittest.TestCase):
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

    def test_advanced_and_book_need_a_session_and_validate(self):
        fresh = type(self.client)(self.client.app)
        self.assertEqual(fresh.get("/api/web/terminal/advanced").status_code, 401)
        self.assertEqual(fresh.get("/api/web/terminal/book?symbol=ETH").status_code, 401)
        c = self.client
        c.post("/api/web/login", json={"password": "correct horse"})
        with mock.patch.object(terminal_adv, "snapshot", lambda stake: {"stake": stake}):
            self.assertEqual(c.get("/api/web/terminal/advanced?stake=25").json(), {"stake": 25.0})
        self.assertEqual(c.get("/api/web/terminal/advanced?stake=-1").status_code, 422)

        def down(stake):
            raise market_data.DataError("Hyperliquid did not answer.")
        with mock.patch.object(terminal_adv, "snapshot", down):
            self.assertEqual(c.get("/api/web/terminal/advanced").status_code, 502)
        with mock.patch.object(market_data, "catalogue", lambda *a, **k: {"ETH": {}}), \
                mock.patch.object(market_data, "order_book", lambda symbol: {"symbol": symbol, "bids": [], "asks": []}):
            self.assertEqual(c.get("/api/web/terminal/book?symbol=ETH").json()["symbol"], "ETH")
            self.assertEqual(c.get("/api/web/terminal/book?symbol=NOPE").status_code, 422)


if __name__ == "__main__":
    unittest.main()
