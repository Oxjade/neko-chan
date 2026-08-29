"""Execution vault: trading keys for real venues.

Uses a SEPARATE Fernet master key (TG_EXEC_MASTER_KEY) from the Telegram/AI
vault. Keys are never logged; masks show a short suffix only.
"""

import hashlib
import os

from cryptography.fernet import Fernet


def _master_key() -> bytes:
    key = os.getenv("TG_EXEC_MASTER_KEY", "")
    if not key:
        raise RuntimeError(
            "TG_EXEC_MASTER_KEY is not set. Generate: python -c "
            "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return key.encode()


def generate_key_material(chain: str) -> tuple[str, str]:
    """(address, private_key) for a fresh trading wallet per chain."""
    if chain == "hyperliquid":
        import eth_keys

        key = eth_keys.keys.PrivateKey(os.urandom(32))
        return key.public_key.to_checksum_address(), key.to_hex()
    if chain == "solana":
        from solders.keypair import Keypair

        kp = Keypair()
        return str(kp.pubkey()), bytes(kp.secret()).hex()
    if chain == "sui":
        from pysui.sui.sui_crypto import create_new_keypair

        _, kp = create_new_keypair()
        pub_key = bytes(kp.public_key.key_bytes)
        # Sui address = blake2b-256(0x00 || pubkey); 0x00 is the ED25519
        # scheme flag PREPENDED (the flag is a prefix, not a suffix).
        addr = "0x" + hashlib.blake2b(b"\x00" + pub_key, digest_size=32).hexdigest()
        return addr, bytes(kp.private_key.key_bytes).hex()
    raise ValueError(f"unsupported chain for key generation: {chain}")


class ExecVault:
    def __init__(self, master_key: bytes | None = None):
        self._fernet = Fernet(master_key or _master_key())

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        return self._fernet.decrypt(ciphertext).decode()

    @staticmethod
    def fingerprint(secret: str, visible: int = 4) -> str:
        return f"•••{secret[-visible:]}"

    @staticmethod
    def key_hash(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()