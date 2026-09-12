"""Single-bot onboarding: create your Neko trading bot with just a NAME.

The @BotFather token + VERIFY-code challenge are gone. The master bot
(@Neko_tradesbot) IS the bot the user talks to; adding a "trading bot" now
means creating an agent + a token-less registry row that the master router
serves (see UserBotController.route). Signup is: tap Add -> type a name -> done.

The AI key / risk profile / markets are configured later on the bot's dashboard,
so signup stays minimal.
"""

import re

import telegram
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from messages import WIZARD, MENU, mask_key
from handlers.common import (
    HOME, CANCEL, menu_keyboard, cancel_keyboard,
)

# S_TOKEN kept as a stable id (== 1) only so state numbering is unchanged; the
# name step is the single step now.
S_TOKEN, S_NAME = range(2)

NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{3,24}$")


def validate_name(name: str) -> str | None:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        return "Only letters, numbers, spaces, 3–24 chars."
    return None


def simple_flow_handlers(registry, vault, platform, userbot, agent_pool):
    """Register the token-less 'Add my bot' conversation (name only)."""

    async def start_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🐾 What should your trading cat be called?\n\n"
            "Pick a name (3–24 chars, letters/numbers/space), e.g. BitcoinWhale.\n"
            "No @BotFather needed anymore — it's just you and me now.",
            reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(CANCEL, callback_data="wiz:cancel")]]),
        )
        return S_NAME

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query:
            await query.answer()
            await query.message.reply_text(WIZARD["cancel"],
                                           reply_markup=telegram.ReplyKeyboardMarkup(menu_keyboard(), resize_keyboard=True))
        else:
            await update.message.reply_text(WIZARD["cancel"])
        return ConversationHandler.END

    async def on_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
        import logging

        log = logging.getLogger("tg_bot")
        name = (update.message.text or "").strip()
        err = validate_name(name)
        if err:
            await update.message.reply_text(f"❌ {err}",
                                            reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(CANCEL, callback_data="wiz:cancel")]]))
            return S_NAME
        tg_id = update.effective_user.id
        registry.upsert_user(tg_id, update.effective_user.username or update.effective_user.first_name)

        # a per-user unique agent handle so the platform leaderboard can tell
        # two people's "Whale" apart (mirrors the legacy naming rule).
        agent_name = name
        try:
            agent = platform.register_agent(agent_name)
        except Exception as exc:
            if "already exists" in str(exc):
                agent_name = f"{name}_{tg_id % 10000}"
                try:
                    agent = platform.register_agent(agent_name)
                except Exception as exc2:
                    log.error("agent register fallback failed: %s", exc2)
                    await update.message.reply_text(f"⚠️ Platform error: {exc2}")
                    return S_NAME
            else:
                log.error("agent register failed: %s", exc)
                await update.message.reply_text(f"⚠️ Platform error: {exc}")
                return S_NAME
        try:
            # token-less bot: no @BotFather token, master serves it via router.
            bot = registry.create_bot(
                tg_id, name, None, None,
                agent.get("name", agent_name), agent["token"],
                {"perps": 0, "spot": 1, "us-stock": 1, "forex": 1},
                1.0, 120, "balanced",
                agent_id=agent.get("agent_id"),
            )
        except ValueError as exc:
            log.warning("bot create failed: %s", exc)
            await update.message.reply_text(str(exc))
            return S_NAME
        try:
            if userbot:
                userbot.start_bot(bot["id"])  # master-only: routed, not polled
            if agent_pool:
                agent_pool.start(bot["id"])
        except Exception as exc:  # noqa: BLE001 - bot is registered; start is best-effort
            log.error("bot start failed (bot still registered): %s", exc)
        await update.message.reply_text(
            f"✅ {name} is live! Tap it to open its dashboard.",
            reply_markup=telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton(f"🐾 Open {name}", callback_data=f"switch:{bot['id']}")],
                [telegram.InlineKeyboardButton("🤖 My Bots", callback_data="nav:mybots")],
            ]),
        )
        log.info("token-less bot registered user=%s name=%s bot_id=%s", tg_id, name, bot["id"])
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[CommandHandler("addbot", start_wizard),
                      CallbackQueryHandler(start_wizard, pattern=r"^nav:add$")],
        states={
            S_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_name)],
        },
        fallbacks=[CallbackQueryHandler(cancel, pattern=r"^wiz:cancel$")],
        name="add_bot_flow",
        allow_reentry=True,
    )
