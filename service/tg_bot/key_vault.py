"""Fernet-encrypted storage of user secrets (Telegram bot tokens + AI API keys).

Secrets are encrypted at rest, never logged, and only ever displayed masked.
"""

from cryptography.fernet import Fernet

from tg_config import require_vault_key


class KeyVault:
    def __init__(self, master_key: bytes | None = None):
        self._fernet = Fernet(master_key or require_vault_key())

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        return self._fernet.decrypt(ciphertext).decode()

    @staticmethod
    def mask(secret: str, visible: int = 4) -> str:
        """sk-•••4821 style mask; never reveals the full secret."""
        if not secret:
            return "(empty)"
        prefix = secret[:3] if secret.startswith(("sk-", "sk_")) else ""
        tail = secret[-visible:]
        return f"{prefix}•••{tail}"

    @staticmethod
    def hash_key(secret: str) -> str:
        import hashlib

        return hashlib.sha256(secret.encode()).hexdigest()