"""Central configuration for the AI-Trader Telegram bot network."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]  # repo root

# Local secrets (gitignored)
load_dotenv(BASE_DIR / "service" / "tg_bot" / ".env")
load_dotenv(BASE_DIR / ".env")

# Master bot (operator's) Telegram token - required to poll
MASTER_BOT_TOKEN = os.getenv("TG_MASTER_TOKEN", "")

# Registry + vault
REGISTRY_PATH = Path(os.getenv("TG_REGISTRY_PATH", str(BASE_DIR / "service" / "tg_bot" / "registry.db")))
VAULT_MASTER_KEY = os.getenv("TG_VAULT_MASTER_KEY", "")  # Fernet key; required

# AI-Trader platform
PLATFORM_URL = os.getenv("AI_TRADER_URL", "http://127.0.0.1:8000")
PLATFORM_REGISTER_PASSWORD = os.getenv("TG_PLATFORM_PASSWORD", "tg-network-2026")

# Operator allowlist for /admin
ADMIN_TG_IDS = {int(x) for x in os.getenv("TG_ADMIN_IDS", "").split(",") if x.strip()}

# Runner (agent) defaults
RUNNER_SCRIPT = BASE_DIR / "service" / "agent" / "live_agent.py"
RUNNER_INTERVAL_DEFAULT = 120
RUNNER_INTERVALS = {60, 120, 300, 600}

# Risk presets
RISK_PRESETS = {
    "conservative": {"max_daily_trades": 4, "max_position_pct": 20, "force_stop_pct": 5, "active_mode": 0},
    "balanced": {"max_daily_trades": 8, "max_position_pct": 30, "force_stop_pct": 5, "active_mode": 1},
    "aggressive": {"max_daily_trades": 16, "max_position_pct": 40, "force_stop_pct": 3, "active_mode": 1},
}

# Markets
MARKET_OPTIONS = {"perps": "⚡ Perps", "spot": "₿ Spot", "us-stock": "📈 US Stocks", "forex": "💱 Forex"}
LEVERAGE_OPTIONS = (1, 2, 5, 10)

# Provider presets for AI keys
PROVIDER_PRESETS = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "openrouter/auto"},
    "opencode-go": {"base_url": os.getenv("TG_OPENCODE_GO_URL", "https://opencode.ai/api/v1"), "model": "opencode-go/deepseek-v4-flash"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "claude": {"base_url": "https://api.anthropic.com/v1", "model": "claude-3-5-sonnet-latest"},
}


def require_master_token() -> str:
    # read lazily so tests/operators can set it after import
    token = os.getenv("TG_MASTER_TOKEN", "")
    if not token:
        raise RuntimeError(
            "TG_MASTER_TOKEN is not set. Create the master bot in @BotFather and export its token."
        )
    return token


def require_vault_key() -> bytes:
    # read lazily so tests/operators can set it after import
    key = os.getenv("TG_VAULT_MASTER_KEY", "")
    if not key:
        raise RuntimeError("TG_VAULT_MASTER_KEY is not set (Fernet key). Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
    return key.encode()