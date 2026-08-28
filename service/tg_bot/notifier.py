"""Push notifications with dedup and batching (event ledger in registry)."""

import os
import time
import tempfile

import requests

from store import utcnow
from messages import NOTIF


class Notifier:
    def __init__(self, registry):
        self.registry = registry

    def _send(self, bot_token: str, chat_id: int, text: str,
              buttons: list[list[str]] | None = None) -> bool:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": b, "callback_data": f"sb:{cb}"} for b, cb in row]
                                    for row in buttons]
            }
        try:
            r = requests.post(url, json=payload, timeout=20)
            return r.status_code == 200
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
                if buttons:
                    data["reply_markup"] = {
                        "inline_keyboard": [[{"text": b, "callback_data": f"sb:{cb}"}
                                             for b, cb in row] for row in buttons]
                    }
                r = requests.post(url, data=data, files=files, timeout=30)
                return r.status_code == 200
        except requests.RequestException:
            return False

    def notify(self, bot_id: int, tg_id: int, bot_token: str, chat_id: int,
               kind: str, ref_id: str, text: str,
               buttons: list[list[str]] | None = None, dedup: bool = True,
               photo_path: str | None = None) -> bool:
        """Push one event. dedup=True drops repeats of the same (kind, ref_id).
        If photo_path is set, sends the photo with caption instead of plain text."""
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