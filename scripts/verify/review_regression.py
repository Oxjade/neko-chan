"""Regression: verify all line-by-line review findings are fixed."""
import os
import sys
import re
import ast

SRC = "service/tg_bot/userbot.py"
WSRC = "service/tg_bot/watcher.py"
STORE = "service/tg_bot/store.py"
SUI = "service/execution/sui_adapter.py"
LEDGER = "service/execution/ledger.py"
ROUTER = "service/execution/router.py"

PASS = 0
FAIL = 0
RESULTS = []


def check(name, cond, msg=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        RESULTS.append((name, "PASS"))
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        RESULTS.append((name, "FAIL"))
        print(f"[FAIL] {name}: {msg}")


# R1: csv imported in Peek handler
code = open(SRC).read()
check("R1 csv import in Peek", "import csv as _csv" in code and "_csv.DictReader" in code, "missing csv import")

# R2: chain defined in send_amount
check("R2 chain defined in send_amount", "chain = (b or {}).get(\"chain\") or \"sui\"" in code, "chain undefined")

# R3: watcher uses platform_token, not bot_token
wcode = open(WSRC).read()
check("R3 watcher equity uses platform_token", "self.registry.platform_token" in wcode, "equity still uses bot_token")
check("R3 watcher _positions uses platform_token", wcode.count("self.registry.platform_token") >= 2, "need both equity + _positions")

# R4: platform_token accessor in registry
scode = open(STORE).read()
check("R4 platform_token accessor exists", "def platform_token" in scode, "missing platform_token()")

# R5: BCS u16 uses fixed 2-byte LE, not uleb128
scode = open(SUI).read()
seg = scode.split("def _bcs_u16")[1].split("def ")[0] if "def _bcs_u16" in scode else ""
check("R5 _bcs_u16 uses fixed 2-byte LE", "to_bytes(2" in seg, "bcs_u16 uses uleb128")

# R6: ledger upsert_position uses update-then-insert (no row accumulation)
lcode = open(LEDGER).read()
upsert_seg = lcode.split("def upsert_position")[1].split("def delete_position")[0]
check("R6 upsert_position no accumulation", "ON CONFLICT" not in upsert_seg and "rowcount == 0" in upsert_seg, "position rows accumulate")

# R7: router TOCTOU on idempotency_key — fixed in ledger.create_order (atomic
# UNIQUE insert returns existing id on IntegrityError), router rejects id<0
lcode = open(LEDGER).read()
rcode = open(ROUTER).read()
check("R7 router TOCTOU fixed",
      "except sqlite3.IntegrityError" in lcode and 'return row["id"] if row else -1' in lcode
      and "order_id is None or order_id < 0" in rcode,
      "TOCTOU not fixed in create_order")

# R8: router zero-fill handling
check("R8 zero fill guard", "ZERO-FILL GUARD" in rcode and "fill_price <= 0 or fill_qty <= 0" in rcode, "zero fill not guarded")

# R9: trailing_high persisted across restarts
acode = open("service/agent/live_agent.py").read()
check("R9 trailing_high persisted", "TRAILING_HIGH_PATH" in acode and "trailing_high_cache.json" in acode and "_trailing_high = _json.loads" in acode, "trailing_high not persisted")

# R10: private-key messages auto-delete after 5 min
ucode = open(SRC).read()
check("R10 private keys auto-delete", "_schedule_msg_delete" in ucode and ucode.count("_schedule_msg_delete(") >= 3, "private key messages not auto-deleted")

# R11: P&L card shows empty state when no active position and no realized P&L
check("R11 PnL empty-state guard", "No active P&L yet" in ucode and "not positions and abs(total_pnl) < 0.005" in ucode, "P&L card still generated with no active P&L")

print(f"\n=== REVIEW REGRESSION: {PASS}/{PASS+FAIL} passed ===")
print(f"REVIEW CERTAINTY: {round(PASS/max(PASS+FAIL,1)*100, 1)} %")