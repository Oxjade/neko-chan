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
MSG_TTL_SECONDS = int(os.getenv("TG_MSG_TTL_SECONDS", "300"))


def _schedule_delete(bot_token: str, chat_id: int, message_id: int,
                     ttl: int = MSG_TTL_SECONDS) -> None:
    """Delete a sent message after ttl seconds, in a background thread."""

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
    def __init__(self, registry):
        self.registry = registry

    def _send(self, bot_token: str, chat_id: int, text: str,
              buttons: list[list[str]] | None = None) -> bool:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        markup = _normalize_buttons(buttons)
        if markup:
            payload["reply_markup"] = markup
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                mid = r.json().get("result", {}).get("message_id")
                if mid:
                    _schedule_delete(bot_token, chat_id, mid)
                return True
            return False
        except requests.RequestException:
            return False

    def _send_photo(self, bot_token: str, chat_id: int, photo_path: str,
                    caption: str = "", buttons: list[list[str]] | None = None) -> bool:
        """Send a photo (PNG card) with optional caption + inline buttons."""
        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
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
                        _schedule_delete(bot_token, chat_id, mid)
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
                      pnl: float, trades: int, win: float, fees: float) -> bool:
        ref = time.strftime("%Y-%m-%d")
        return self.notify(bot_id, tg_id, bot_token, chat_id, "daily", ref,
                           NOTIF["daily"].format(pnl=f"{pnl:+.2f}", trades=trades,
                                                 win=f"{win:.0f}", fees=f"{fees:.2f}"))