import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

from chatbuy import (parse_chat_buy, receipt_text, execute_chat_buy,  # noqa: E402
                     chat_usernames_from_env)

NAMES = ["neko_tradesbot", "nekochanbot"]
CA = "0x" + "a1" * 32
FUZZY_LONG = "0x0000000000000000000000000000000000000000000000000000000000000002::sui::SUI"


# ---------------------------------------------------------------- parser
class TestParse:
    def test_default_username(self):
        assert chat_usernames_from_env() == ["neko_tradesbot"]

    def test_not_a_mention(self):
        assert parse_chat_buy("hello world", NAMES) is None
        assert parse_chat_buy("", NAMES) is None
        assert parse_chat_buy("@someone_else buy 5 sui " + CA, NAMES) is None

    def test_mention_not_buy_is_ignored(self):
        assert parse_chat_buy("@Neko_tradesbot good morning", NAMES) is None
        assert parse_chat_buy("@Neko_tradesbot /buy 5", NAMES) is None

    def test_buy_with_sui_unit(self):
        p = parse_chat_buy(f"@neko_tradesbot buy 5 sui {CA}", NAMES)
        assert p and p["amount"] == 5 and p["ca"] == CA and p["mention"] == "neko_tradesbot"

    def test_buy_without_unit(self):
        p = parse_chat_buy(f"@neko_tradesbot buy 5 {CA}", NAMES)
        assert p and p["amount"] == 5 and p["ca"] == CA

    def test_buy_amount_after_ca(self):
        p = parse_chat_buy(f"@neko_tradesbot buy {CA} 5", NAMES)
        assert p and p["amount"] == 5 and p["ca"] == CA

    def test_uppercase_quantity(self):
        p = parse_chat_buy(f"@NEKO_TRADESBOT buy 12.5 SUI {CA}", NAMES)
        assert p and p["amount"] == 12.5 and p["ca"] == CA

    def test_commas(self):
        p = parse_chat_buy(f"@neko_tradesbot buy 1,000 sui {CA}", NAMES)
        assert p and p["amount"] == 1000

    def test_full_type_string(self):
        p = parse_chat_buy(f"@neko_tradesbot buy 2 {FUZZY_LONG}", NAMES)
        assert p and p["ca"] == FUZZY_LONG

    def test_second_username(self):
        p = parse_chat_buy(f"@nekochanbot buy 2 {CA}", NAMES)
        assert p and p["amount"] == 2

    def test_ca_type_suffix_cannot_inject(self):
        # TBP-04: the type suffix charset stops at quote/brace so a crafted CA
        # can never break out of the GraphQL string built downstream.
        evil = "0x" + "a1" * 32 + '::x") { coinMetadata }'
        p = parse_chat_buy(f"@neko_tradesbot buy 3 {evil}", NAMES)
        assert p and '"' not in p.get("ca", "") and "{" not in p.get("ca", "")

    def test_missing_ca(self):
        p = parse_chat_buy("@neko_tradesbot buy 5 sui peeka", NAMES)
        assert p and p.get("error")

    def test_missing_amount(self):
        p = parse_chat_buy(f"@neko_tradesbot buy {CA}", NAMES)
        assert p and p.get("error")

    def test_zero_amount(self):
        p = parse_chat_buy(f"@neko_tradesbot buy 0 sui {CA}", NAMES)
        assert p and p.get("error")


# ---------------------------------------------------------------- receipts
class TestReceipt:
    def test_private(self):
        assert receipt_text("private", amount=5) == "🐾 neko bought 5 SUI"

    def test_private_fraction(self):
        assert receipt_text("private", amount=0.5) == "🐾 neko bought 0.5 SUI"

    def test_public(self):
        t = receipt_text("public", username="wale", amount=5,
                         digest="0xabc", mention="neko_tradesbot")
        expected = ("@wale\n🔗 https://suiscan.xyz/mainnet/tx/0xabc\n\n"
                    "trade with me @neko_tradesbot")
        assert t == expected

    def test_public_escapes_html(self):
        t = receipt_text("public", username="<evil>&", amount=5, digest="0x1", mention="x")
        assert "<evil>" not in t and "&lt;evil&gt;&amp;" in t


# ---------------------------------------------------------------- execute triage
class _FakeLed:
    def __init__(self):
        self.cfg = {"bot_id": 7, "enabled": 0, "budget_sui": 0.0, "chat_receipt": "public"}

    def get_config(self, bid):
        return dict(self.cfg)

    def set_config(self, bid, **f):
        self.cfg.update(f)


class _FakeEx:
    def __init__(self):
        self.calls = []
        self.killed = False

    def is_killed(self, bid):
        return self.killed

    def _min_out(self, st, amt, slip_bps=500):
        return 42

    def buy(self, bid, **kw):
        self.calls.append(("buy", kw))
        return {"ok": True, "digest": "0xbuy", "tokens": 10}

    def buy_post_grad(self, bid, o, st, **kw):
        self.calls.append(("pg", o, kw))
        return {"ok": True, "digest": "0xpg"}

    def _dex_swap(self, bid, *a, **kw):
        self.calls.append(("ds", a, kw))
        return {"ok": True, "digest": "0xds"}


class _Ui:
    def __init__(self):
        self.ch = object()
        self.led = _FakeLed()
        self.ex = _FakeEx()


def _stub(kind, curve_obj=None):
    return SimpleNamespace(kind=kind, curve_obj=curve_obj,
                           launchpad="suipump", curve_id=CA, token_type=FUZZY_LONG)


class TestExecute:
    def test_unknown_rejected_no_autoboot(self, monkeypatch):
        import degen.launchpad as lp
        monkeypatch.setattr(lp, "resolve_input", lambda ch, ca: _stub("unknown"))
        ui = _Ui()
        r = execute_chat_buy(ui, 7, amount=5, ca=CA, idem="id1")
        assert not r["ok"] and "not buyable" in r["error"]
        assert not ui.led.cfg["enabled"]

    def test_curve_boots_degen_first(self, monkeypatch):
        import degen.launchpad as lp
        monkeypatch.setattr(lp, "resolve_input",
                            lambda ch, ca: _stub("curve", {"shared_version": 3}))
        ui = _Ui()
        r = execute_chat_buy(ui, 7, amount=5, ca=CA, idem="id1")
        assert r["ok"] and ui.led.cfg["enabled"] == 1
        assert ui.led.cfg["budget_sui"] == 40.0
        kind, kw = ui.ex.calls[0]
        assert kind == "buy" and kw["curve_id"] == CA and kw["curve_isv"] == 3
        assert kw["sui_amount"] == 5 and kw["min_out"] == 0 and kw["idem"] == "id1"
        assert kw["slip_bps"] == 500
        assert kw["source"] == "chat"

    def test_budget_kept_when_set(self, monkeypatch):
        import degen.launchpad as lp
        monkeypatch.setattr(lp, "resolve_input",
                            lambda ch, ca: _stub("curve", {"shared_version": 1}))
        ui = _Ui()
        ui.led.set_config(7, budget_sui=100.0)
        execute_chat_buy(ui, 7, amount=5, ca=CA, idem="id1")
        assert ui.led.cfg["budget_sui"] == 100.0

    def test_pool_routes_post_grad(self, monkeypatch):
        import degen.launchpad as lp
        monkeypatch.setattr(lp, "resolve_input", lambda ch, ca: _stub("pool"))
        ui = _Ui()
        r = execute_chat_buy(ui, 7, amount=5, ca=CA, idem="id2")
        assert r["ok"] and r["kind"] == "pool"
        kind, o, kw = ui.ex.calls[0]
        assert kind == "pg" and o["qty_sui"] == 5 and kw["idem"] == "id2"

    def test_generic_routes_dex_swap(self, monkeypatch):
        import degen.launchpad as lp
        from degen.constants import SUI_COIN_TYPE
        monkeypatch.setattr(lp, "resolve_input", lambda ch, ca: _stub("generic"))
        ui = _Ui()
        r = execute_chat_buy(ui, 7, amount=5, ca=CA, idem="id3", slip_bps=500)
        assert r["ok"] and r["kind"] == "generic"
        kind, a, kw = ui.ex.calls[0]
        assert kind == "ds" and a[0] == SUI_COIN_TYPE and a[1] == FUZZY_LONG

    def test_kill_switch_blocks(self, monkeypatch):
        import degen.launchpad as lp
        monkeypatch.setattr(lp, "resolve_input",
                            lambda ch, ca: _stub("curve", {"shared_version": 1}))
        ui = _Ui()
        ui.ex.killed = True
        r = execute_chat_buy(ui, 7, amount=5, ca=CA, idem="id4")
        assert not r["ok"] and "kill-switch" in r["error"]
        assert not ui.ex.calls

    def test_curve_missing_object_rejected(self, monkeypatch):
        import degen.launchpad as lp
        monkeypatch.setattr(lp, "resolve_input", lambda ch, ca: _stub("curve", None))
        ui = _Ui()
        r = execute_chat_buy(ui, 7, amount=5, ca=CA, idem="id5")
        assert not r["ok"] and "unreadable" in r["error"]