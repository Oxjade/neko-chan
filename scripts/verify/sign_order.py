"""Gate G16+G17: build a valid OrderIntent and sign a PersonalMessage with the user wallet."""
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "service", "execution"))

from dotenv import load_dotenv

load_dotenv(os.path.join(REPO, ".env"))
load_dotenv(os.path.join(REPO, "service", "tg_bot", ".env"))

import sqlite3

from exec_vault import ExecVault
from order_model import OrderIntent

# --- G16: OrderIntent validates for a real perp trade ---
intent = OrderIntent(chain="sui", venue="aftermath-perp", symbol="BTC", side="buy", qty=0.001,
                     order_type="market", stop_loss=70000.0, take_profit=90000.0,
                     leverage=20.0, idempotency_key="verify:btc:buy:1")
errs = intent.validate(78000.0)
assert not errs, f"validation failed: {errs}"
print("INTENT: PASS venue=%s lev=%s notional=%.1f" % (intent.venue, intent.leverage, intent.notional(78000.0)))

# --- G17: decrypt wallet key, build adapter, sign a PersonalMessage ---
db = sqlite3.connect(os.path.join(REPO, "exec_ledger.db"))
db.row_factory = sqlite3.Row
w = db.execute("SELECT key_enc FROM exec_wallets WHERE chain='sui' LIMIT 1").fetchone()
db.close()
assert w, "no sui wallet in ledger"
key = ExecVault().decrypt(w["key_enc"])
assert key.startswith("suiprivkey1"), "invalid sui key"
print("key: OK", key[:14] + "...")

from aftermath_adapter import _sign_terms_message, build_aftermath
from ledger import ExecLedger

ledger = ExecLedger(os.path.join(REPO, "exec_ledger.db"))
adapter = build_aftermath(ledger, key)
print("adapter addr:", adapter.address[:16] + "...")
bytes_b64, sig = _sign_terms_message(adapter.seed, adapter.pubkey)
assert len(sig) > 50, "signature too short"
print("signature:", str(sig)[:24] + "...")
print("SIGN ORDER: PASS")