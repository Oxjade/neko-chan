"""In-chat @neko create-wallet: parser + reply copy + registration_provisioner.

Pure functions (parse + reply text) live here. Provisioning touches exec_ledger
(wallet keys) and degen ledger (defaults) but never the Telegram API — the
caller owns the reply. Mirrors chatbuy.py's shape.

Security: the private key NEVER appears in any string returned here — it is
stored encrypted in exec_ledger and revealed only in the user's DM after they
press Start (see userbot.welcome). A group reply only points them there.
"""
from __future__ import annotations

import os
import re

from chatbuy import esc  # reuse shared HTML escaping


def chat_usernames_from_env() -> list[str]:
    """Bot usernames that trigger an in-chat create-wallet (NEKO_CHAT_USERNAMES)."""
    raw = os.getenv("NEKO_CHAT_USERNAMES", "neko_tradesbot")
    return [u.strip().lstrip("@") for u in raw.split(",") if u.strip()]


def master_username() -> str:
    """The single bot users press Start on (DMs carry the key reveal)."""
    names = chat_usernames_from_env()
    return names[0] if names else "neko_tradesbot"


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


def created_reply(username: str = "", master: str = "") -> str:
    """Group reply after a wallet was created. NO key in here — ever."""
    name = esc(username or "trader")
    master = esc(master or master_username())
    return (
        f"🐾 @{name}, your <b>SUI wallet is ready!</b>\n\n"
        f"👉 Press <b>Start</b> on @{master} to collect your <b>private key</b> "
        f"— shown exactly once — and you land straight in <b>degen mode</b>.\n\n"
        f"⚠️ I never DM you your key, and I'll never ask for it here. "
        f"Keep it secret, keep it safe."
    )


def exists_reply(username: str = "", master: str = "", address: str = "") -> str:
    """Group reply when the tagger ALREADY has a wallet (idempotent).

    Says the tagger is already registered and — so they can deposit and buy
    without re-walking the Start flow — shows the PUBLIC deposit address.
    Deposit addresses are safe to print in a group; the PRIVATE key is never
    included (see wallet_credentials), only a Start-button nudge to reach it.
    """
    name = esc(username or "trader")
    master = esc(master or master_username())
    addr = esc(address or "")
    if addr:
        return (
            f"🐾 @{name} you're <b>already a user</b> — your wallet is ready.\n\n"
            f"🏦 <b>Deposit &amp; buy:</b> send SUI to\n"
            f"<code>{addr}</code>\n"
            f"then head to <b>Degen Hub</b> on @{master} to trade it.\n\n"
            f"🔑 Keep trading from <b>Start</b> on @{master} to see your key."
        )
    return (
        f"🐾 @{name} you're <b>already a user</b> — your wallet is ready.\n\n"
        f"👉 Open <b>Start</b> on @{master} to deposit, buy and see your key."
    )


def failed_reply(username: str = "", master: str = "") -> str:
    name = esc(username or "trader")
    master = esc(master or master_username())
    return (
        f"⚠️ @{name}, I couldn't create your wallet just yet.\n\n"
        f"👉 Press <b>Start</b> on @{master} and we'll finish setup there."
    )


def provision_wallet(bot_id: int, network: str = "mainnet") -> tuple[str, str]:
    """Create or reuse the Sui wallet for bot_id in exec_ledger.

    Returns (address, private_key). (None, None) on failure. Reuses the exact
    key-generation + Fernet encryption path from onboarding (userbot.py) so a
    tag-created wallet is indistinguishable from a wizard-created one.
    """
    try:
        from exec_vault import ExecVault, generate_key_material
        from ledger import ExecLedger
    except Exception:
        return None, None
    ledger = ExecLedger(os.environ.get("EXEC_LEDGER_PATH", "exec_ledger.db"))
    try:
        existing = ledger.wallet_by_bot_chain(bot_id, "sui")
        if existing and existing.get("key_enc"):
            return existing["address"], ExecVault().decrypt(existing["key_enc"])
        address, key_hex = generate_key_material("sui")
        enc = ExecVault().encrypt(key_hex)
        try:
            ledger.upsert_wallet(bot_id, "sui", address, address, enc,
                                 ExecVault.key_hash(key_hex))
        except ValueError:
            # one keypair per bot already held by THIS bot -> reuse it
            existing = ledger.wallet_by_bot_chain(bot_id, "sui")
            if existing and existing.get("key_enc"):
                return existing["address"], ExecVault().decrypt(existing["key_enc"])
            return None, None
        return address, key_hex
    finally:
        try:
            ledger.close()
        except Exception:
            pass


def wallet_credentials(bot_id: int) -> tuple[str, str]:
    """(address, private_key) for an existing bot from exec_ledger, else ('','')."""
    try:
        from exec_vault import ExecVault
        from ledger import ExecLedger

        ledger = ExecLedger(os.environ.get("EXEC_LEDGER_PATH", "exec_ledger.db"))
        try:
            row = ledger.wallet_by_bot_chain(bot_id, "sui")
            if not row or not row.get("key_enc"):
                return "", ""
            return row["address"], ExecVault().decrypt(row["key_enc"])
        finally:
            try:
                ledger.close()
            except Exception:
                pass
    except Exception:
        return "", ""


def enable_degen_defaults(bot_id: int) -> bool:
    """Turn on the degen engine for this bot: enabled + degen VIEW + 40 SUI budget.

    set_config() only writes `view` on the UPDATE branch (the INSERT default is
    0), so a fresh config row is created with budget first, then enabled+view.
    Returns False when the degen engine is off (env-gated) — the tag still
    created the wallet, the user just lands on the classic dashboard.
    """
    try:
        from degen import runtime as _rt
        if not _rt.env_on():
            return False
        led = _rt.get_ledger()
        cfg = led.get_config(bot_id)
        if not float(cfg.get("budget_sui") or 0):
            led.set_config(bot_id, budget_sui=40.0)
        led.set_config(bot_id, enabled=1, view=1)
        return True
    except Exception:
        return False