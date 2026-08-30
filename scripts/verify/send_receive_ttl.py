"""Verify send/receive + notification auto-delete (5-minute TTL)."""
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(REPO, "service", "tg_bot", "userbot.py")
NOTIFIER = os.path.join(REPO, "service", "tg_bot", "notifier.py")

src = open(SRC).read()
nof = open(NOTIFIER).read()

print("== TTL definitions ==")
m = re.search(r"MSG_TTL_SECONDS = int\(os.getenv\(\"TG_MSG_TTL_SECONDS\", \"(\d+)\"\)\)", nof)
ttl = int(m.group(1)) if m else None
print("notifier MSG_TTL_SECONDS:", ttl)
assert ttl == 300, f"expected 300, got {ttl}"

print("\n== notification send paths schedule delete ==")
# _send and _send_photo both call _schedule_delete
assert "_schedule_delete(bot_token, chat_id, mid)" in nof
print("notifier._send/_send_photo schedule delete: YES")

print("\n== P&L card photo schedules delete ==")
assert "_delayed_photo_delete(" in src
assert "def _delayed_photo_delete(bot_token: str, chat_id: int, message_id: int," in src
m2 = re.search(r"def _delayed_photo_delete\([^)]*\):\s*\n.*?ttl: int = (\d+)\)", src, re.S)
print("P&L card TTL:", 300 if "ttl: int = 300" in src else "?")
print("P&L card delete scheduled: YES")

print("\n== Receive QR photo schedules delete? ==")
recv = src.split("async def receive(")[1].split("\n        async def gen_wallet")[0]
has_recv_delete = "_delayed_photo_delete" in recv or "_schedule_delete" in recv
print("receive schedules delete:", has_recv_delete)
assert has_recv_delete, "receive QR photo does NOT auto-delete"

print("\n== Send confirmation schedules delete? ==")
send = src.split("async def send_amount(")[1].split("\n        async def send_cancel")[0]
has_send_delete = "_delayed_photo_delete" in send or "_schedule_delete" in send
print("send schedules delete:", has_send_delete)
assert has_send_delete, "send confirmation does NOT auto-delete"

print("\nSEND/RECEIVE TTL: PASS")
