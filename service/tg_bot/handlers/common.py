"""Shared helpers: keyboards, ownership verification, token lookups."""

import secrets
import time

import requests

from tg_config import MARKET_OPTIONS
from messages import MENU

HOME = "🏠 Home"
BACK = "↩️ Back"
CANCEL = "❌ Cancel"


def menu_keyboard() -> list[list[str]]:
    return [
        [MENU["main_new"], MENU["how"]],
        [MENU["leaderboard"], MENU["help"]],
    ]


def home_keyboard() -> list[list[str]]:
    return [[HOME]]


def cancel_keyboard() -> list[list[str]]:
    return [[CANCEL]]


def retry_cancel() -> list[list[str]]:
    return [["↻ Retry", CANCEL]]


def markets_keyboard(current: dict) -> list[list[str]]:
    rows = []
    for key, label in MARKET_OPTIONS.items():
        mark = "✅" if current.get(key) else "⬜"
        rows.append([f"{mark} {label}"])
    rows.append(["✅ Done", CANCEL])
    return rows


def generate_verify_code() -> str:
    return f"VERIFY-{secrets.randbelow(9000) + 1000}"


class TokenInvalid(Exception):
    pass


def get_bot_username(token: str) -> str | None:
    """getMe for a bot token. Returns username or None (invalid)."""
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        data = r.json()
        if data.get("ok"):
            return data["result"].get("username")
    except requests.RequestException:
        pass
    return None


def _code_matches(received: str, code: str) -> bool:
    """Accept the full 'VERIFY-1234', just the digits '1234', or whitespace variants."""
    got = (received or "").strip().upper()
    want = (code or "").strip().upper()
    if got == want:
        return True
    want_digits = "".join(ch for ch in want if ch.isdigit())
    got_digits = "".join(ch for ch in got if ch.isdigit())
    return bool(want_digits) and got_digits == want_digits


def poll_for_verify_code(token: str, code: str, timeout_s: float = 90.0,
                         interval: float = 3.0) -> str:
    """Poll getUpdates on the user's bot until the challenge code arrives.

    Accepts the full 'VERIFY-1234' or just the digits '1234'.
    Returns 'verified' | 'timeout' | 'no_updates' | 'token_invalid' | 'network'.
    'no_updates' = the bot exists but has NO chat at all yet - the user
    probably hasn't pressed Start on it (a bot cannot receive messages before
    that, so the code can never arrive).
    """
    offset = 0
    deadline = time.time() + timeout_s
    saw_any = False
    while time.time() < deadline:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"timeout": 10, "offset": offset},
                timeout=15,
            )
        except requests.RequestException:
            time.sleep(interval)
            continue
        data = r.json()
        if not data.get("ok"):
            if data.get("error_code") == 401:
                return "token_invalid"
            time.sleep(interval)
            continue
        result = data.get("result", [])
        if result:
            saw_any = True
        for upd in result:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            text = msg.get("text", "") or ""
            if _code_matches(text, code):
                return "verified"
        time.sleep(interval)
    if not saw_any:
        return "no_updates"
    return "timeout"