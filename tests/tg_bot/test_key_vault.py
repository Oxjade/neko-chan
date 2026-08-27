import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("TG_VAULT_MASTER_KEY", Fernet.generate_key().decode())

from key_vault import KeyVault


def test_roundtrip():
    v = KeyVault()
    enc = v.encrypt("sk-abc123")
    assert enc != b"sk-abc123"
    assert v.decrypt(enc) == "sk-abc123"


def test_mask():
    assert KeyVault.mask("sk-abcdef123456") == "sk-•••3456"
    assert KeyVault.mask("123456789:AAsecret") == "•••cret"
    assert KeyVault.mask("") == "(empty)"


def test_wrong_key_fails():
    v1 = KeyVault()
    v2 = KeyVault(Fernet.generate_key().decode().encode())
    enc = v1.encrypt("secret")
    with pytest.raises(Exception):
        v2.decrypt(enc)


def test_hash_stable():
    assert KeyVault.hash_key("abc") == KeyVault.hash_key("abc")
    assert KeyVault.hash_key("abc") != KeyVault.hash_key("abd")