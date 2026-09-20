import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

from chatwallet import (parse_chat_wallet, created_reply, exists_reply,  # noqa: E402
                        failed_reply, master_username, provision_wallet,
                        wallet_credentials, enable_degen,
                        chat_usernames_from_env)

NAMES = ["neko_tradesbot", "nekochanbot"]

import pytest


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch):
    """provision_wallet() encrypts with TG_EXEC_MASTER_KEY — give tests a real one."""
    from cryptography.fernet import Fernet
    monkeypatch.setenv("TG_EXEC_MASTER_KEY", Fernet.generate_key().decode())


# ---------------------------------------------------------------- parser
class TestParse:
    def test_default_username(self):
        names = chat_usernames_from_env()
        assert names and names[0] in ("neko_tradesbot", "neko_tradesbot")

    def test_not_a_request(self):
        assert parse_chat_wallet("hello world", NAMES) is None
        assert parse_chat_wallet("", NAMES) is None
        assert parse_chat_wallet("@someone_else create wallet", NAMES) is None

    def test_unrelated_mention_ignored(self):
        assert parse_chat_wallet("@Neko_tradesbot good morning", NAMES) is None
        assert parse_chat_wallet("@Neko_tradesbot buy 5 sui", NAMES) is None

    def test_canonical(self):
        p = parse_chat_wallet("@neko_tradesbot create wallet", NAMES)
        assert p and p["mention"] == "neko_tradesbot"

    def test_case_insensitive_username(self):
        p = parse_chat_wallet("@NEKO_TRADESBOT create wallet", NAMES)
        assert p and p["mention"] == "NEKO_TRADESBOT"

    def test_variants(self):
        for txt in ("@neko_tradesbot make me a wallet",
                    "@nekochanbot create my wallet",
                    "@neko_tradesbot new wallet",
                    "@neko_tradesbot wallet please",
                    "@neko_tradesbot setup a wallet for me",
                    "@neko_tradesbot need a wallet for trading",
                    "@neko_tradesbot   create    a    wallet  now"):
            assert parse_chat_wallet(txt, NAMES) is not None, txt

    def test_not_a_wallet_request(self):
        for txt in ("@neko_tradesbot wallet on solana",
                    "@neko_tradesbot wallets",
                    "@neko_tradesbot sell wallet",
                    "@neko_tradesbot create tokens"):
            assert parse_chat_wallet(txt, NAMES) is None, txt


# ---------------------------------------------------------------- replies
class TestReplies:
    def test_master_default(self):
        assert master_username() == "neko_tradesbot"

    def test_created_never_leaks_key(self):
        t = created_reply(username="wale", master="neko_tradesbot")
        assert "@wale" in t and "@neko_tradesbot" in t
        assert "key" in t.lower()
        # the private key itself is never in the group reply
        assert "suiprivkey" not in t
        assert not t.count(" ") > 120  # sanely sized

    def test_created_escapes_username(self):
        t = created_reply(username="<evil>&", master="neko_tradesbot")
        assert "<evil>" not in t and "&lt;evil&gt;&amp;" in t

    def test_exists_and_failed(self):
        assert "@wale" in exists_reply("wale", "neko_tradesbot")
        assert failed_reply("wale", "neko_tradesbot")


# ---------------------------------------------------------------- provisioning
class TestProvision:
    def test_provision_round_trip(self, monkeypatch, tmp_path):
        db = str(tmp_path / "exec_ledger.db")
        monkeypatch.setenv("EXEC_LEDGER_PATH", db)
        addr, key = provision_wallet(42)
        assert addr and addr.startswith("0x") and key
        # wallet survives + is reusable (idempotent)
        addr2, key2 = provision_wallet(42)
        assert addr2 == addr and key2 == key
        # readable via the read path
        addr3, key3 = wallet_credentials(42)
        assert addr3 == addr and key3 == key

    def test_credentials_empty_for_unknown_bot(self, monkeypatch, tmp_path):
        db = str(tmp_path / "exec_ledger.db")
        monkeypatch.setenv("EXEC_LEDGER_PATH", db)
        assert wallet_credentials(999) == ("", "")

    def test_degen_off_when_env_gated(self, monkeypatch):
        monkeypatch.setenv("DEGEN_ENABLED", "0")
        assert enable_degen(7) is False

    def test_enable_degen_grants_no_budget(self, monkeypatch, tmp_path):
        from degen import runtime
        db = str(tmp_path / "degen_ledger.db")
        monkeypatch.setenv("DEGEN_ENABLED", "1")
        monkeypatch.setenv("DEGEN_LEDGER_PATH", db)
        runtime._SINGLETON.clear()
        assert enable_degen(7) is True
        cfg = runtime.get_ledger().get_config(7)
        assert cfg["enabled"] == 1 and cfg["view"] == 1
        assert float(cfg["budget_sui"]) == 0.0

    def test_provision_isolated_per_bot(self, monkeypatch, tmp_path):
        db = str(tmp_path / "exec_ledger.db")
        monkeypatch.setenv("EXEC_LEDGER_PATH", db)
        a1, _ = provision_wallet(1)
        a2, _ = provision_wallet(2)
        assert a1 != a2