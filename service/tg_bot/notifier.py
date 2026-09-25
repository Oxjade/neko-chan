"""Push notifications with dedup and batching (event ledger in registry).

Notifications are stored in the registry (the dashboard's Notifications button
reads them) AND pushed to the chat with a TTL: the chat message self-destructs
after MSG_TTL_SECONDS so the chat stays clean while the record survives in the
inbox.
"""

import os
import threading
import time
import tempfile

import requests

from store import utcnow
from messages import NOTIF

# Chat messages self-destruct after this many seconds (default 3 minutes).
# The notification REMAINS stored in the registry inbox regardless.
MSG_TTL_SECONDS = int(os.getenv("TG_MSG_TTL_SECONDS", "180"))


class SendBudget:
    """Rate limiter that keeps the SINGLE master bot inside Telegram's send
    limits when it fans out notifications for many users at once.

    Two independent ceilings (Telegram's documented bot limits, with headroom):
      * GLOBAL: ~30 messages/sec across all chats -> default `global_rps` 25.
      * PER-CHAT: ~1 message/sec to one chat, and a slow ~20-30/min burst cap
        -> default `per_chat_spacing` 1.0s (chat A's flood can't starve chat B).

    `acquire()` BLOCKS the calling (watcher) thread until it is safe to send,
    which is exactly what we want: pushes are smoothed, never dropped, and one
    chatty user can't 429 the whole fleet. Injectable clock/sleep for tests."""

    def __init__(self, global_rps: float = 25.0, per_chat_spacing: float = 1.0,
                 *, _sleep=time.sleep, _now=time.monotonic):
        self._rate = global_rps
        self._spacing = per_chat_spacing
        self._tokens = global_rps          # start with a full second's worth
        self._last_refill = _now()
        self._last_per_chat: dict[int, float] = {}
        self._lock = threading.Lock()
        self._sleep = _sleep
        self._now = _now

    def _refill(self, now: float) -> None:
        self._tokens = min(self._rate, self._tokens + (now - self._last_refill) * self._rate)
        self._last_refill = now

    def acquire(self, chat_id: int) -> None:
        # per-chat spacing first (cheap, avoids same-chat pile-ups)
        with self._lock:
            now = self._now()
            wait_chat = max(0.0, self._spacing - (now - self._last_per_chat.get(chat_id, float("-inf"))))
        if wait_chat:
            self._sleep(wait_chat)
        # global budget: wait for a token
        while True:
            with self._lock:
                self._refill(self._now())
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._last_per_chat[chat_id] = self._now()
                    return
                need = (1.0 - self._tokens) / self._rate
            self._sleep(max(0.01, need))

    def on_429(self, retry_after: float) -> None:
        """A real Telegram 429 means we overshot despite the budget: pause all
        sends for the requested cooldown (capped so one bad actor can't freeze
        the process)."""
        self._sleep(min(float(retry_after or 1.0), 30.0))


# Group chats (supergroups/groups) keep messages: the chat is the shared record,
# so trading alerts must stay visible instead of self-destructing.
PERSISTENT_CHAT_IDS = {
    int(x) for x in os.getenv("TG_PERSISTENT_CHAT_IDS", "").replace(",", " ").split() if x
}


def _schedule_delete(bot_token: str, chat_id: int, message_id: int,
                     ttl: int = MSG_TTL_SECONDS) -> None:
    """Delete a sent message after ttl seconds, in a background thread.

    Skipped entirely for chats in PERSISTENT_CHAT_IDS (the GC) so alerts there
    stay in the chat as the shared record."""
    if chat_id in PERSISTENT_CHAT_IDS:
        return
    if ttl <= 0:
        return

    def _delete():
        time.sleep(ttl)
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
                timeout=10,
            )
        except requests.RequestException:
            pass

    threading.Thread(target=_delete, daemon=True).start()


def _normalize_buttons(buttons):
    """Accept either [("Label", "cb"), ...] or ["Label", "cb"] rows.

    Returns inline_keyboard payload for Telegram. Handles a flat pair row like
    ["Label", "callback"] and a tuple row like [("Label", "callback")]."""
    rows = []
    for row in buttons or []:
        items = []
        for cell in row:
            if isinstance(cell, (tuple, list)) and len(cell) == 2:
                label, cb = cell
            elif isinstance(cell, str):
                label, cb = cell, ""
            else:
                continue
            items.append({"text": label, "callback_data": f"sb:{cb}" if cb and not cb.startswith("sb:") else cb})
        if items:
            rows.append(items)
    return {"inline_keyboard": rows} if rows else None


class Notifier:
    def __init__(self, registry, budget: "SendBudget | None" = None):
        self.registry = registry
        # A shared budget makes one master token safe across many users. When
        # None (e.g. unit tests, single-legacy-bot use) sends are unpaced.
        self.budget = budget

    def _token(self, bot_token: str | None) -> str:
        """Send identity for the single-master-bot model. A token-less bot
        (created without a @BotFather token) pushes through the MASTER token;
        a legacy bot that still has its own token keeps using it. This keeps
        per-user @BotFather tokens optional without breaking existing pushes."""
        if bot_token:
            return bot_token
        try:
            import tg_config as _cfg
            return _cfg.MASTER_BOT_TOKEN or _cfg.require_master_token()
        except Exception:
            return ""

    def _post(self, url, *, budget_chat: int | None, **kw):
        """Single send choke-point: pace via the shared budget, then honor a
        real Telegram 429 by cooling down once and retrying. Returns the raw
        response or None on network failure."""
        if self.budget is not None and budget_chat is not None:
            self.budget.acquire(budget_chat)
        try:
            r = requests.post(url, timeout=20, **kw)
        except requests.RequestException:
            return None
        if r.status_code == 429 and self.budget is not None:
            try:
                ra = (r.json().get("parameters") or {}).get("retry_after", 1)
            except Exception:
                ra = 1
            self.budget.on_429(ra)
            try:
                r = requests.post(url, timeout=20, **kw)
            except requests.RequestException:
                return None
        return r

    def _send(self, bot_token: str, chat_id: int, text: str,
              buttons: list[list[str]] | None = None) -> bool:
        token = self._token(bot_token)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        markup = _normalize_buttons(buttons)
        if markup:
            payload["reply_markup"] = markup
        r = self._post(url, budget_chat=chat_id, json=payload)
        if r is not None and r.status_code == 200:
            mid = r.json().get("result", {}).get("message_id")
            if mid:
                _schedule_delete(token, chat_id, mid)
            return True
        return False

    def _send_photo(self, bot_token: str, chat_id: int, photo_path: str,
                    caption: str = "", buttons: list[list[str]] | None = None) -> bool:
        """Send a photo (PNG card) with optional caption + inline buttons."""
        token = self._token(bot_token)
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        if self.budget is not None:
            self.budget.acquire(chat_id)  # cards count against the same global cap
        try:
            with open(photo_path, "rb") as f:
                files = {"photo": f}
                data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
                markup = _normalize_buttons(buttons)
                if markup:
                    data["reply_markup"] = markup
                r = requests.post(url, data=data, files=files, timeout=30)
                if r.status_code == 200:
                    mid = r.json().get("result", {}).get("message_id")
                    if mid:
                        _schedule_delete(token, chat_id, mid)
                    return True
                return False
        except requests.RequestException:
            return False

    def notify(self, bot_id: int, tg_id: int, bot_token: str, chat_id: int,
               kind: str, ref_id: str, text: str,
               buttons: list[list[str]] | None = None, dedup: bool = True,
               photo_path: str | None = None) -> bool:
        """Push one event. dedup=True drops repeats of the same (kind, ref_id).
        If photo_path is set, sends the photo with caption instead of plain text.
        Every event is recorded in the registry for dedup; the dashboard's
        Notifications button filters to trade events only."""
        if dedup and self.registry.event_seen(tg_id, kind, ref_id):
            return False
        if photo_path:
            ok = self._send_photo(bot_token, chat_id, photo_path, text, buttons)
        else:
            ok = self._send(bot_token, chat_id, text, buttons)
        if ok:
            self.registry.mark_event(tg_id, kind, ref_id, {"text": text})
        return ok

    def error_event(self, bot_id: int, tg_id: int, bot_token: str, chat_id: int,
                    message: str, error_count: int) -> bool:
        """First error pings; repeats merge into one 'still retrying' message."""
        if error_count <= 1:
            return self.notify(bot_id, tg_id, bot_token, chat_id, "error", "first",
                               NOTIF["error_first"].format(message=message))
        if error_count % 5 == 0:  # refresh the batched notice every 5
            return self._send(bot_token, chat_id, NOTIF["error_batch"].format(n=error_count))
        return False

    def daily_summary(self, bot_id: int, tg_id: int, bot_token: str, chat_id: int,
                      pnl: float, trades: int, win: float, fees: float = 0.0) -> bool:
        ref = time.strftime("%Y-%m-%d")
        return self.notify(bot_id, tg_id, bot_token, chat_id, "daily", ref,
                           NOTIF["daily"].format(pnl=f"{pnl:+.2f}", trades=trades,
                                                 win=f"{win:.0f}"))