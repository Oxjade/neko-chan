"""Simple Add-My-Bot flow for Neko (master bot).

User journey:
  1. /start  -> welcome message (id auto-captured; first user becomes owner)
  2. paste their @BotFather bot token  -> validated + ownership check
  3. type the name they want to trade with -> agent registered on the platform,
     bot added to the network.

The AI key / risk profile / markets are configured later inside the user's own
bot (Settings) - signup stays minimal.
"""

import re

import telegram
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from tg_config import REGISTRY_PATH
from messages import WIZARD, MENU, mask_key
from handlers.common import (
    HOME, CANCEL, menu_keyboard, cancel_keyboard,
    generate_verify_code, get_bot_username, poll_for_verify_code,
)

# conversation states
S_TOKEN, S_NAME = range(2)

NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{3,24}$")


def validate_name(name: str) -> str | None:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        return "Only letters, numbers, spaces, 3–24 chars."
    return None


def simple_flow_handlers(registry, vault, platform, userbot, agent_pool):
    """Register the 3-step Add-My-Bot conversation."""

    async def start_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            WIZARD["token"],
            reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(CANCEL, callback_data="wiz:cancel")]]),
        )
        return S_TOKEN

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query:
            await query.answer()
            await query.message.reply_text(WIZARD["cancel"],
                                           reply_markup=telegram.ReplyKeyboardMarkup(menu_keyboard(), resize_keyboard=True))
        else:
            await update.message.reply_text(WIZARD["cancel"])
        return ConversationHandler.END

    async def on_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
        token = (update.message.text or "").strip()
        if len(token) < 20 or ":" not in token:
            await update.message.reply_text(WIZARD["token_bad"],
                                            reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("↻ Retry", callback_data="wiz:retry")]]))
            return S_TOKEN
        username = get_bot_username(token)
        if not username:
            await update.message.reply_text(WIZARD["token_bad"],
                                            reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("↻ Retry", callback_data="wiz:retry")]]))
            return S_TOKEN
        # duplicate check before ownership proof
        for b in registry.all_bots():
            if registry.bot_token(b["id"]) == token:
                await update.message.reply_text("This bot is already registered.")
                return S_TOKEN
        context.bot_data["pending"] = {"token": token, "username": username,
                                       "verify_code": generate_verify_code()}
        await _send_verify_screen(update.message, context.bot_data["pending"])
        return S_TOKEN

    async def _send_verify_screen(msg, pending: dict):
        """Shows the code + buttons. New code on every request (no reuse)."""
        pending["verify_code"] = generate_verify_code()
        code_digits = "".join(ch for ch in pending["verify_code"] if ch.isdigit())
        await msg.reply_text(
            WIZARD["verify"].format(username=pending["username"], code_digits=code_digits),
            reply_markup=telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("✅ I sent it", callback_data="wiz:verify_ok")],
                [telegram.InlineKeyboardButton("🔄 Send code again", callback_data="wiz:resend_code")],
                [telegram.InlineKeyboardButton(CANCEL, callback_data="wiz:cancel")],
            ]),
        )

    async def on_resend(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        pending = context.bot_data.get("pending")
        await query.answer()
        if not pending:
            await query.message.reply_text("Session expired - use /start to begin again.")
            return ConversationHandler.END
        await _send_verify_screen(query.message, pending)
        return S_TOKEN

    async def on_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        pending = context.bot_data.get("pending")
        await query.answer()
        if not pending:
            await query.message.reply_text("Session expired - use /start to begin again.")
            return ConversationHandler.END
        await query.message.edit_text("⏳ Watching your bot for the code (up to 60s)…")
        result = poll_for_verify_code(pending["token"], pending["verify_code"], timeout_s=60)
        import logging

        logging.getLogger("tg_bot").info(
            "verify result=%s user=%s bot=%s", result, query.from_user.id, pending["username"])
        if result == "no_updates":
            code_digits = "".join(ch for ch in pending["verify_code"] if ch.isdigit())
            await query.message.edit_text(
                WIZARD["verify_no_chat"].format(username=pending["username"],
                                                code=code_digits),
                reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("✅ I sent it", callback_data="wiz:verify_ok")],
                                                            [telegram.InlineKeyboardButton("🔄 Send code again", callback_data="wiz:resend_code")],
                                                            [telegram.InlineKeyboardButton(CANCEL, callback_data="wiz:cancel")]]),
            )
            return S_TOKEN
        if result != "verified":
            await query.message.edit_text(WIZARD["verify_bad"],
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("🔄 Send code again", callback_data="wiz:resend_code")],
                                                                                      [telegram.InlineKeyboardButton("↻ Retry check", callback_data="wiz:verify_ok")]]))
            return S_TOKEN
        await query.message.edit_text(WIZARD["verify_ok"])
        await query.message.reply_text(
            "Now send the name you want to trade with (3–24 chars, e.g. BitcoinWhale):",
            reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(CANCEL, callback_data="wiz:cancel")]]),
        )
        return S_NAME

    async def on_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
        import logging

        log = logging.getLogger("tg_bot")
        name = (update.message.text or "").strip()
        err = validate_name(name)
        if err:
            await update.message.reply_text(f"❌ {err}")
            return S_NAME
        pending = context.bot_data.get("pending")
        if not pending:
            await update.message.reply_text("Session expired - use /start to begin again.")
            return ConversationHandler.END
        tg_id = update.effective_user.id
        registry.upsert_user(tg_id, update.effective_user.username or update.effective_user.first_name)
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
            bot = registry.create_bot(
                tg_id, name, pending["token"], pending["username"],
                agent.get("name", agent_name), agent["token"],
                {"perps": 0, "spot": 1, "us-stock": 1, "forex": 1},
                1.0, 120, "balanced",
                agent_id=agent.get("agent_id"),
            )
        except ValueError as exc:
            log.warning("bot create failed: %s", exc)
            await update.message.reply_text(str(exc))
            return S_TOKEN
        try:
            if userbot:
                userbot.start_bot(bot["id"])
            if agent_pool:
                agent_pool.start(bot["id"])
        except Exception as exc:  # noqa: BLE001 - bot is registered; start is best-effort
            log.error("bot start failed (bot still registered): %s", exc)
        await update.message.reply_text(
            WIZARD["done"].format(name=name, username=pending["username"]),
            reply_markup=telegram.ReplyKeyboardMarkup(menu_keyboard(), resize_keyboard=True),
        )
        log.info("bot registered user=%s name=%s username=%s", tg_id, name, pending["username"])
        context.bot_data.pop("pending", None)
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[CommandHandler("addbot", start_wizard)],
        states={
            S_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_token),
                CallbackQueryHandler(on_verify, pattern=r"^wiz:verify_ok$"),
                CallbackQueryHandler(on_resend, pattern=r"^wiz:resend_code$"),
            ],
            S_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_name)],
        },
        fallbacks=[CallbackQueryHandler(cancel, pattern=r"^wiz:cancel$")],
        name="add_bot_flow",
        allow_reentry=True,
    )