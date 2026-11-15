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
