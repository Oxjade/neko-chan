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


class InOrOutTests(unittest.TestCase):
    def closes(self, now, four_weeks_ago):
        from datetime import date, timedelta
        start = date(2026, 11, 8)
        prices = [now, 1, 1, 1, four_weeks_ago, 1]
        return [{"date": start - timedelta(days=7 * i), "close": p} for i, p in enumerate(prices)]

    def test_hold_needs_both_greed_and_a_rise(self):
        import in_or_out
        self.assertTrue(in_or_out.decide(60, self.closes(110, 100))["hold"])
        self.assertFalse(in_or_out.decide(49, self.closes(110, 100))["hold"])      # fearful
        self.assertFalse(in_or_out.decide(60, self.closes(90, 100))["hold"])       # fell
        d = in_or_out.decide(60, self.closes(110, 100))
        self.assertAlmostEqual(d["four_week_change_pct"], 10.0)

    def test_week_answer_and_email_line(self):
        import crypto_mood
        import in_or_out
        mood = {"week_reading": {"value": 70, "label": "Greed", "date": "2026-11-08"}}
        up, down = self.closes(110, 100), self.closes(90, 100)
        with mock.patch.object(crypto_mood, "this_week", lambda: mood), \
                mock.patch.object(in_or_out, "sunday_closes", lambda coin: up if coin == "BTC" else down):
            week = in_or_out.this_week()
            line = in_or_out.one_line()
        self.assertEqual((week["answer"], week["hold"]), ("hold", ["BTC"]))
        self.assertIn("hold BTC", line)
        rec = week["record"]
        self.assertGreater(rec["rule"]["all"]["grew_to"], rec["hold"]["all"]["grew_to"])
        self.assertGreater(rec["rule"]["all"]["worst_fall_pct"], rec["hold"]["all"]["worst_fall_pct"])
        fearful = {"week_reading": {"value": 10, "label": "Extreme Fear", "date": "2026-11-08"}}
        with mock.patch.object(crypto_mood, "this_week", lambda: fearful), \
                mock.patch.object(in_or_out, "sunday_closes", lambda coin: up):
            self.assertEqual(in_or_out.this_week()["answer"], "cash")
            self.assertIn("stay in cash", in_or_out.one_line())

    def week(self, monday, moves, before=100.0, month_ago=90.0):
        """Daily candles for one week: opens at 100, with the given (low, close) moves in percent."""
        from datetime import timedelta
        candles = {monday - timedelta(days=1): {"open": before, "low": before, "close": before},
                   monday - timedelta(days=29): {"open": month_ago, "low": month_ago, "close": month_ago}}
        for i, (low, close) in enumerate(moves):
            candles[monday + timedelta(days=i)] = {"open": 100.0, "low": 100.0 * (1 + low / 100),
                                                   "close": 100.0 * (1 + close / 100)}
        return candles

    def test_week_result_cash_gain_and_liquidation(self):
        import in_or_out
        from datetime import date
        monday = date(2026, 9, 14)
        calm = self.week(monday, [(-0.5, 0.2)] * 6 + [(-0.5, 4.0)])        # ends +4%, dips 0.5%
        # fearful mood: no trade
        self.assertEqual(in_or_out.week_result(calm, monday, 40, 9.0, 5, 40)["result"], 0.0)
        # greedy, unleveraged: 4% of 9 USD less two small fees
        spot = in_or_out.week_result(calm, monday, 60, 9.0, 1, 40)
        self.assertTrue(spot["hold"])
        self.assertAlmostEqual(spot["result"], 9.0 * (0.04 - 2 * in_or_out.FEE_PER_SIDE))
        # 5x multiplies the move and adds funding
        lev = in_or_out.week_result(calm, monday, 60, 9.0, 5, 40)
        self.assertGreater(lev["result"], 4 * spot["result"])
        self.assertLess(lev["result"], 5 * spot["result"])
        # a 2.5% dip liquidates 30x even though the week ends up 4%
        dip = self.week(monday, [(-2.5, 1.0)] + [(-0.5, 0.2)] * 5 + [(-0.5, 4.0)])
        wiped = in_or_out.week_result(dip, monday, 60, 9.0, 30, 40)
        self.assertTrue(wiped["wiped"])
        self.assertEqual(wiped["result"], -9.0)
        self.assertFalse(in_or_out.week_result(dip, monday, 60, 9.0, 5, 40)["wiped"])
        # leverage above the exchange's limit is capped
        self.assertEqual(in_or_out.week_result(calm, monday, 60, 9.0, 30, 25)["leverage_used"], 25)
        # missing days: the week is not scored
        short = dict(calm)
        short.pop(monday)
        self.assertFalse(in_or_out.week_result(short, monday, 60, 9.0, 1, 40)["known"])

    def test_scoreboard_totals(self):
        import crypto_mood
        import in_or_out
        from datetime import date, timedelta
        mondays = [date(2026, 9, 14), date(2026, 9, 7)]
        # The older week's last day is also the newer week's "Sunday before", so
        # build the newer week first and let the older week's real days win.
        candles = {}
        candles.update(self.week(mondays[0], [(-0.5, 0.2)] * 6 + [(-0.5, 4.0)]))     # newer week: +4%
        candles.update(self.week(mondays[1], [(-0.5, 0.2)] * 6 + [(-0.5, -3.0)]))    # older week: -3%
        moods = {m - timedelta(days=1): 60 for m in mondays}
        live = {c: {"max_leverage": 40 if c == "BTC" else 25} for c in in_or_out.COINS}
        with mock.patch.object(crypto_mood, "history", lambda: moods), \
                mock.patch.object(market_data, "catalogue", lambda: live), \
                mock.patch.object(in_or_out, "daily_candles", lambda coin: candles):
            board = in_or_out.scoreboard(18, 1, weeks=2)
            high = in_or_out.scoreboard(18, 30, weeks=2)
        self.assertEqual([r["week_of"] for r in board["rows"]], ["2026-09-14", "2026-09-07"])
        self.assertEqual((board["ahead"], board["behind"], board["no_trade"]), (1, 1, 0))
        self.assertAlmostEqual(board["rows"][1]["running_total"], board["rows"][1]["result"])
        self.assertAlmostEqual(board["rows"][0]["running_total"], board["total"])
        self.assertAlmostEqual(board["total"], 18 * (0.04 - 0.03 - 4 * in_or_out.FEE_PER_SIDE))
        self.assertEqual(high["capped"], {"ETH": 25})
        self.assertGreater(high["expect"]["lost_whole_stake"], 0.5)

    def test_mismatched_weeks_are_refused(self):
        import crypto_mood
        import in_or_out
        mood = {"week_reading": {"value": 70, "label": "Greed", "date": "2026-11-15"}}
        with mock.patch.object(crypto_mood, "this_week", lambda: mood), \
                mock.patch.object(in_or_out, "sunday_closes", lambda coin: self.closes(110, 100)):
            with self.assertRaises(market_data.DataError):
                in_or_out.this_week()


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
        import in_or_out
        with mock.patch.object(in_or_out, "this_week", lambda: {"answer": "cash"}):
            self.assertEqual(c.get("/api/web/week").json(), {"answer": "cash"})
        fresh = type(c)(c.app)
        self.assertEqual(fresh.get("/api/web/week").status_code, 401)
        self.assertEqual(fresh.get("/api/web/week/scoreboard").status_code, 401)
        with mock.patch.object(in_or_out, "scoreboard", lambda stake, leverage: {"stake": stake, "lev": leverage}):
            self.assertEqual(c.get("/api/web/week/scoreboard?stake=18&leverage=5").json(),
                             {"stake": 18.0, "lev": 5.0})
        self.assertEqual(c.get("/api/web/week/scoreboard?stake=18&leverage=7").status_code, 422)
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
