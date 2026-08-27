import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("TG_EXEC_MASTER_KEY", Fernet.generate_key().decode())

from exec_vault import ExecVault, generate_key_material


def test_roundtrip_and_fingerprint():
    v = ExecVault()
    enc = v.encrypt("0xdeadbeefcafe")
    assert enc != b"0xdeadbeefcafe"
    assert v.decrypt(enc) == "0xdeadbeefcafe"
    assert v.fingerprint("0xdeadbeefcafe") == "•••cafe"
    assert v.key_hash("abc") == v.key_hash("abc")


def test_wrong_key_fails():
    v1 = ExecVault()
    v2 = ExecVault(Fernet.generate_key().decode().encode())
    enc = v1.encrypt("secret")
    with pytest.raises(Exception):
        v2.decrypt(enc)


def test_key_generation_hyperliquid():
    addr, priv = generate_key_material("hyperliquid")
    assert addr.startswith("0x") and len(addr) == 42
    assert priv.startswith("0x") and len(priv) == 66