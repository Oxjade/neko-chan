import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("TG_VAULT_MASTER_KEY", Fernet.generate_key().decode())

from key_vault import KeyVault
from store import Registry


@pytest.fixture()
def reg():
    tmp = tempfile.mkdtemp()
    r = Registry(os.path.join(tmp, "registry.db"), KeyVault())
    yield r
    r.close()


def test_user_upsert_and_isolation(reg):
    reg.upsert_user(1, "alice")
    reg.upsert_user(2, "bob")
    assert reg.get_user(1)["tg_username"] == "alice"
    assert reg.get_user(2)["tg_username"] == "bob"
    assert reg.get_user(3) is None


def test_key_store_dedupe_and_revoke(reg):
    k1 = reg.store_key(1, "openai", "sk-aaaa", None, "gpt-4o-mini")
    assert k1["provider"] == "openai"
    # same key from another user -> blocked
    with pytest.raises(ValueError):
        reg.store_key(2, "openai", "sk-aaaa", None, "gpt-4o-mini")
    # own key rotation allowed (old revoked)
    reg.store_key(1, "openai", "sk-bbbb", None, "gpt-4o-mini")
    active = reg.get_active_key(1)
    assert active["api_key"] == "sk-bbbb"
    # plaintext not in DB dump
    reg.revoke_keys(1)
    assert reg.get_active_key(1) is None


def test_bot_create_and_duplicate_token(reg):
    b = reg.create_bot(1, "Whale", "111:tok1", "whale_bot", "WhaleAgent", "ptok",
                       {"perps": 1, "spot": 0, "us-stock": 1, "forex": 0},
                       5.0, 120, "balanced")
    assert reg.get_bot(b["id"])["bot_name"] == "Whale"
    assert reg.bot_token(b["id"]) == "111:tok1"
    with pytest.raises(ValueError):
        reg.create_bot(2, "Other", "111:tok1", "other_bot", "OtherAgent", "ptok2",
                       {"perps": 1}, 1.0, 120, "balanced")
    # owner isolation
    assert reg.bots_for(2) == []
    assert len(reg.bots_for(1)) == 1


def test_event_dedup(reg):
    reg.mark_event(1, "fill", "sig-42", {"symbol": "BTC"})
    assert reg.event_seen(1, "fill", "sig-42") is True
    assert reg.event_seen(1, "fill", "sig-43") is False
    assert len(reg.recent_events(1)) == 1


def test_update_and_delete_bot(reg):
    b = reg.create_bot(1, "Whale", "111:tok1", "whale_bot", "WhaleAgent", "ptok",
                       {"perps": 1}, 1.0, 120, "balanced")
    reg.update_bot(b["id"], interval_sec=60, is_running=1)
    assert reg.get_bot(b["id"])["interval_sec"] == 60
    assert reg.get_bot(b["id"])["is_running"] == 1
    reg.delete_bot(b["id"], 1)
    assert reg.get_bot(b["id"]) is None