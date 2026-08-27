"""Master bot handlers: /start, How It Works, My Bots, Leaderboard, Help, /admin."""

import telegram
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

from tg_config import ADMIN_TG_IDS
from key_vault import KeyVault
from messages import WELCOME_NEW, WELCOME_RETURNING, HOW_IT_WORKS, MENU, ERRORS
from handlers.common import HOME, menu_keyboard, home_keyboard


def register_master_handlers(app, registry, platform, userbot_controller):
    async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        registry.upsert_user(user.id, user.username or user.first_name)
        promoted = registry.promote_first_user_to_admin(user.id)
        bots = registry.bots_for(user.id)
        if promoted:
            text = (f"👑 Welcome, {user.first_name or user.username or 'owner'}! You are now the owner of Neko.\n\n"
                    "Send /addbot to connect your own trading bot, or browse below.")
        elif bots:
            names = ", ".join(f"{b['bot_name']} {'🟢' if b['is_running'] else '⏸️'}" for b in bots)
            text = f"👋 Welcome back to Neko! You have: {names}"
        else:
            text = ("👋 Welcome to Neko — the AI trader bot network.\n\n"
                    "Run your own AI trading bot on a paper platform with real prices.\n"
                    "You bring two keys — we do the rest:\n"
                    "  1️⃣ A Telegram bot token (from @BotFather) → your channel\n"
                    "  2️⃣ An AI API key → your bot's brain\n\n"
                    "⚠️ Paper trading only. No real money. Not financial advice.")
        kb = [[telegram.InlineKeyboardButton("➕ Add My Bot", callback_data="nav:add"),
               telegram.InlineKeyboardButton("🏆 Leaderboard", callback_data="nav:lb")],
              [telegram.InlineKeyboardButton("🤖 My Bots", callback_data="nav:mybots"),
               telegram.InlineKeyboardButton("❓ Help", callback_data="nav:help")]]
        await update.message.reply_text(text, reply_markup=telegram.InlineKeyboardMarkup(kb))

    async def nav_how(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await q.message.edit_text(
            HOW_IT_WORKS[1],
            reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("1/3 → Next", callback_data="how:2")], [telegram.InlineKeyboardButton(HOME, callback_data="nav:home")]]),
        )

    async def how_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        page = int(q.data.split(":")[1])
        kb = []
        if page > 1:
            kb.append([telegram.InlineKeyboardButton("← Back", callback_data=f"how:{page-1}")])
        if page < 3:
            kb.append([telegram.InlineKeyboardButton(f"{page}/3 → Next", callback_data=f"how:{page+1}")])
        if page == 3:
            kb.append([telegram.InlineKeyboardButton("✅ I Understand", callback_data="nav:add")])
        kb.append([telegram.InlineKeyboardButton(HOME, callback_data="nav:home")])
        await q.message.edit_text(HOW_IT_WORKS[page], reply_markup=telegram.InlineKeyboardMarkup(kb))

    async def nav_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await q.message.edit_text("Main menu", reply_markup=telegram.InlineKeyboardMarkup(
            [[telegram.InlineKeyboardButton(MENU["main_new"], callback_data="nav:add"),
              telegram.InlineKeyboardButton(MENU["how"], callback_data="nav:how")],
             [telegram.InlineKeyboardButton(MENU["leaderboard"], callback_data="nav:lb"),
              telegram.InlineKeyboardButton(MENU["help"], callback_data="nav:help")]]))

    async def nav_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await q.message.reply_text(
            "➕ Add your bot:\n\n"
            "1. Create it in @BotFather → /newbot → copy the token\n"
            "2. Type /addbot here and paste it\n\n"
            "Then send the name you want to trade with. That's it.",
        )

    async def nav_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        try:
            lb = platform.leaderboard("")  # no auth needed for leaderboard
        except Exception:
            lb = {"top_agents": []}
        rows = lb.get("top_agents", [])[:10]
        lines = ["🏆 Bot Network Leaderboard (live)"]
        for i, a in enumerate(rows, 1):
            name = a.get("name", "?")
            pct = a.get("total_profit_percent", 0)
            mine = " ← you" if name in {b["agent_name"] for b in registry.bots_for(q.from_user.id)} else ""
            lines.append(f"{i}. {name}  {pct:+.2f}%{mine}")
        if not rows:
            lines.append("(no bots yet — be the first!)")
        await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(
            [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="nav:lb")],
             [telegram.InlineKeyboardButton(HOME, callback_data="nav:home")]]))

    async def nav_mybots(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        bots = registry.bots_for(q.from_user.id)
        if not bots:
            await q.message.edit_text("You have no bots yet.", reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(MENU["main_new"], callback_data="nav:add")], [telegram.InlineKeyboardButton(HOME, callback_data="nav:home")]]))
            return
        lines = ["🤖 Your bots:"]
        kb = []
        for b in bots:
            mark = "🟢" if b["is_running"] else "⏸️"
            lines.append(f"{mark} {b['bot_name']}  @{b['bot_username']}")
            kb.append([telegram.InlineKeyboardButton(f"👁 {b['bot_name']}", callback_data=f"bot:view:{b['id']}")])
        kb.append([telegram.InlineKeyboardButton(MENU["main_new"], callback_data="nav:add")])
        kb.append([telegram.InlineKeyboardButton(HOME, callback_data="nav:home")])
        await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(kb))

    async def bot_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        _, _, bot_id = q.data.split(":")
        bot = registry.get_bot(int(bot_id))
        if not bot or bot["tg_id"] != q.from_user.id:
            await q.message.edit_text("Bot not found.")
            return
        line = (f"{bot['bot_name']} — all controls live in @{bot['bot_username']}\n"
                f"Heartbeat: {bot['last_heartbeat'] or 'never'} · interval {bot['interval_sec']}s · "
                f"profile {bot['risk_profile']}\n"
                f"Agent: {bot['agent_name']} · {('🟢 running' if bot['is_running'] else '⏸️ paused')}")
        await q.message.edit_text(line, reply_markup=telegram.InlineKeyboardMarkup(
            [[telegram.InlineKeyboardButton(f"🔗 Open @{bot['bot_username']}", callback_data=f"none:{bot_id}")],
             [telegram.InlineKeyboardButton("🗑️ Remove from network", callback_data=f"bot:remove:{bot_id}")],
             [telegram.InlineKeyboardButton(BACK, callback_data="nav:mybots"), telegram.InlineKeyboardButton(HOME, callback_data="nav:home")]]))

    async def bot_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        _, _, bot_id = q.data.split(":")
        bot = registry.get_bot(int(bot_id))
        if not bot or bot["tg_id"] != q.from_user.id:
            return
        await q.message.edit_text(f"Remove {bot['bot_name']} from the network? This stops its runner and wipes its keys.",
                                  reply_markup=telegram.InlineKeyboardMarkup(
                                      [[telegram.InlineKeyboardButton("✅ Yes, remove", callback_data=f"bot:remove_yes:{bot_id}")],
                                       [telegram.InlineKeyboardButton("↩️ Keep it", callback_data="nav:mybots")]]))

    async def bot_remove_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        _, _, bot_id = q.data.split(":")
        bot = registry.get_bot(int(bot_id))
        if not bot or bot["tg_id"] != q.from_user.id:
            return
        userbot_controller.stop_bot(bot_id)
        registry.delete_bot(int(bot_id), q.from_user.id)
        registry.revoke_keys(q.from_user.id)
        await q.message.edit_text("🗑️ Removed. Your agent history stays on the platform.",
                                  reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(HOME, callback_data="nav:home")]]))

    async def nav_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await q.message.edit_text(
            "❓ Help\n\n"
            "• Get a bot token: open @BotFather → /newbot → copy the token\n"
            "• AI key rejected: check the key starts with the right prefix (sk-…)\n"
            "• Paper trading: real prices, simulated money — no real risk\n"
            "• Lost your bot in BotFather: re-create it, then re-verify the token here\n\n"
            "Contact: @support",
            reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(HOME, callback_data="nav:home")]]),
        )

    async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_TG_IDS and not registry.is_admin(update.effective_user.id):
            await update.message.reply_text(ERRORS["unauthorized"])
            return
        bots = registry.all_bots()
        lines = [f"👑 Fleet — {len(bots)} bots · {sum(b['is_running'] for b in bots)} running"]
        for b in bots[:50]:
            mark = "🟢" if b["is_running"] else ("🔴" if b["last_error"] else "⏸️")
            lines.append(f"{mark} {b['bot_name']}  @{b['bot_username']}  agent:{b['agent_name']}  "
                         f"err:{b['last_error'] or '-'}")
        await update.message.reply_text("\n".join(lines) if lines else "No bots.")

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("menu", on_start))
    app.add_handler(CommandHandler("admin", admin_list))
    app.add_handler(CallbackQueryHandler(nav_how, pattern=r"^nav:how$"))
    app.add_handler(CallbackQueryHandler(how_page, pattern=r"^how:\d+$"))
    app.add_handler(CallbackQueryHandler(nav_home, pattern=r"^nav:home$"))
    app.add_handler(CallbackQueryHandler(nav_add, pattern=r"^nav:add$"))
    app.add_handler(CallbackQueryHandler(nav_leaderboard, pattern=r"^nav:lb$"))
    app.add_handler(CallbackQueryHandler(nav_mybots, pattern=r"^nav:mybots$"))
    app.add_handler(CallbackQueryHandler(bot_view, pattern=r"^bot:view:\d+$"))
    app.add_handler(CallbackQueryHandler(bot_remove, pattern=r"^bot:remove:\d+$"))
    app.add_handler(CallbackQueryHandler(bot_remove_yes, pattern=r"^bot:remove_yes:\d+$"))
    app.add_handler(CallbackQueryHandler(nav_help, pattern=r"^nav:help$"))