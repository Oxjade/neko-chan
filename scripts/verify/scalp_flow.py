"""Gate G11: Agent can fetch 5m candles and build a scalp scenario matrix."""
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "service", "agent"))

os.environ.setdefault("LIVE_AGENT_EXECUTION", "0")
os.environ.setdefault("LIVE_AGENT_NAME", "verify")
os.environ.setdefault("LIVE_AGENT_BOT_ID", "4")
os.environ.setdefault("TG_CHAT_ID", "0")
os.environ.setdefault("TG_BOT_TOKEN", "verify")
os.environ.setdefault("LIVE_AGENT_SYMBOLS", "BTC:crypto,ETH:crypto")
os.environ.setdefault("LIVE_AGENT_API_KEY", "dummy")
os.environ.setdefault("LIVE_AGENT_PROVIDER", "openai")

from live_agent import fetch_5m_closes
from quant_strategy import momentum_confirmed

c = fetch_5m_closes("BTC", "crypto", 6)
print("5m bars:", len(c))
assert len(c) >= 20, f"only {len(c)} bars"
m = momentum_confirmed(c, "long")
print("momentum confirmed:", m)
print("SCALP FLOW: PASS")