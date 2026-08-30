"""AI-Trader Telegram Bot Network - entry point.

Master bot (polling) + user bot serving + agent pool + notifier.

Run:
  export TG_MASTER_TOKEN=... TG_VAULT_MASTER_KEY=...
  python service/tg_bot/main.py
"""

import logging
import os
import sys
import threading
import time

import requests

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "execution"))

from telegram import Update
from telegram.ext import Application, ContextTypes

import tg_config as cfg
from key_vault import KeyVault
from store import Registry
from platform_client import PlatformClient
from userbot import UserBotController
from agent_pool import AgentPool
from gateway import ExecGateway
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

    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unhandled error: %s", context.error, exc_info=context.error)
        if update and isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Internal error. The cat tripped.")

    app.add_error_handler(_error_handler)
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


def start_bot_cleanup(registry: Registry, userbot: UserBotController,
                      agent_pool: AgentPool, deadline_hours: int = 3,
                      poll_seconds: int = 60) -> threading.Thread:
    """Janitor: delete unconfigured bots whose owner never added an AI key.

    When a user cancels/declines key setup, the bot is scheduled for deletion
    `deadline_hours` later (see userbot.key_cancel). This loop enforces it:
    due bots are stopped, deleted from the registry, and their owner is
    notified on the master bot (so they can re-add if they change their mind).
    Keeps the network free of idle load-bots.
    """
    import threading as _threading

    def _notify_owner(bot: dict, message: str):
        try:
            token = registry.bot_token(bot["id"])
            if not token:
                return
            import requests as _r
            _r.post(f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": bot["tg_id"], "text": message}, timeout=15)
        except Exception:
            pass

    def _loop():
        while True:
            try:
                for bot in registry.due_bot_deletions():
                    bot_id = bot["id"]
                    _notify_owner(bot, (
                        f"🗑️ Bot <b>{bot['bot_name']}</b> was removed from the network "
                        f"because no AI key was added within {deadline_hours} hours.\n\n"
                        "You can re-add it anytime from the master bot."))
                    try:
                        userbot.stop_bot(bot_id)
                    except Exception:
                        pass
                    try:
                        agent_pool.stop(bot_id)
                    except Exception:
                        pass
                    registry.delete_bot(bot_id, bot["tg_id"])
                    log.info("[cleanup] removed unconfigured bot %s (%s)", bot_id, bot.get("bot_name"))
            except Exception as exc:
                log.warning("[cleanup] sweep error: %s", exc)
            # Restart crashed agents: the pool is only touched at boot and on
            # explicit commands, so a dead live_agent (e.g. a startup race
            # against the API server) would otherwise stay dead forever and the
            # bot silently stops trading. healthcheck() respawns crashed
            # runners up to the per-hour cap.
            try:
                agent_pool.healthcheck()
            except Exception as exc:
                log.warning("[cleanup] agent healthcheck error: %s", exc)
            time.sleep(poll_seconds)

    t = _threading.Thread(target=_loop, name="bot-cleanup", daemon=True)
    t.start()
    return t


def main():
    vault = KeyVault()
    registry = Registry(cfg.REGISTRY_PATH, vault)
    platform = PlatformClient()
    agent_pool = AgentPool(registry)
    gateway = ExecGateway.build()
    if gateway.ready:
        log.info("execution gateway ready: chains=%s", list(gateway.adapters.keys()))
    userbot = UserBotController(registry, platform, vault=vault, agent_pool=agent_pool,
                                gateway=gateway)

    app = build_app(registry, platform, vault, userbot, agent_pool)
    userbot.start_all()
    agent_pool.start_all_active()
    start_watchers(registry, platform)
    start_bot_cleanup(registry, userbot, agent_pool)

    log.info("Master bot starting (polling)...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()