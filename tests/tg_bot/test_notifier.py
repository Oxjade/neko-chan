import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest
from cryptography.fernet import Fernet
from unittest.mock import patch

os.environ.setdefault("TG_VAULT_MASTER_KEY", Fernet.generate_key().decode())

from key_vault import KeyVault
from store import Registry
from notifier import Notifier


@pytest.fixture()
def notifier():
    tmp = tempfile.mkdtemp()
    reg = Registry(os.path.join(tmp, "reg.db"), KeyVault())
    yield Notifier(reg), reg
    reg.close()


def test_first_error_pings(notifier):
    n, reg = notifier
    sent = []
    with patch.object(n, "_send", side_effect=lambda *a, **k: sent.append(a) or True):
        ok = n.error_event(1, 1, "tok", 42, "price feed failed", error_count=1)
    assert ok is True
    assert len(sent) == 1
    assert "retrying" in sent[0][2]


def test_repeat_errors_batch(notifier):
    n, reg = notifier
    sent = []
    with patch.object(n, "_send", side_effect=lambda *a, **k: sent.append(a) or True):
        n.error_event(1, 1, "tok", 42, "feed down", error_count=1)
        n.error_event(1, 1, "tok", 42, "feed down", error_count=2)
        n.error_event(1, 1, "tok", 42, "feed down", error_count=3)
        n.error_event(1, 1, "tok", 42, "feed down", error_count=5)
    # first error pings; only the %5 batch refresh is sent in between
    assert len(sent) == 2


def test_dedup_prevents_double_fill(notifier):
    n, reg = notifier
    sent = []
    with patch.object(n, "_send", side_effect=lambda *a, **k: sent.append(a) or True):
        n.notify(1, 1, "tok", 42, "fill", "sig-1", "✅ FILL BTC")
        n.notify(1, 1, "tok", 42, "fill", "sig-1", "✅ FILL BTC")
        n.notify(1, 1, "tok", 42, "fill", "sig-2", "✅ FILL ETH")
    assert len(sent) == 2


def test_daily_summary_once_per_day(notifier):
    n, reg = notifier
    sent = []
    with patch.object(n, "_send", side_effect=lambda *a, **k: sent.append(a) or True):
        n.daily_summary(1, 1, "tok", 42, 12.34, 3, 33.0, 4.12)
        n.daily_summary(1, 1, "tok", 42, 12.34, 3, 33.0, 4.12)
    assert len(sent) == 1