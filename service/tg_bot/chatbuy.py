"""In-chat @neko buy: parser + receipt formatter + executor shell.

Pure functions live here; execute_chat_buy() touches the degen engine but
never the Telegram API — the caller owns the reply.
"""
from __future__ import annotations

import os
import re


def chat_usernames_from_env() -> list[str]:
    """Bot usernames that trigger an in-chat buy (NEKO_CHAT_USERNAMES env)."""
    raw = os.getenv("NEKO_CHAT_USERNAMES", "neko_tradesbot")
    return [u.strip().lstrip("@") for u in raw.split(",") if u.strip()]


# CA = a hex address, optionally a full Move type. The type suffix is restricted
# to the legal Move-type charset so a crafted "CA" can never inject into the
# GraphQL strings built upstream (TBP-04).
_CA_RE = re.compile(r"0x[0-9a-fA-F]{6,}(?:::[0-9A-Za-z_:<>,\.]+)?")
_AMT_RE = re.compile(r"(\d[\d,]*(?:\.\d*)?|\.\d+)")
_MENTION_RE = re.compile(r"(?i)^\s*@([A-Za-z0-9_]{3,64})\b(.*)$")


def parse_chat_buy(text: str, usernames: list[str]) -> dict | None:
    """Parse an in-chat @mention buy message.

    Returns:
        dict with keys when the message is addressed to one of our bots:
            {"amount": float, "ca": str, "mention": str}       — valid buy
            {"error": str, "mention": str}                      — malformed
        None — not a mention we recognise (caller should fall through).
    """
    if not text:
        return None

    m = _MENTION_RE.match(text)
    if not m:
        return None

    name = m.group(1).lower()
    if name not in {u.lower().lstrip("@") for u in usernames}:
        return None

    rest = m.group(2).strip()

    # must start with "buy" keyword
    if not re.match(r"(?i)^buy\b", rest):
        return None

    body = rest[re.match(r"(?i)^buy\s*", rest).end():].strip()

    # find the first 0x… address (the CA)
    ca_match = _CA_RE.search(body)
    if not ca_match:
        return {"error": "I need the token address (0x…).", "mention": m.group(1)}

    ca = ca_match.group(0)
    piece = (body[: ca_match.start()] + " " + body[ca_match.end() :]).strip()

    amount = _extract_amount(piece)
    if amount is None or amount <= 0:
        return {"error": 'I need an amount: e.g. "buy 5 sui <address>"', "mention": m.group(1)}

    return {"amount": round(amount, 6), "ca": ca, "mention": m.group(1)}


def _extract_amount(s: str) -> float | None:
    """Pull the first numeric token from *s*, ignoring 'SUI' suffixes."""
    s = re.sub(r"(?i)\bsui\b", "", s).strip()
    m = _AMT_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def esc(text) -> str:
    """Escape untrusted text for Telegram parse_mode=HTML."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def receipt_text(mode: str, *, username: str = "", amount: float = 0.0,
                 digest: str = "", mention: str = "") -> str:
    """Build the in-chat receipt (reply to the user's message)."""
    if mode == "private":
        return f"🐾 neko bought {amount:g} SUI"
    name = esc(username or "trader")
    mention = esc(mention or "neko_tradesbot")
    return (f"@{name}\n"
            f"🔗 https://suiscan.xyz/mainnet/tx/{digest}\n\n"
            f"trade with me @{mention}")


def execute_chat_buy(ui, bot_id: int, *, amount: float, ca: str,
                     idem: str, slip_bps: int = 500) -> dict:
    """Venue-triaged degen buy driven entirely by the raw CA.

    Auto-enables degen config for the bot on first use.  The caller must
    serialize calls per bot_id with an asyncio.Lock (two rapid buys on the
    same wallet risk picking the same SUI coin object).
    """
    from degen.constants import SUI_COIN_TYPE
    from degen.launchpad import resolve_input

    ch, led, ex = ui.ch, ui.led, ui.ex

    st = resolve_input(ch, ca)
    kind = getattr(st, "kind", "unknown") if st else "unknown"
    if st is None or kind not in ("curve", "pool", "generic"):
        return {"ok": False, "error": f"not buyable ({kind})", "kind": kind}
    if ex is None:
        return {"ok": False, "error": "degen engine offline", "kind": kind}

    # auto-enable degen on first chat buy (mirrors dg:on defaults)
    cfg = led.get_config(bot_id)
    if not cfg.get("enabled"):
        if not float(cfg.get("budget_sui") or 0):
            led.set_config(bot_id, budget_sui=40.0)
        led.set_config(bot_id, enabled=1)

    if ex.is_killed(bot_id):
        return {"ok": False, "error": "kill-switch engaged", "kind": kind}

    if kind == "curve":
        if not st.curve_obj:
            return {"ok": False, "error": "curve state unreadable", "kind": kind}
        res = ex.buy(
            bot_id,
            launchpad=st.launchpad or "suipump",
            curve_id=st.curve_id,
            token_type=st.token_type,
            curve_isv=st.curve_obj.get("shared_version", 0),
            sui_amount=float(amount),
            min_out=0,   # executor probes the chain (dry-run) to set the floor
            slip_bps=int(slip_bps),
            idem=idem,
            otype="market",
            target_price=None,
            source="chat",
        )
    elif kind == "pool":
        res = ex.buy_post_grad(
            bot_id, {"qty_sui": float(amount)}, st,
            side="buy", slip_bps=int(slip_bps), idem=idem,
        )
    else:  # generic
        atoms = int(float(amount) * 1e9)
        res = ex._dex_swap(
            bot_id, SUI_COIN_TYPE, st.token_type,
            atoms, int(slip_bps), idem,
            "buy", "generic", st.token_type, st.token_type,
        )

    res["kind"] = kind
    return res
