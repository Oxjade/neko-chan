import os
import sys

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "..", "service", "execution"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "service"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "service", "tg_bot"))

import chattrack  # noqa: E402
from degen.db import DegenLedger  # noqa: E402
from degen import constants as K  # noqa: E402

W = "0x" + "ab" * 32
W2 = "0x" + "cd" * 32
USERS = ["neko_tradesbot"]
PKG = K.SUIPUMP_PACKAGES[0]  # real package id -> matches tracker poll keys


def test_parse_track():
    p = chattrack.parse_chat_track(f"@neko_tradesbot track {W}", USERS)
    assert p == {"op": "track", "wallet": W, "mention": "neko_tradesbot"}


def test_parse_track_caps_and_spaces():
    p = chattrack.parse_chat_track(f"@NEKO_TRADESBOT  follow  {W.upper()}", USERS)
    assert p["op"] == "track"
    assert p["wallet"] == W


def test_parse_untrack():
    p = chattrack.parse_chat_track(f"@neko_tradesbot stop tracking {W}", USERS)
    assert p == {"op": "untrack", "wallet": W, "mention": "neko_tradesbot"}


def test_parse_list():
    for txt in ("@neko_tradesbot tracks", "@neko_tradesbot my tracks",
                "@neko_tradesbot track list"):
        p = chattrack.parse_chat_track(txt, USERS)
        assert p == {"op": "list", "mention": "neko_tradesbot"}


def test_parse_rejects_bad_wallet():
    p = chattrack.parse_chat_track("@neko_tradesbot track 0x12abc", USERS)
    assert p is not None and p.get("error")
    assert p["op"] == "track"


def test_parse_ignores_other_bot():
    assert chattrack.parse_chat_track(f"@other_bot track {W}", USERS) is None


def test_parse_ignores_buy():
    assert chattrack.parse_chat_track(f"@neko_tradesbot buy 5 sui {W}", USERS) is None


def test_parse_ignores_create_wallet():
    assert chattrack.parse_chat_track("@neko_tradesbot create wallet", USERS) is None


def test_short_wallet():
    assert chattrack.short_wallet(W) == f"{W[:7]}…{W[-4:]}"


# ---------------------------------------------------------------- ledger
def make_ledger():
    return DegenLedger(":memory:")


def test_chat_track_add_list_remove():
    led = make_ledger()
    assert led.chat_track_count(1) == 0
    assert led.chat_track_add(1, 999, W, bot_id=7, username="alice",
                              buys=3, min_sui=1.0)
    assert led.chat_track_add(1, 999, W2, buys=1, min_sui=5.0)
    assert led.chat_track_count(1) == 2
    rows = led.chat_track_rows(1)
    assert {r["wallet"] for r in rows} == {W, W2}
    assert next(r for r in rows if r["wallet"] == W)["bot_id"] == 7
    all_rows = led.chat_track_all()
    assert len(all_rows) == 2
    # idempotent re-add updates, not duplicates
    led.chat_track_add(1, 999, W, bot_id=8, buys=3)
    assert led.chat_track_count(1) == 2
    assert next(r for r in led.chat_track_rows(1) if r["wallet"] == W)["bot_id"] == 8
    # remove
    assert led.chat_track_remove(1, W)
    assert led.chat_track_count(1) == 1
    assert not led.chat_track_remove(1, W)
    # nothing leaks to another user
    assert led.chat_track_count(2) == 0


def test_chat_track_wallet_normalized():
    led = make_ledger()
    caps = W.upper()
    led.chat_track_add(1, 999, caps)
    assert led.chat_track_rows(1)[0]["wallet"] == W  # normalized lowercase


# ---------------------------------------------------------------- tracker
from tracker import WalletTracker  # noqa: E402


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def _send(self, bot_token, chat_id, text, buttons=None):
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return True


class FakeChain:
    def __init__(self, nodes=None):
        self._nodes = nodes or {}

    def events(self, t, first=50, after=""):
        return self._nodes.get(t, []), "", False


def _node(ev, seq=1, wallet=W, amt_atoms=3 * 10**9, curve="0x" + "e" * 64,
          digest="dc" * 32, side_field=None):
    field = side_field or ("buyer" if ev == "TokensPurchased" else "seller")
    return {"seq": seq, "digest": digest,
            "json": {field: wallet, "sui_in": amt_atoms, "sui_out": amt_atoms,
                     "curve_id": curve}}


EV_BUY = f"{PKG}::{K.SUIPUMP_MODULE}::TokensPurchased"
EV_SELL = f"{PKG}::{K.SUIPUMP_MODULE}::TokensSold"


def test_tracker_alerts_buy_and_sell():
    led = make_ledger()
    led.chat_track_add(1, 999, W, username="alice", buys=3, min_sui=1.0)
    ch = FakeChain({EV_BUY: [_node("TokensPurchased")],
                    EV_SELL: [_node("TokensSold")]})
    nf = FakeNotifier()
    tr = WalletTracker(ch, led, nf, neko_username="neko_tradesbot")
    fired = tr.poll_once()
    assert fired == 2  # buy AND sell within the same poll (per-side cooldown)
    assert len(nf.sent) == 2
    bodies = {m["text"] for m in nf.sent}
    assert any("bought" in t for t in bodies)
    assert any("sold" in t for t in bodies)
    for m in nf.sent:
        assert "3.00 SUI" in m["text"]
        assert W in m["text"]  # full address in the alert
        assert "@neko_tradesbot" in m["text"]
        # CTA button registered (row = [label, callback])
        assert m["buttons"][0][1].startswith("sb:tbuy:")
    cta_id = nf.sent[0]["buttons"][0][1].split(":")[-1]
    payload = tr.pop_cta(cta_id)
    assert payload["tg_uid"] == 1 and payload["amount"] == 3.0


def test_tracker_symbol_cache_reuse():
    """Second alert for the SAME curve within TTL hits the symbol cache."""
    led = make_ledger()
    led.chat_track_add(1, 999, W, min_sui=1.0)
    led.chat_track_add(2, 999, W2, min_sui=1.0)
    curve = "0x" + "e" * 64
    ch = FakeChain({EV_BUY: [
        _node("TokensPurchased", seq=1, wallet=W, curve=curve),
        _node("TokensPurchased", seq=2, wallet=W2, curve=curve)]})
    nf = FakeNotifier()
    tr = WalletTracker(ch, led, nf)
    fired = tr.poll_once()
    assert fired == 2
    assert len(nf.sent) == 2
    assert "curve:0xeeeeee" in nf.sent[0]["text"]


def test_tracker_min_amount_filters():
    led = make_ledger()
    led.chat_track_add(1, 999, W, buys=3, min_sui=5.0)  # min 5 SUI
    ch = FakeChain({EV_BUY: [_node("TokensPurchased", amt_atoms=3 * 10**9)]})
    nf = FakeNotifier()
    tr = WalletTracker(ch, led, nf)
    emitted = tr.poll_once()
    assert emitted == 0
    assert nf.sent == []


def test_tracker_respects_scope():
    led = make_ledger()
    led.chat_track_add(1, 999, W, buys=1, min_sui=1.0)  # buys only
    ch = FakeChain({EV_SELL: [_node("TokensSold")]})
    nf = FakeNotifier()
    tr = WalletTracker(ch, led, nf)
    tr.poll_once()
    assert nf.sent == []


def test_tracker_dedup_and_cooldown():
    led = make_ledger()
    led.chat_track_add(1, 999, W, buys=3, min_sui=1.0)
    node = _node("TokensPurchased")
    ch = FakeChain({EV_BUY: [node, node]})
    nf = FakeNotifier()
    tr = WalletTracker(ch, led, nf)
    tr.poll_once()
    # same digest+seq delivered twice -> one alert
    assert len(nf.sent) == 1
    # second identical poll after cooldown -> still deduped
    tr.poll_once()
    assert len(nf.sent) == 1