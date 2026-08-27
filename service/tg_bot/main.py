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


def start_watchers(registry: Registry, platform: PlatformClient):
    """One watcher thread per running bot → smart event pushes (fills/closes/
    stops/targets/milestones) with dedup + batching.

    The push goes to the bot's OWN master chat (user.tg_id) — the operator's
    chat — via its own bot token, so it works even when the user is chatting
    with the master bot. Deliberately idempotent: watcher watermark + registry
    event ledger mean restarts never double-push.
    """
    from notifier import Notifier
    from watcher import Watcher
    from tg_config import BASE_DIR
    import threading as _threading

    notifier = Notifier(registry)
    paths = {
        "sqlite": str(BASE_DIR / "service" / "server" / "data" / "clawtrader.db"),
    }
    db_path = paths["sqlite"]
    threads = []
    for bot in registry.all_bots():
        if not bot.get("is_running") or not bot.get("agent_id"):
            continue
        bot_token = registry.bot_token(bot["id"])
        if not bot_token:
            continue
        watcher = Watcher(
            db_path=db_path,
            notify=notifier,
            registry=registry,
            bot_id=bot["id"],
            tg_id=bot["tg_id"],
            bot_token=bot_token,
            chat_id=bot["tg_id"],
            platform_base=platform.base,
            start_equity=100_000.0,
        )
        t = _threading.Thread(target=watcher.run, name=f"watcher-{bot['id']}", daemon=True)
        t.start()
        threads.append(t)
        log.info("[watcher] started for bot %s (agent %s)", bot["id"], bot.get("agent_id"))
    return threads


def main():
    vault = KeyVault()
    registry = Registry(cfg.REGISTRY_PATH, vault)
    platform = PlatformClient()
    agent_pool = AgentPool(registry)
    userbot = UserBotController(registry, platform, vault=vault, agent_pool=agent_pool)

    app = build_app(registry, platform, vault, userbot, agent_pool)
    userbot.start_all()
    agent_pool.start_all_active()
    start_watchers(registry, platform)

    log.info("Master bot starting (polling)...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()