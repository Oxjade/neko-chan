"""Master bot handlers: /start, How It Works, My Bots, Leaderboard, Help, /admin."""

import telegram
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

from tg_config import ADMIN_TG_IDS
from key_vault import KeyVault
from messages import TOUR, WELCOME_RETURNING, HOW_IT_WORKS, MENU, ERRORS, humanize_error
from handlers.common import HOME, menu_keyboard, home_keyboard


def tour_nav(page: int):
    """Pure: which callback the current tour page's button leads to.
    Returns (page_text, button_label, button_callback). Every page uses the same
    "Continue" affordance; the last page's Continue hands the user into the name
    step (nav:add) which creates a token-less bot and enters the ob:intro
    onboarding - so it reads as one Continue-through to the dashboard."""
    page = max(2, min(len(TOUR), page))
    if page < len(TOUR):
        return TOUR[page], "🐾 Continue", f"tour:{page+1}"
    return TOUR[page], "🐾 Continue", "nav:add"


def register_master_handlers(app, registry, platform, userbot_controller):
    async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        registry.upsert_user(user.id, user.username or user.first_name)
        promoted = registry.promote_first_user_to_admin(user.id)
        bots = registry.bots_for(user.id)
        # Single-bot model: an existing user who already has their bot is dropped
        # straight onto that bot's dashboard (routed into its Application, whose
        # send identity is the master token) — the chat looks EXACTLY like the
        # old standalone bot did. Nothing else about the UX changes.
        if bots and userbot_controller is not None:
            target = userbot_controller.route_target(user.id, context.chat_data.get("active_bot_id"))
            if target is not None:
                context.chat_data["active_bot_id"] = target
                try:
                    userbot_controller.start_bot(target)  # idempotent, routed
                except Exception:
                    pass
                if await userbot_controller.route(update, target):
                    return
        if promoted:
            text = (f"👑 Welcome, {user.first_name or user.username or 'owner'}! You're the owner of Neko. 🐾")
        else:
            # Guided tour for new users: a short read->Continue sequence that ends
            # by handing them into the per-bot onboarding (name -> chain -> wallet)
            # which itself runs all the way to the dashboard.
            kb = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("🐾 Continue", callback_data="tour:2")]])
            await update.message.reply_text(TOUR[1], parse_mode="HTML", reply_markup=kb)
            return
        kb = [[telegram.InlineKeyboardButton("➕ Add My Bot", callback_data="nav:add"),
               telegram.InlineKeyboardButton("🏆 Leaderboard", callback_data="nav:lb")],
              [telegram.InlineKeyboardButton("❓ Help", callback_data="nav:help")]]
        await update.message.reply_text(text, reply_markup=telegram.InlineKeyboardMarkup(kb))

    async def tour_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        page = int(q.data.split(":")[1])
        text, label, cb = tour_nav(page)
        await q.message.edit_text(text, parse_mode="HTML", reply_markup=telegram.InlineKeyboardMarkup(
            [[telegram.InlineKeyboardButton(label, callback_data=cb)]]))

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
            "➕ New trading bot — one tap:\n\n"
            "1. Tap the button below (or type /addbot)\n"
            "2. Send the name you want to trade with\n\n"
            "That's the whole setup.",
            reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("🚀 Create my bot", callback_data="nav:add")]]),
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

    # nav_mybots / bot_view / bot_remove removed: single-bot model has no bot
    # picker. An owner lands directly on their dashboard (see on_start -> route).
    # The per-bot Application's own Settings screen owns start/pause/delete, so
    # management is not lost — it just lives inside the dashboard now.

    async def nav_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        await q.message.edit_text(
            "❓ Help\n\n"
            "• Add a bot: tap \"Add My Bot\" → send a name.\n"
            "• AI key rejected: check the key starts with the right prefix (sk-…)\n"
            "• Trading is live: live prices, live execution — understand the risk\n"
            "• Your cat's dashboard, wallet and positions all live right here in this chat\n\n"
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
            err = humanize_error(b["last_error"]) if b["last_error"] else "-"
            lines.append(f"{mark} {b['bot_name']}  @{b['bot_username']}  agent:{b['agent_name']}  "
                         f"err:{err}")
        kb = []
        gw = getattr(userbot_controller, "gateway", None)
        if gw and getattr(gw, "ready", False):
            kb.append([telegram.InlineKeyboardButton("🛑 Kill ALL bots",
                                                     callback_data="admin:killall")])
        await update.message.reply_text("\n".join(lines) if lines else "No bots.",
                                        reply_markup=telegram.InlineKeyboardMarkup(kb) if kb else None)

    async def admin_killall(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        user = q.from_user if q else update.effective_user
        if user.id not in ADMIN_TG_IDS and not registry.is_admin(user.id):
            if q:
                await q.message.edit_text(ERRORS["unauthorized"])
            else:
                await update.message.reply_text(ERRORS["unauthorized"])
            return
        n = len(registry.all_bots())
        text = (f"🛑 NETWORK KILL-SWITCH\n\n"
                f"Flatten ALL positions and cancel ALL open orders on every chain "
                f"for all {n} bot(s), immediately.\n\n"
                f"⚠️ This cannot be undone automatically. Trading stays halted for "
                f"each bot until released.")
        kb = telegram.InlineKeyboardMarkup(
            [[telegram.InlineKeyboardButton("🛑 Yes, kill all", callback_data="admin:killall_yes")],
             [telegram.InlineKeyboardButton("↩️ Cancel", callback_data="nav:home")]])
        if q:
            await q.message.edit_text(text, reply_markup=kb)
        else:
            await update.message.reply_text(text, reply_markup=kb)

    async def admin_killall_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if q.from_user.id not in ADMIN_TG_IDS and not registry.is_admin(q.from_user.id):
            await q.message.edit_text(ERRORS["unauthorized"])
            return
        gw = getattr(userbot_controller, "gateway", None)
        if not gw or not getattr(gw, "ready", False):
            await q.message.edit_text("🛑 Kill-switch: execution is not configured.")
            return
        results = []
        for b in registry.all_bots():
            try:
                res = gw.engage_killswitch(b["id"], "operator: network kill-switch")
                results.append((b["bot_name"], res.get("fully_flattened")))
            except Exception as exc:
                results.append((b["bot_name"], f"error: {str(exc)[:60]}"))
        ok = sum(1 for _, r in results if r is True)
        lines = [f"🛑 NETWORK KILL-SWITCH ENGAGED\n",
                 f"Flattened: {ok}/{len(results)} bots\n"]
        for name, r in results[:25]:
            lines.append(f"  {'✅' if r is True else '❌'} {name}" + ("" if r is True else f" ({r})"))
        await q.message.edit_text("\n".join(lines),
                                  reply_markup=telegram.InlineKeyboardMarkup(
                                      [[telegram.InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]))

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("menu", on_start))
    app.add_handler(CallbackQueryHandler(tour_page, pattern=r"^tour:\d+$"))
    app.add_handler(CommandHandler("admin", admin_list))
    app.add_handler(CommandHandler("adminkill", admin_killall))
    app.add_handler(CallbackQueryHandler(nav_how, pattern=r"^nav:how$"))
    app.add_handler(CallbackQueryHandler(how_page, pattern=r"^how:\d+$"))
    app.add_handler(CallbackQueryHandler(nav_home, pattern=r"^nav:home$"))
    app.add_handler(CallbackQueryHandler(nav_add, pattern=r"^nav:add$"))
    app.add_handler(CallbackQueryHandler(nav_leaderboard, pattern=r"^nav:lb$"))
    app.add_handler(CallbackQueryHandler(nav_help, pattern=r"^nav:help$"))
    app.add_handler(CallbackQueryHandler(admin_killall, pattern=r"^admin:killall$"))
    app.add_handler(CallbackQueryHandler(admin_killall_yes, pattern=r"^admin:killall_yes$"))