"""Gate G1+G2: build the execution gateway + Bluefin adapter from the encrypted ledger key."""
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "service", "execution"))

# Load env like the runtime does (master key lives in .env)
from dotenv import load_dotenv

load_dotenv(os.path.join(REPO, ".env"))
load_dotenv(os.path.join(REPO, "service", "tg_bot", ".env"))

os.environ["REAL_TRADING_ENABLED"] = "1"
os.environ["EXEC_LEDGER_PATH"] = os.path.join(REPO, "exec_ledger.db")

from gateway import ExecGateway

gw = ExecGateway.build()
print("ready:", gw.ready)
print("adapters:", list(gw.adapters.keys()))
assert gw.ready, "gateway not ready"
assert "sui" in gw.adapters, "sui adapter missing"

# Verify the Bluefin adapter inside the SUIAdapter
sui_adapter = gw.adapters["sui"]
bf = getattr(sui_adapter, "bluefin", None)
assert bf is not None, "no Bluefin adapter wired"
print("bluefin:", type(bf).__name__)
print("address:", bf.address[:16] + "...")
print("GATEWAY+ADAPTER: PASS")
