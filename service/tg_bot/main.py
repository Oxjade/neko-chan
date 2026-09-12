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
from telegram.ext import Application, ContextTypes, TypeHandler, ApplicationHandlerStop

import tg_config as cfg
from key_vault import KeyVault
from store import Registry
from platform_client import PlatformClient
from userbot import UserBotController
from agent_pool import AgentPool
from gateway import ExecGateway
from ledger import ExecLedger
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

    # Master router: single-bot serving. Every update the master's own handlers
    # did NOT claim (dashboard callbacks, onboarding, text on a bot screen) is
    # forwarded into the OWNING bot's Application, whose send identity is the
    # master token - so the whole per-bot dashboard runs inside @Neko_tradesbot
    # with zero handler rewrites. Registered last in group 0 so specific master
    # commands (/start, nav:, admin:) win first.
    async def _router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await userbot.route(update, context.chat_data.get("active_bot_id")):
            raise ApplicationHandlerStop
        user = getattr(update, "effective_user", None)
        if not user:
            return
        bots = registry.bots_for(user.id)
        if len(bots) > 1:
            # multi-bot owner with no active bot: show a switcher, then route
            import telegram as _tg
            kb = [[_tg.InlineKeyboardButton(b["bot_name"],
                                            callback_data=f"switch:{b['id']}")]
                  for b in bots]
            msg = (getattr(update, "effective_message", None)
                   or getattr(getattr(update, "callback_query", None), "message", None))
            if msg:
                await msg.reply_text("🐾 Which cat do you want to drive?",
                                     reply_markup=_tg.InlineKeyboardMarkup(kb))

    async def _switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        bot_id = int(q.data.split(":", 1)[1])
        if bot_id not in {b["id"] for b in registry.bots_for(q.from_user.id)}:
            return
        context.chat_data["active_bot_id"] = bot_id
        b = registry.get_bot(bot_id)
        await q.edit_message_text(f"🐈 Now driving {b['bot_name']}.")

    # Order within group 0 decides precedence: master commands (registered
    # above) and the switcher win first; the router is the last catch-all, so
    # only updates nothing else claimed are forwarded to a bot Application.
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(_switch, pattern=r"^switch:\d+$"))
    app.add_handler(TypeHandler(Update, _router))
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
        # Master-only model: a token-less bot pushes via the master token
        # (Notifier resolves the fallback). Do NOT skip it — that would leave
        # migrated users silent. chat_id is the owner's own tg_id.
        bot_token = registry.bot_token(bot["id"])
        watcher = Watcher(
            db_path=db_path,
            notify=notifier,
            registry=registry,
            bot_id=bot["id"],
            tg_id=bot["tg_id"],
            bot_token=bot_token,
            chat_id=bot["tg_id"],
            platform_base=platform.base,
            start_equity=0.0,
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
        # token-less (master-only) bot: fall back to the master sender so the
        # owner still receives the deletion notice.
        if not registry.bot_token(bot["id"]):
            _notify_owner_via_master(bot, message)
            return
        try:
            token = registry.bot_token(bot["id"])
            if not token:
                return
            import requests as _r
            _r.post(f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": bot["tg_id"], "text": message,
                          "parse_mode": "HTML"}, timeout=15)
        except Exception:
            pass

    def _notify_owner_via_master(bot: dict, message: str):
        """Notify through the MASTER bot - the user's own bot token may be
        dead (that is often WHY onboarding was never finished)."""
        try:
            import os as _os
            token = _os.environ.get("TG_BOT_TOKEN") or cfg.require_master_token()
            import requests as _r
            _r.post(f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": bot["tg_id"], "text": message,
                          "parse_mode": "HTML"}, timeout=15)
        except Exception:
            pass

    def _purge_incomplete_onboarding(registry: Registry, deadline_minutes: int = 30):
        """Delete bots whose onboarding never completed (operator rule: 30 min).

        A bot with onboarding_complete=0 has no confirmed wallet, no trading
        chain, and (by the key-cancel rule) usually no AI key - it just idles
        a poller. Safe-deletes only: a bot with ANY exec order/fill is kept
        (it traded, so its owner must relink manually instead)."""
        from datetime import datetime, timedelta, timezone as _tz
        cutoff = datetime.now(_tz.utc) - timedelta(minutes=deadline_minutes)
        for bot in registry.all_bots():
            if bot.get("onboarding_complete"):
                continue
            created = bot.get("created_at") or ""
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                continue
            if created_dt > cutoff:
                continue
            if registry.get_active_key(bot.get("tg_id")):
                continue  # has an AI key - keep it despite incomplete onboarding
            # trade-history guard: a bot that ever filled keeps its records
            try:
                ledger = ExecLedger(os.environ.get("EXEC_LEDGER_PATH", "exec_ledger.db"))
                orders = ledger._conn.execute(
                    "SELECT COUNT(*) c FROM exec_orders WHERE bot_id=?", (bot["id"],)).fetchone()["c"]
                fills = ledger._conn.execute(
                    "SELECT COUNT(*) c FROM exec_fills WHERE order_id IN "
                    "(SELECT id FROM exec_orders WHERE bot_id=?)", (bot["id"],)).fetchone()["c"]
                if orders or fills:
                    log.info("[cleanup] bot %s has trade history - not auto-deleted", bot["id"])
                    continue
                # purge the (unused) wallet key material with the bot
                ledger._conn.execute("DELETE FROM exec_wallets WHERE bot_id=?", (bot["id"],))
                ledger._conn.commit()
                ledger.close()
            except Exception as exc:
                log.warning("[cleanup] exec-ledger purge failed for bot %s: %s", bot["id"], exc)
            _notify_owner_via_master(bot, (
                f"🗑️ Bot <b>{bot.get('bot_name')}</b> was removed because setup "
                f"was never completed within {deadline_minutes} minutes.\n\n"
                "You can re-add it anytime from the master bot with /addbot."))
            try:
                userbot.stop_bot(bot["id"])
            except Exception:
                pass
            try:
                agent_pool.stop(bot["id"])
            except Exception:
                pass
            registry.delete_bot(bot["id"], bot["tg_id"])
            log.info("[cleanup] removed incomplete-onboarding bot %s (%s)",
                     bot["id"], bot.get("bot_name"))

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
            try:
                _purge_incomplete_onboarding(registry)
            except Exception as exc:
                log.warning("[cleanup] onboarding sweep error: %s", exc)
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