"""Desk: the rule replay, per-market trust, muting, and the watchlist endpoints."""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

SERVER_DIR = Path(__file__).resolve().parents[1]
ROOT = SERVER_DIR.parents[1]
for path in (SERVER_DIR, ROOT / "research" / "scripts", ROOT / "service" / "agent",
             ROOT / "service" / "tg_bot"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import desk  # noqa: E402
import market_data  # noqa: E402


def bars_from_csv(coin: str) -> dict:
    import strategy_search as ss
    df = ss.load(coin, "4h")
    return {"t": (df["t"].astype("int64") // 10**6).to_numpy(),
            "open": df["open"].to_numpy(float), "high": df["high"].to_numpy(float),
            "low": df["low"].to_numpy(float), "close": df["close"].to_numpy(float)}, df


def synthetic(closes) -> dict:
    c = np.array(closes, dtype=float)
    return {"t": np.arange(len(c), dtype=np.int64) * market_data.H4_MS, "open": c.copy(),
            "high": c * 1.01, "low": c * 0.99, "close": c}


class ReplayTests(unittest.TestCase):
    def test_matches_the_research_backtest(self):
        """The desk's replay and strategy_search.py must agree trade for trade."""
        import strategy_search as ss
        for coin in ("BTC", "SUI"):
            bars, df = bars_from_csv(coin)
            mine = desk.replay(bars, warmup=1)["trades"]
            theirs = ss.simulate(df, ss.ema_trend(df, 60, 240, 6), False)
            self.assertEqual(len(mine), len(theirs))
            for a, b in zip(mine, theirs):
                self.assertAlmostEqual(a["gross_pct"], b["gross"], places=6)

    def test_warmup_skips_the_unsettled_start(self):
        bars, _ = bars_from_csv("BTC")
        full = desk.replay(bars, warmup=1)["trades"]
        settled = desk.replay(bars)["trades"]
        self.assertLess(len(settled), len(full))
        first_allowed = desk._iso(bars["t"][desk.SLOW_BARS])
        self.assertTrue(all(t["opened_at"] >= first_allowed for t in settled))

    def test_fresh_cross_is_pending(self):
        closes = [200 - i * 0.2 for i in range(400)]
        while desk.replay(synthetic(closes))["trend"] == "down":
            closes.append(closes[-1] * 1.01)
        run = desk.replay(synthetic(closes))
        self.assertEqual(run["pending"]["direction"], "long")
        self.assertIsNone(run["open"])
        later = desk.replay(synthetic(closes + [closes[-1] * 1.01]))
        self.assertIsNone(later["pending"])
        self.assertEqual(later["open"]["dir"], 1)


class TrustTests(unittest.TestCase):
    def rows(self, direction, results):
        return [{"direction": direction, "gross_pct": r} for r in results]

    def test_few_trades_are_unproven(self):
        self.assertEqual(desk.trust(self.rows("long", [5, 5, 5]), "long")["state"], "unproven")

    def test_losing_side_is_muted_and_winning_side_trusted(self):
        rows = self.rows("short", [-3, -3, -3, -3, 4]) + self.rows("long", [-1, -1, -1, -1, 20])
        self.assertEqual(desk.trust(rows, "short")["state"], "muted")
        self.assertEqual(desk.trust(rows, "long")["state"], "trusted")

    def test_costs_count(self):
        # five trades that each make less than the round-trip cost are a loss
        rows = self.rows("long", [desk.COST_PCT / 2] * 5)
        self.assertEqual(desk.trust(rows, "long")["state"], "muted")

    def test_lessons_and_allow_filter(self):
        row = {"symbol": "xyz:NVDA", "label": "NVDA",
               "trust": {"long": {"state": "muted", "trades": 8, "wins": 1, "total_net_pct": -8.5},
                         "short": {"state": "trusted", "trades": 6, "wins": 3, "total_net_pct": 9.0}}}
        lessons = desk.lessons([row, {"symbol": "X", "error": "no data"}])
        self.assertEqual(len(lessons), 1)
        self.assertIn("Longs on NVDA are muted", lessons[0]["text"])
        allow = desk.allowed([row])
        self.assertFalse(allow("xyz:NVDA", "long"))
        self.assertTrue(allow("xyz:NVDA", "short"))
        self.assertTrue(allow("BTC", "long"))


class MutedSignalIsNotPaperTraded(unittest.TestCase):
    def test_veto_blocks_the_entry(self):
        import trend_agent as ta
        from paper_gateway import PaperGateway
        from paper_store import PaperStore
        closes = [200 - i * 0.2 for i in range(400)]
        while ta.signal([{"t": i, "o": c, "h": c, "l": c, "c": c} for i, c in enumerate(closes)])["trend"] <= 0:
            closes.append(closes[-1] * 1.01)
        bars = [{"t": i * ta.BAR_MS, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c}
                for i, c in enumerate(closes)]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ta, "EXPORT_DIR", Path(tmp)):
            gw = PaperGateway(PaperStore(str(Path(tmp) / "r.db")))
            gw.store.ensure_portfolio(3)
            lines = ta.run_cycle(3, ["BTC"], gw, ta.Journal(3), fetch_bars=lambda s: bars,
                                 fetch_mids=lambda: {"BTC": closes[-1]},
                                 allow=lambda symbol, direction: False)
            self.assertEqual(gw.store.positions(3), [])
            self.assertTrue(any("muted" in line for line in lines))
            ta.run_cycle(3, ["BTC"], gw, ta.Journal(3), fetch_bars=lambda s: bars,
                         fetch_mids=lambda: {"BTC": closes[-1]}, allow=lambda s, d: True)
            self.assertEqual(len(gw.store.positions(3)), 1)


class CryptoMoodTests(unittest.TestCase):
    def test_rule_directions(self):
        import crypto_mood
        self.assertEqual(crypto_mood.rule_direction(10), {"with_mood": "short", "extreme": "short"})
        self.assertEqual(crypto_mood.rule_direction(49), {"with_mood": "short", "extreme": None})
        self.assertEqual(crypto_mood.rule_direction(50), {"with_mood": "long", "extreme": None})
        self.assertEqual(crypto_mood.rule_direction(80), {"with_mood": "long", "extreme": "long"})

    def test_week_uses_sundays_reading_and_carries_the_record(self):
        import crypto_mood
        from datetime import date
        readings = [  # newest first; 2026-11-15 is a Sunday
            {"date": date(2026, 11, 17), "value": 30, "label": "Fear"},
            {"date": date(2026, 11, 16), "value": 20, "label": "Extreme Fear"},
            {"date": date(2026, 11, 15), "value": 60, "label": "Greed"},
            {"date": date(2026, 11, 14), "value": 55, "label": "Greed"},
            {"date": date(2026, 11, 8), "value": 40, "label": "Fear"},
        ]
        with mock.patch.object(crypto_mood, "_readings", lambda: readings):
            week = crypto_mood.this_week()
        self.assertEqual(week["week_reading"]["value"], 60)
        self.assertEqual(week["today"]["value"], 30)
        self.assertEqual(week["rules"]["with_mood"], "long")
        self.assertEqual(week["change_from_last_week"], 20)
        ev = week["evidence"]
        self.assertGreater(ev["with_mood"]["choose"]["accuracy"], 0.5)
        self.assertGreater(ev["with_mood"]["check"]["avg_pct"], ev["holding"]["check"]["avg_pct"])


    def test_on_a_sunday_the_week_is_still_last_sundays(self):
        import crypto_mood
        from datetime import date
        readings = [{"date": date(2026, 11, 15), "value": 10, "label": "Extreme Fear"},   # today, a Sunday
                    {"date": date(2026, 11, 14), "value": 12, "label": "Extreme Fear"},
                    {"date": date(2026, 11, 8), "value": 28, "label": "Fear"},
                    {"date": date(2026, 11, 1), "value": 45, "label": "Fear"}]
        with mock.patch.object(crypto_mood, "_readings", lambda: readings):
            week = crypto_mood.this_week()
        self.assertEqual(week["week_reading"]["date"], "2026-11-08")
        self.assertEqual(week["today"]["value"], 10)
        self.assertEqual(week["change_from_last_week"], -17)


def wc_fee():
    import weekly_calls
    return weekly_calls.FEE_PER_SIDE


CONFIG = {  # the shape research/scripts/crypto_long_short.py writes
    coin: {
        "long": {"allowed": True, "suggested_leverage": 3,
                 "first_half": {"right": 0.6, "avg_pct": 3.0, "coin_weeks": 60},
                 "second_half": {"right": 0.55, "avg_pct": 1.5, "coin_weeks": 60},
                 "by_leverage": {f"{lev}x": {"ahead": 0.55, "liquidated": 0.7 if lev >= 20 else 0.0,
                                             "avg_per_week": 0.05, "weeks": 120}
                                 for lev in (1, 2, 3, 5, 7, 10, 20, 30)}},
        "short": {"allowed": coin != "BTC", "suggested_leverage": 3,
                  "first_half": {"right": 0.52, "avg_pct": 0.7, "coin_weeks": 60},
                  "second_half": {"right": 0.48, "avg_pct": 0.0, "coin_weeks": 45},
                  "by_leverage": {f"{lev}x": {"ahead": 0.5, "liquidated": 0.0, "avg_per_week": 0.01,
                                              "weeks": 100} for lev in (1, 2, 3, 5, 7, 10, 20, 30)}},
    } for coin in ("BTC", "ETH")
}


class WeeklyCallsTests(unittest.TestCase):
    def closes(self, now, four_weeks_ago):
        from datetime import date, timedelta
        start = date(2026, 11, 8)
        prices = [now, 1, 1, 1, four_weeks_ago, 1]
        return [{"date": start - timedelta(days=7 * i), "close": p} for i, p in enumerate(prices)]

    def test_signal_needs_mood_and_four_weeks_to_agree(self):
        import weekly_calls as wc
        self.assertEqual(wc.signal(60, 110, 100), "long")
        self.assertEqual(wc.signal(40, 90, 100), "short")
        self.assertIsNone(wc.signal(60, 90, 100))        # greedy but falling
        self.assertIsNone(wc.signal(40, 110, 100))       # fearful but rising
        self.assertEqual(wc.signal(50, 110, 100), "long")
        self.assertEqual(wc.signal(49, 90, 100), "short")

    def test_liquidation_distance(self):
        import weekly_calls as wc
        self.assertEqual(wc.liquidation_distance(1, 40), 1.0)
        self.assertAlmostEqual(wc.liquidation_distance(3, 25), 1 / 3 - 1 / 50)
        self.assertAlmostEqual(wc.liquidation_distance(30, 40), 1 / 30 - 1 / 80)

    def test_real_evidence_file_has_the_expected_shape(self):
        import weekly_calls as wc
        config = wc.calls_config()
        for coin in config:
            for side in ("long", "short"):
                c = config[coin][side]
                self.assertIn("allowed", c)
                if c["allowed"]:        # a called side always has a tested, rarely-liquidated leverage
                    self.assertIn(f"{c['suggested_leverage']}x", c["by_leverage"])
                    self.assertLessEqual(c["by_leverage"][f"{c['suggested_leverage']}x"]["liquidated"], 0.02)
        self.assertTrue(config["BTC"]["long"]["allowed"])
        self.assertFalse(config["BTC"]["short"]["allowed"])
        self.assertEqual(set(config), {"BTC", "ETH", "SOL", "SUI"})

    def test_preview_uses_todays_mood_and_live_price(self):
        import weekly_calls as wc
        mood = {"today": {"value": 10, "label": "Extreme Fear", "date": "2026-11-15"}}
        live = {"BTC": {"price": 90.0}, "ETH": {"price": 90.0}}
        closes = self.closes(1, 1)
        closes[3]["close"] = 100.0                       # four weeks before the coming week's end
        with mock.patch.object(wc, "sunday_closes", lambda coin: closes):
            out = wc.preview(CONFIG, mood, live)
        by = {c["coin"]: c for c in out["coins"]}
        self.assertEqual((by["ETH"]["side"], by["ETH"]["leverage"]), ("short", 3))
        self.assertEqual((by["BTC"]["side"], by["BTC"]["skipped"]), (None, True))     # short Bitcoin is not called
        self.assertAlmostEqual(by["ETH"]["four_week_change_pct"], -10.0)
        self.assertIsNone(wc.preview(CONFIG, {}, live))

    EXITS = {"stop": 1.0, "optional_target": 3.0, "with": None, "holding": None, "by_call": {}}

    def _week(self, mood_value, label, closes_by_coin, normal=0.05, monday_open=None):
        import crypto_mood
        import weekly_calls as wc
        from datetime import date
        mood = {"week_reading": {"value": mood_value, "label": label, "date": "2026-11-08"}}
        live = {"BTC": {"price": 100.0, "max_leverage": 40}, "ETH": {"price": 50.0, "max_leverage": 25}}
        candles = {date(2026, 11, 9): {"open": monday_open}} if monday_open else {}
        with mock.patch.object(crypto_mood, "this_week", lambda: mood), \
                mock.patch.object(market_data, "catalogue", lambda: live), \
                mock.patch.object(wc, "calls_config", lambda: CONFIG), \
                mock.patch.object(wc, "exit_rule", lambda: self.EXITS), \
                mock.patch.object(wc, "daily_candles", lambda coin: candles), \
                mock.patch.object(wc, "normal_week", lambda candles, sunday: normal), \
                mock.patch.object(wc, "sunday_closes", lambda coin: closes_by_coin[coin]):
            return wc.this_week(), wc.one_line()

    def test_fearful_and_falling_shorts_ether_but_skips_bitcoin(self):
        down = self.closes(90, 100)
        week, line = self._week(28, "Fear", {"BTC": down, "ETH": down})
        self.assertEqual(week["calls"], [{"coin": "ETH", "side": "short", "leverage": 3}])
        btc, eth = week["coins"]
        self.assertTrue(btc["skipped"])
        self.assertIsNone(btc["side"])
        self.assertEqual(btc["signal"], "short")
        # a short is liquidated above the entry price, and its stop sits above too
        self.assertAlmostEqual(eth["liquidation_price"], 50.0 * (1 + 1 / 3 - 1 / 50))
        self.assertAlmostEqual(eth["stop_price"], 50.0 * 1.05)          # one normal week (5%)
        self.assertAlmostEqual(eth["target_price"], 50.0 * 0.85)        # optional, three normal weeks
        self.assertAlmostEqual(eth["stake_lost_at_stop"], 3 * (0.05 + 2 * wc_fee()))
        self.assertLess(eth["stop_price"], eth["liquidation_price"])
        self.assertEqual((week["week_of"], week["next_call"]), ("2026-11-09", "2026-11-16"))
        self.assertIn("short ETH at 3x", line)

    def test_levels_use_mondays_open_once_it_exists(self):
        down = self.closes(90, 100)
        week, _ = self._week(28, "Fear", {"BTC": down, "ETH": down}, monday_open=60.0)
        eth = week["coins"][1]
        self.assertEqual(eth["entry"], 60.0)
        self.assertAlmostEqual(eth["stop_price"], 63.0)

    def test_greedy_and_rising_goes_long_and_mixed_stays_out(self):
        up, down = self.closes(110, 100), self.closes(90, 100)
        week, line = self._week(70, "Greed", {"BTC": up, "ETH": down})
        self.assertEqual(week["calls"], [{"coin": "BTC", "side": "long", "leverage": 3}])
        self.assertLess(week["coins"][0]["liquidation_price"], 100.0)   # a long is liquidated below
        self.assertAlmostEqual(week["coins"][0]["stop_price"], 95.0)    # and stopped below
        self.assertFalse(week["coins"][1]["skipped"])                   # no signal at all
        week, line = self._week(70, "Greed", {"BTC": down, "ETH": down})
        self.assertEqual(week["calls"], [])
        self.assertIn("stay out", line)

    def week(self, monday, moves, before=100.0, month_ago=90.0):
        """Daily candles for one week opening at 100: (high, low, close) moves in percent."""
        from datetime import timedelta
        flat = lambda v: {"open": v, "high": v, "low": v, "close": v}
        candles = {monday - timedelta(days=1): flat(before), monday - timedelta(days=29): flat(month_ago)}
        for i, (high, low, close) in enumerate(moves):
            candles[monday + timedelta(days=i)] = {
                "open": 100.0, "high": 100.0 * (1 + high / 100), "low": 100.0 * (1 + low / 100),
                "close": 100.0 * (1 + close / 100)}
        return candles

    def test_stop_loss_ends_a_week_early(self):
        import weekly_calls as wc
        from datetime import date
        monday = date(2026, 9, 14)
        # dips 6% on day two, then recovers to end +4%
        dip = self.week(monday, [(0.5, -0.5, 0.2), (0.5, -6.0, -1.0)] + [(0.5, -0.5, 0.2)] * 4 + [(4.5, -0.5, 4.0)])
        with mock.patch.object(wc, "exit_rule", lambda: self.EXITS), \
                mock.patch.object(wc, "normal_week", lambda candles, sunday: 0.05):
            stopped = wc.week_result(dip, monday, 60, 9.0, wc.SUGGESTED, 25, CONFIG["ETH"])
        self.assertTrue(stopped["stopped"])
        self.assertFalse(stopped["wiped"])
        # loses the stop distance (5%) at 3x, with fees and two days of funding
        self.assertAlmostEqual(stopped["result"],
                               9.0 * 3 * (-0.05 - 2 * wc.FEE_PER_SIDE - 2 * wc.FUNDING_PER_DAY))
        with mock.patch.object(wc, "exit_rule", lambda: {**self.EXITS, "stop": None}):
            held = wc.week_result(dip, monday, 60, 9.0, wc.SUGGESTED, 25, CONFIG["ETH"])
        self.assertFalse(held["stopped"])
        self.assertGreater(held["result"], 0)

    def test_week_result_long_short_and_liquidation(self):
        import weekly_calls as wc
        from datetime import date
        self.enterContext(mock.patch.object(wc, "exit_rule", lambda: {**self.EXITS, "stop": None}))
        monday = date(2026, 9, 14)
        rise = self.week(monday, [(0.5, -0.5, 0.2)] * 6 + [(4.5, -0.5, 4.0)])            # ends +4%
        fall = self.week(monday, [(0.5, -0.5, -0.2)] * 6 + [(0.5, -4.5, -4.0)], 100, 110)  # ends -4%
        eth = CONFIG["ETH"]
        # long at the suggested 3x: three times the move, less fees and funding
        long = wc.week_result(rise, monday, 60, 9.0, wc.SUGGESTED, 25, eth)
        self.assertEqual((long["side"], long["leverage_used"]), ("long", 3.0))
        self.assertAlmostEqual(long["result"],
                               9.0 * 3 * (0.04 - 2 * wc.FEE_PER_SIDE - 7 * wc.FUNDING_PER_DAY))
        # short profits from the fall and pays no funding
        short = wc.week_result(fall, monday, 40, 9.0, wc.SUGGESTED, 25, eth)
        self.assertEqual(short["side"], "short")
        self.assertAlmostEqual(short["result"], 9.0 * 3 * (0.04 - 2 * wc.FEE_PER_SIDE))
        # shorting Bitcoin is not called: no trade, flagged as skipped
        skipped = wc.week_result(fall, monday, 40, 9.0, wc.SUGGESTED, 40, CONFIG["BTC"])
        self.assertEqual((skipped["side"], skipped["skipped"], skipped["result"]), (None, True, 0.0))
        # mood and price disagree: no trade, not skipped
        none = wc.week_result(fall, monday, 60, 9.0, wc.SUGGESTED, 25, eth)
        self.assertEqual((none["side"], none["skipped"]), (None, False))
        # a 3% spike against a 30x short liquidates it even though the week ends in its favour
        spike = self.week(monday, [(3.0, -0.5, -0.2)] + [(0.5, -0.5, -0.2)] * 5 + [(0.5, -4.5, -4.0)],
                          100, 110)
        wiped = wc.week_result(spike, monday, 40, 9.0, 30, 40, CONFIG["ETH"])
        self.assertTrue(wiped["wiped"])
        self.assertEqual(wiped["result"], -9.0)
        self.assertFalse(wc.week_result(spike, monday, 40, 9.0, 3, 40, CONFIG["ETH"])["wiped"])
        # leverage above the exchange's limit is capped
        self.assertEqual(wc.week_result(rise, monday, 60, 9.0, 30, 25, eth)["leverage_used"], 25)
        # missing days: the week is not scored
        short_week = dict(rise)
        short_week.pop(monday)
        self.assertFalse(wc.week_result(short_week, monday, 60, 9.0, 1, 40, eth)["known"])

    def test_scoreboard_totals(self):
        import crypto_mood
        import weekly_calls as wc
        from datetime import date, timedelta
        mondays = [date(2026, 9, 14), date(2026, 9, 7)]
        # The older week's last day is also the newer week's "Sunday before", so
        # build the newer week first and let the older week's real days win.
        candles = {}
        candles.update(self.week(mondays[0], [(0.5, -0.5, 0.2)] * 6 + [(4.5, -0.5, 4.0)]))     # +4%
        candles.update(self.week(mondays[1], [(0.5, -0.5, 0.2)] * 6 + [(0.5, -3.5, -3.0)]))    # -3%
        moods = {m - timedelta(days=1): 60 for m in mondays}
        live = {"BTC": {"max_leverage": 40}, "ETH": {"max_leverage": 25}}
        with mock.patch.object(crypto_mood, "history", lambda: moods), \
                mock.patch.object(market_data, "catalogue", lambda: live), \
                mock.patch.object(wc, "calls_config", lambda: CONFIG), \
                mock.patch.object(wc, "exit_rule", lambda: {**self.EXITS, "stop": None}), \
                mock.patch.object(wc, "daily_candles", lambda coin: candles):
            board = wc.scoreboard(18, 1, weeks=2)
            high = wc.scoreboard(18, 30, weeks=2)
            suggested = wc.scoreboard(18, wc.SUGGESTED, weeks=2)
        self.assertEqual([r["week_of"] for r in board["rows"]], ["2026-09-14", "2026-09-07"])
        self.assertEqual((board["ahead"], board["behind"], board["no_trade"]), (1, 1, 0))
        self.assertAlmostEqual(board["rows"][1]["running_total"], board["rows"][1]["result"])
        self.assertAlmostEqual(board["rows"][0]["running_total"], board["total"])
        self.assertAlmostEqual(board["total"], 18 * (0.04 - 0.03 - 4 * wc.FEE_PER_SIDE))
        self.assertEqual(board["rows"][0]["calls"][0], {"coin": "BTC", "side": "long", "leverage": 1.0})
        self.assertEqual(high["capped"], {"ETH": 25})
        self.assertEqual({(e["coin"], e["leverage"]) for e in high["expect"] if e["side"] == "long"},
                         {("BTC", 30), ("ETH", 25)})
        self.assertTrue(all(c["leverage"] == 3.0 for r in suggested["rows"] for c in r["calls"]))

    def test_mismatched_weeks_are_refused(self):
        import crypto_mood
        import weekly_calls as wc
        mood = {"week_reading": {"value": 70, "label": "Greed", "date": "2026-11-15"}}
        live = {"BTC": {"price": 1.0, "max_leverage": 40}, "ETH": {"price": 1.0, "max_leverage": 25}}
        with mock.patch.object(crypto_mood, "this_week", lambda: mood), \
                mock.patch.object(market_data, "catalogue", lambda: live), \
                mock.patch.object(wc, "calls_config", lambda: CONFIG), \
                mock.patch.object(wc, "sunday_closes", lambda coin: self.closes(110, 100)):
            with self.assertRaises(market_data.DataError):
                wc.this_week()

    def test_normal_week_is_the_spread_of_recent_weekly_moves(self):
        import weekly_calls as wc
        from datetime import date, timedelta
        sunday = date(2026, 11, 8)
        price, candles = 100.0, {}
        for k in range(wc.VOL_WEEKS, -1, -1):                   # alternate +2% and -2% weeks
            candles[sunday - timedelta(days=7 * k)] = {"close": price}
            price *= 1.02 if k % 2 else 0.98
        normal = wc.normal_week(candles, sunday)
        self.assertAlmostEqual(normal, 0.02, delta=0.002)
        candles.pop(sunday - timedelta(days=7 * 5))
        self.assertIsNone(wc.normal_week(candles, sunday))      # a missing week: no estimate

    def test_real_exit_rule_file(self):
        import weekly_calls as wc
        rule = wc.exit_rule()
        self.assertEqual(rule["stop"], 1.0)
        self.assertGreater(rule["with"]["worst"], rule["holding"]["worst"])    # the stop caps the worst week
        self.assertGreater(rule["with"]["avg"], 0)


class DeskEndpointTests(unittest.TestCase):
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
        bars, _ = bars_from_csv("BTC")
        live = {"symbol": "BTC", "label": "BTC", "class": "crypto", "price": float(bars["close"][-1]),
                "day_change_pct": 1.0, "funding_hourly": 0.0, "max_leverage": 40,
                "day_volume_usd": 1.0}
        import crypto_mood
        cls.patches = [mock.patch.object(market_data, "catalogue", lambda: {"BTC": live}),
                       mock.patch.object(market_data, "candles", lambda symbol: bars),
                       mock.patch.object(crypto_mood, "this_week", lambda: {"marker": "mood"})]
        for p in cls.patches:
            p.start()
        import routes_web
        cls.routes_web = importlib.reload(routes_web)
        app = FastAPI()
        cls.routes_web.register_web_routes(app)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def test_desk_needs_a_session(self):
        fresh = type(self.client)(self.client.app)
        self.assertEqual(fresh.get("/api/web/desk").status_code, 401)
        self.assertEqual(fresh.post("/api/web/desk/watchlist", json={"symbol": "BTC"}).status_code, 401)

    def test_watch_read_and_unwatch(self):
        c = self.client
        c.post("/api/web/login", json={"password": "correct horse"})
        self.assertEqual(c.post("/api/web/desk/watchlist", json={"symbol": "NOPE"}).status_code, 422)
        self.assertEqual(c.post("/api/web/desk/watchlist", json={"symbol": "BTC"}).status_code, 200)
        state = c.get("/api/web/desk").json()
        row = state["watchlist"][0]
        self.assertEqual(row["symbol"], "BTC")
        self.assertIn(row["signal"]["state"], {"new", "active", "stopped", "waiting"})
        self.assertGreater(row["record"]["trades"], 10)
        self.assertEqual(state["paper"]["account"]["equity"], 1000.0)
        self.assertEqual(state["mood"], {"marker": "mood"})
        import weekly_calls
        with mock.patch.object(weekly_calls, "this_week", lambda: {"calls": []}):
            self.assertEqual(c.get("/api/web/week").json(), {"calls": []})
        fresh = type(c)(c.app)
        self.assertEqual(fresh.get("/api/web/week").status_code, 401)
        self.assertEqual(fresh.get("/api/web/week/scoreboard").status_code, 401)
        with mock.patch.object(weekly_calls, "scoreboard", lambda stake, leverage: {"stake": stake, "lev": leverage}):
            self.assertEqual(c.get("/api/web/week/scoreboard?stake=18&leverage=5").json(),
                             {"stake": 18.0, "lev": 5.0})
            self.assertEqual(c.get("/api/web/week/scoreboard?stake=18").json()["lev"], 0.0)   # suggested
        self.assertEqual(c.get("/api/web/week/scoreboard?stake=18&leverage=4").status_code, 422)
        self.assertEqual(c.get("/api/web/week/scoreboard?stake=0&leverage=5").status_code, 422)
        # the desk's paper account cannot be started as an ordinary bot
        bot_id = state["paper"]["bot_id"]
        self.assertEqual(c.post(f"/api/web/console/bots/{bot_id}/start", json={}).status_code, 409)
        # one paper-trading pass runs against the same data without error
        import trend_agent
        with mock.patch.object(trend_agent, "EXPORT_DIR", Path(self.tmp.name)):
            self.assertIsInstance(self.routes_web._desk_cycle(), list)
        c.post("/api/web/desk/watchlist/remove", json={"symbol": "BTC"})
        self.assertEqual(c.get("/api/web/desk").json()["watchlist"], [])


if __name__ == "__main__":
    unittest.main()
