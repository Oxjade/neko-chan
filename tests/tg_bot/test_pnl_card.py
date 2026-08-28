"""Tests for the PnL card + photo push notifications in the watcher."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot", "cards"))

import pytest

from cards.generator import generate_pnl_card, random_avatar, TEMPLATES


def test_random_avatar_from_templates():
    assert len(TEMPLATES) >= 1
    av = random_avatar()
    assert os.path.exists(av)
    assert av.endswith((".jpg", ".png"))


def test_generate_win_card():
    out = generate_pnl_card(avatar_path=random_avatar(), pnl_pct=42.5,
                            buy_price=1.234, sell_price=1.759, token="$NEKO",
                            chain="SOLANA", out_path="/tmp/neko_win_test.png")
    assert os.path.exists(out)
    from PIL import Image
    im = Image.open(out)
    assert im.size == (1080, 1350)
    os.remove(out)


def test_generate_loss_card():
    out = generate_pnl_card(avatar_path=random_avatar(), pnl_pct=-8.2,
                            buy_price=2.5, sell_price=2.295, token="$NEKO",
                            chain="SUI", out_path="/tmp/neko_loss_test.png")
    assert os.path.exists(out)
    from PIL import Image
    assert Image.open(out).size == (1080, 1350)
    os.remove(out)


def test_watcher_pnl_card_for_fill():
    from watcher import Watcher
    w = Watcher(db_path=":memory:", notify=None, registry=None, bot_id=1, tg_id=1,
                bot_token="tok", chat_id=1, platform_base="http://x",
                start_equity=100000.0)
    path = w._pnl_card_for({"symbol": "BTC", "quantity": 0.1, "entry_price": 80000,
                            "exit_price": 88000, "pnl": 800.0, "market": "crypto",
                            "signal_id": 99}, "watcher_close")
    assert path and os.path.exists(path)
    from PIL import Image
    assert Image.open(path).size == (1080, 1350)
    os.remove(path)


def test_watcher_profit_report_dedup():
    from watcher import Watcher
    sent = []
    class FakeNotify:
        def notify(self, bot_id, tg_id, bot_token, chat_id, kind, ref_id, text,
                   buttons=None, dedup=True, photo_path=None):
            sent.append(text)
            return True
    w = Watcher(db_path=":memory:", notify=FakeNotify(), registry=None, bot_id=1, tg_id=1,
                bot_token="tok", chat_id=1, platform_base="http://x", start_equity=100000.0)
    ok = w.profit_report(123.45, 5, 60.0, 2.1, 100123.45)
    assert ok is True
    assert sent and "PROFIT REPORT" in sent[0]