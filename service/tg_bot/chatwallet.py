"""In-chat create-wallet: parser, reply text, and wallet provisioning.

Pure functions (parse + reply text) plus provisioning that touches the exec
ledger (wallet keys) and the degen ledger (engine flags). Never calls the
Telegram API — the caller owns the reply. Mirrors chatbuy.py's shape.

Security: the private key never appears in any string returned here. It is
stored encrypted in the exec ledger and revealed only in the user's DM after
they press Start (see userbot.welcome). A group reply only points them there.
"""
from __future__ import annotations

import contextlib
import os
import re
from typing import Iterator

from chatbuy import esc  # shared HTML escaping

_DEFAULT_USERNAME = "neko_tradesbot"
_WALLET_CHAIN = "sui"


def chat_usernames_from_env() -> list[str]:
    """Usernames that trigger an in-chat create-wallet (NEKO_CHAT_USERNAMES)."""
    raw = os.getenv("NEKO_CHAT_USERNAMES", _DEFAULT_USERNAME)
    return [u.strip().lstrip("@") for u in raw.split(",") if u.strip()]


def master_username() -> str:
    """The bot users press Start on (DMs carry the key reveal)."""
    return (chat_usernames_from_env() or [_DEFAULT_USERNAME])[0]


# "create wallet", "make me a wallet", "new wallet", "wallet please", …
_CREATE_WALLET_RE = re.compile(
    r"(?i)^\s*@([A-Za-z0-9_]{3,64})\b\s*"
    r"(?:(?:create|make|new|setup|open|give|gim?me|need|get)\s+)?"
    r"(?:(?:me|my|a|an|the|us|one)\s+)*"
    r"wallet\b(?:\s+(?:please|pls|now|right\s+now))?"
    r"(?:\s+(?:for|to)\s+\w+)?\s*$"
)


def parse_chat_wallet(text: str, usernames: list[str]) -> dict | None:
    """Parse an in-chat @mention create-wallet request.

    Returns {"mention": str} when the message is a create-wallet request
    addressed to one of our bots, else None (caller falls through).
    """
    if not text:
        return None
    m = _CREATE_WALLET_RE.match(text)
    if not m:
        return None
    name = m.group(1).lower()
    if name not in {u.lower().lstrip("@") for u in usernames}:
        return None
    return {"mention": m.group(1)}


def _reply_names(username: str, master: str) -> tuple[str, str]:
    """Escape reply parties, defaulting the master to the configured bot."""
    return esc(username or "trader"), esc(master or master_username())


def created_reply(username: str = "", master: str = "") -> str:
    """Group reply after a wallet was created. NO key in here — ever."""
    name, master = _reply_names(username, master)
    return (
        f"🐾 @{name}, your <b>SUI wallet is ready!</b>\n\n"
        f"👉 Press <b>Start</b> on @{master} to collect your <b>private key</b>, "
        f"shown exactly once, and you land straight in <b>degen mode</b>.\n\n"
        f"⚠️ I never DM you your key, and I'll never ask for it here. "
        f"Keep it secret, keep it safe."
    )


def exists_reply(username: str = "", master: str = "") -> str:
    """Group reply when the tagger ALREADY has a wallet (idempotent)."""
    name, master = _reply_names(username, master)
    return f"🐾 @{name} you're <b>already a user</b>, trade with @{master}!"


def failed_reply(username: str = "", master: str = "") -> str:
    name, master = _reply_names(username, master)
    return (
        f"⚠️ @{name}, I couldn't create your wallet just yet.\n\n"
        f"👉 Press <b>Start</b> on @{master} and we'll finish setup there."
    )


@contextlib.contextmanager
def _open_ledger() -> Iterator:
    """Open the exec ledger, yielding to the caller, always closing it."""
    from ledger import ExecLedger  # heavy import; keep local + lazy

    ledger = ExecLedger(os.getenv("EXEC_LEDGER_PATH", "exec_ledger.db"))
    try:
        yield ledger
    finally:
        try:
            ledger.close()
        except Exception:
            pass


def _decrypt_key(key_enc: str) -> str:
    from exec_vault import ExecVault  # lazy: needs reserved crypto layer

    return ExecVault().decrypt(key_enc)


def _sui_keypair(bot_id: int) -> tuple[str, str]:
    """(address, decrypted key) for bot_id from the exec ledger, else ('', '')."""
    with _open_ledger() as ledger:
        row = ledger.wallet_by_bot_chain(bot_id, _WALLET_CHAIN)
        if not row or not row.get("key_enc"):
            return "", ""
        return row["address"], _decrypt_key(row["key_enc"])


def provision_wallet(bot_id: int) -> tuple[str, str]:
    """Create or reuse the Sui wallet for bot_id in the exec ledger.

    Returns (address, private_key), or (None, None) on failure. Uses the exact
    key-generation + Fernet-encryption path from onboarding (userbot.py) so a
    tag-created wallet is indistinguishable from a wizard-created one.
    """
    from exec_vault import ExecVault, generate_key_material

    try:
        with _open_ledger() as ledger:
            existing = ledger.wallet_by_bot_chain(bot_id, _WALLET_CHAIN)
            if existing and existing.get("key_enc"):
                return existing["address"], _decrypt_key(existing["key_enc"])
            address, key_hex = generate_key_material(_WALLET_CHAIN)
            enc = ExecVault().encrypt(key_hex)
            try:
                ledger.upsert_wallet(bot_id, _WALLET_CHAIN, address, address,
                                     enc, ExecVault.key_hash(key_hex))
            except ValueError:
                # one keypair per bot already held by THIS bot -> reuse it
                existing = ledger.wallet_by_bot_chain(bot_id, _WALLET_CHAIN)
                if existing and existing.get("key_enc"):
                    return existing["address"], _decrypt_key(existing["key_enc"])
                return None, None
            return address, key_hex
    except Exception:
        return None, None


def wallet_credentials(bot_id: int) -> tuple[str, str]:
    """(address, private_key) for an existing bot, else ('', '')."""
    try:
        return _sui_keypair(bot_id)
    except Exception:
        return "", ""


def enable_degen(bot_id: int) -> bool:
    """Turn on the degen engine for this bot (enabled + the degen VIEW).

    set_config() only writes `view` on the UPDATE branch — the INSERT default
    is 0 (db.py) — so the row is created with enabled=1 first, then the second
    call (always an UPDATE) flips view=1.
    Returns False when the degen engine is off (env-gated) — the tag still
    created the wallet, the user just lands on the classic dashboard.
    """
    try:
        from degen import runtime as _rt

        if not _rt.env_on():
            return False
        led = _rt.get_ledger()
        led.set_config(bot_id, enabled=1)          # forces the row to exist
        led.set_config(bot_id, enabled=1, view=1)  # UPDATE branch writes view
        return True
    except Exception:
        return False