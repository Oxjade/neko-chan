"""AI-Trader Telegram Bot Network - entry point.

Master bot (polling) + user bot serving + agent pool + notifier.

Run:
  export TG_MASTER_TOKEN=... TG_VAULT_MASTER_KEY=...
  python service/tg_bot/main.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from telegram.ext import Application

import tg_config as cfg
from key_vault import KeyVault
from store import Registry
from platform_client import PlatformClient
from userbot import UserBotController
from agent_pool import AgentPool
from handlers.common import menu_keyboard
from handlers.master import register_master_handlers
from handlers.wizard import simple_flow_handlers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# keep bot tokens out of operational logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.vendor.ptb_urllib3.urllib3").setLevel(logging.WARNING)
log = logging.getLogger("tg_bot")


def menu_buttons() -> list[list[str]]:
    """Pure check used by tests: the main menu exists."""
    return menu_keyboard()


def build_app(registry: Registry, platform: PlatformClient, vault: KeyVault,
              userbot: UserBotController, agent_pool: AgentPool) -> Application:
    token = cfg.require_master_token()
    app = Application.builder().token(token).build()

    app.add_handler(simple_flow_handlers(registry, vault, platform, userbot, agent_pool))
    register_master_handlers(app, registry, platform, userbot)
    return app


def main():
    vault = KeyVault()
    registry = Registry(cfg.REGISTRY_PATH, vault)
    platform = PlatformClient()
    agent_pool = AgentPool(registry)
    userbot = UserBotController(registry, platform, vault=vault, agent_pool=agent_pool)

    app = build_app(registry, platform, vault, userbot, agent_pool)
    userbot.start_all()
    agent_pool.start_all_active()

    log.info("Master bot starting (polling)...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()