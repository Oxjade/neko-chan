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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

    from chattrack import chat_usernames_from_env as _track_names
    _neko_names = _track_names() or ["neko_tradesbot"]

    # In-chat wallet-tracker "buy it" button (alert CTA). Registered before the
    # router so the press is consumed here, not forwarded into a bot app.
    async def _on_tbuy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from tracker import get_tracker
        q = update.callback_query
        if q:
            await q.answer()
        _tr = get_tracker()
        if _tr is None:
            await q.message.reply_text("❌ wallet tracker offline")
            raise ApplicationHandlerStop
        payload = _tr.pop_cta(str(q.data).split(":")[-1]) if q and q.data else None
        if not payload:
            await q.message.reply_text("🐾 this trade link expired — re-tag @neko to buy.")
            raise ApplicationHandlerStop
        amount = float(payload.get("amount") or 1.0)
        ca = payload.get("ca") or ""
        bot_id = payload.get("bot_id")
        user = update.effective_user
        uid = user.id if user else None
        name = (user.username or user.first_name or "trader") if user else "trader"
        if not uid or uid != payload.get("tg_uid"):
            await q.message.reply_text(
                "🐾 that button belongs to its tagger — tag me yourself: "
                f"<code>@{_neko_names[0]} buy 1 sui {ca}</code>", parse_mode="HTML")
            raise ApplicationHandlerStop
        if not (ca and bot_id):
            await q.message.reply_text(
                "🐾 In-chat <b>buy</b> needs a trading bot of your own — press "
                "<b>Start</b> on @%s first, then tap buy again." % _neko_names[0],
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🐾 Create my bot", callback_data="nav:add")]]))
            raise ApplicationHandlerStop
        idem = f"tbuy:{bot_id}:{uid}:{q.message.message_id}"
        res = await userbot.alert_buy(bot_id, amount, ca, idem)
        if not res.get("ok"):
            await q.message.reply_text("❌ " + str(res.get("error", "rejected")))
            raise ApplicationHandlerStop
        from chatbuy import receipt_text
        txt = receipt_text("public", username=name, amount=amount,
                           digest=str(res.get("digest", "")),
                           mention=_tr.neko_username)
        await q.message.reply_text(txt, parse_mode="HTML")
        raise ApplicationHandlerStop

    app.add_handler(CallbackQueryHandler(_on_tbuy, pattern=r"^sb:tbuy:"))

    # Master router: single-bot serving. Every update the master's own handlers
    # did NOT claim (dashboard callbacks, onboarding, text on a bot screen) is
    # forwarded into the OWNING bot's Application, whose send identity is the
    # master token - so the whole per-bot dashboard runs inside @Neko_tradesbot
    # with zero handler rewrites. Registered last in group 0 so specific master
    # commands (/start, nav:, admin:) win first.
    async def _router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        import logging
        log = logging.getLogger("tg_bot")
        # In-chat @neko "create wallet": provision a Sui wallet + register the
        # tagger so one /start later drops them straight onto the dashboard in
        # degen mode. Runs BEFORE the buy branch so it also works for brand-new
        # users (chatbuy's no-bot fallback only knows about buys).
        try:
            from chatwallet import (parse_chat_wallet, created_reply, exists_reply,
                                    failed_reply, provision_wallet,
                                    enable_degen, master_username,
                                    chat_usernames_from_env)
            msg = update.effective_message
            if msg and msg.text:
                w = parse_chat_wallet(msg.text, chat_usernames_from_env())
                if w is not None:
                    user = update.effective_user
                    uid = user.id if user else None
                    if uid is not None:
                        name = (user.username or user.first_name or "trader")
                        log.info("router: create-wallet mention uid=%s name=%r", uid, w)
                        master = master_username()
                        bots = registry.bots_for(uid)
                        bid = max(b["id"] for b in bots) if bots else None
                        if bid is not None and any(b["id"] == bid and b.get("wallet_addr")
                                                   for b in bots):
                            await msg.reply_text(exists_reply(name, master),
                                                 parse_mode="HTML")
                            raise ApplicationHandlerStop
                        if bid is None:
                            # token-less master-only bot row; degen does not read
                            # platform_token/agent credentials, so the platform
                            # register_agent round-trip is skipped here.
                            import re as _re
                            agent = _re.sub(r"[^0-9A-Za-z_-]", "-",
                                            f"tag-{name}-{uid % 10000}")[:32]
                            try:
                                bid = registry.create_bot(
                                    uid, name, None, None, agent, "",
                                    {"perps": 0, "spot": 1, "us-stock": 0, "forex": 0},
                                    1.0, 120, "balanced")["id"]
                            except ValueError as exc:
                                log.warning("router: create-wallet bot row failed: %s", exc)
                                await msg.reply_text(failed_reply(name, master),
                                                     parse_mode="HTML")
                                raise ApplicationHandlerStop
                        address, key = provision_wallet(bid)
                        if not address:
                            log.warning("router: create-wallet provision failed bot=%s", bid)
                            await msg.reply_text(failed_reply(name, master),
                                                 parse_mode="HTML")
                            raise ApplicationHandlerStop
                        registry.update_bot(bid, wallet_addr=address,
                                            wallet_precreated=1, chain="sui",
                                            network="mainnet")
                        degen_on = enable_degen(bid)
                        try:
                            userbot.start_bot(bid)
                        except Exception:  # noqa: BLE001 - best-effort
                            pass
                        log.info("router: wallet created bot=%s uid=%s degen=%s", bid, uid, degen_on)
                        await msg.reply_text(
                            created_reply(name, master), parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("🗝️ Get my key",
                                                     url=f"https://t.me/{master}")]]))
                        raise ApplicationHandlerStop
        except ApplicationHandlerStop:
            raise
        except Exception:  # noqa: BLE001 - the router must never dead-drop updates
            pass
        # In-chat @neko wallet tracking: subscribe/untrack/list. A tracked wallet
        # is just an on-chain Sui address — no bot row needed. Max 8 per tg uid.
        try:
            from chattrack import (parse_chat_track, tracked_reply, limit_reply,
                                   untracked_reply, not_tracked_reply,
                                   list_reply, MAX_TRACKS)
            msg = update.effective_message
            if msg and msg.text:
                p = parse_chat_track(msg.text, _track_names())
                if p is not None:
                    user = update.effective_user
                    uid = user.id if user else None
                    name = (user.username or user.first_name or "trader")
                    if p.get("error"):
                        await msg.reply_text("❌ " + p["error"], parse_mode="HTML")
                        raise ApplicationHandlerStop
                    if uid is None:
                        log.info("router: track dropped anonymous chat=%s", msg.chat_id)
                        raise ApplicationHandlerStop
                    try:
                        from degen.runtime import get_ledger
                        _led = get_ledger()
                    except Exception as exc:
                        log.warning("router: track ledger offline: %s", exc)
                        _led = None
                    if _led is None:
                        await msg.reply_text("❌ tracking engine offline — degen is dark.")
                        raise ApplicationHandlerStop
                    op, wallet = p["op"], p["wallet"]
                    if op == "track":
                        log.info("router: track uid=%s wallet=%s", uid, wallet)
                        if _led.chat_track_count(uid) >= MAX_TRACKS:
                            await msg.reply_text(limit_reply(name), parse_mode="HTML")
                            raise ApplicationHandlerStop
                        bot_id = None
                        try:
                            _bots = registry.bots_for(uid)
                            _act = context.chat_data.get("active_bot_id")
                            ids = {b["id"] for b in _bots}
                            bot_id = _act if _act in ids else (max(ids) if ids else None)
                        except Exception:
                            bot_id = None
                        _led.chat_track_add(uid, msg.chat_id, wallet, bot_id=bot_id,
                                            username=name)
                        await msg.reply_text(tracked_reply(
                            name, _led.chat_track_count(uid), mention=p["mention"]),
                            parse_mode="HTML")
                        raise ApplicationHandlerStop
                    if op == "untrack":
                        removed = _led.chat_track_remove(uid, wallet)
                        await msg.reply_text(
                            untracked_reply(name, wallet) if removed
                            else not_tracked_reply(name, wallet), parse_mode="HTML")
                        raise ApplicationHandlerStop
                    if op == "list":
                        await msg.reply_text(list_reply(_led.chat_track_rows(uid), name),
                                             parse_mode="HTML")
                        raise ApplicationHandlerStop
        except ApplicationHandlerStop:
            raise
        except Exception:  # noqa: BLE001
            pass
        # In-chat @neko buy for someone with no bot yet: point them at onboarding
        # instead of silently dropping their tag.
        try:
            from chatbuy import parse_chat_buy
            msg = update.effective_message
            if msg and msg.text:
                log.info("router: text chat=%s uid=%s. %r", msg.chat_id,
                         update.effective_user.id if update.effective_user else None,
                         msg.text[:100])
                p = parse_chat_buy(msg.text, chat_usernames_from_env())
                if p is not None:
                    uid = update.effective_user.id if update.effective_user else None
                    log.info("router: chat-buy mention detected uid=%s parsed=%s", uid, p)
                    if uid is not None and not registry.bots_for(uid):
                        await msg.reply_text(
                            "🐾 In-chat <b>buy</b> needs a trading bot of your own — "
                            "your tags then trade from YOUR wallet (funded or not), "
                            "and the position lands on your dashboard.",
                            reply_markup=telegram.InlineKeyboardMarkup([[
                                telegram.InlineKeyboardButton(
                                    "🐾 Create my bot", callback_data="nav:add")]]))
                        raise ApplicationHandlerStop
        except Exception:  # noqa: BLE001
            pass
        # Every update the master's own handlers did NOT claim is forwarded to
        # the caller's own dashboard Application. route() is owner-scoped, so a
        # stranger's update can never reach another trader's bot.
        if await userbot.route(update, context.chat_data.get("active_bot_id")):
            raise ApplicationHandlerStop

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
    from notifier import Notifier, SendBudget
    from watcher import Watcher
    from tg_config import BASE_DIR
    import threading as _threading

    # ONE shared send budget for the whole fleet: every watcher thread pushes
    # through the same master token, so a single global+per-chat limiter keeps
    # us inside Telegram's send limits no matter how many users we onboard.
    budget = SendBudget(
        global_rps=float(os.getenv("TG_SEND_GLOBAL_RPS", "25")),
        per_chat_spacing=float(os.getenv("TG_SEND_CHAT_SPACING_S", "1.0")),
    )
    notifier = Notifier(registry, budget=budget)
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
    # In-chat wallet tracking: one shared poller, alerts posted via the same
    # budgeted master notifier. No-op when degen is disabled (env-gated).
    try:
        from tracker import start_wallet_tracker
        start_wallet_tracker(notifier=notifier, budget=budget)
    except Exception as exc:
        log.warning("[tracker] start failed: %s", exc)
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