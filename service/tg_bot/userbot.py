"""User bot serving: one python-telegram-bot Application per registered bot.

First-run onboarding (welcome -> AI API key -> dashboard), then a detailed,
polished dashboard: P&L, positions, live markets with sentiment, trades feed,
settings, inbox. All data comes from the AI-Trader platform in real time.
"""

import json
import threading
from datetime import datetime, timezone

import telegram
from telegram import Update
from telegram.ext import (Application, ContextTypes, CommandHandler,
                          CallbackQueryHandler, ConversationHandler,
                          MessageHandler, filters)

from messages import USERBOT, NOTIF, mask_key
from store import utcnow
from provider import validate_key, ProviderError

BACK = "↩️ Back"
HOME = "🏠 Home"
CANCEL = "❌ Cancel"

# key onboarding states
K_PROVIDER, K_KEY = range(2)


def _ago(iso: str | None) -> str:
    if not iso:
        return "n/a"
    try:
        dt = datetime.fromisoformat(iso)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        return f"{secs // 60}m ago"
    except Exception:
        return "n/a"


def _sparkline(equity: list[float], width: int = 12) -> str:
    """ASCII equity sparkline like ▁▃▅▆▇█."""
    if not equity:
        return "(no history yet)"
    lo, hi = min(equity), max(equity)
    if hi - lo < 1e-9:
        return "▁" * min(width, len(equity))
    bars = "▁▂▃▄▅▆▇█"
    step = max(1, len(equity) // width)
    pts = equity[::step][:width]
    return "".join(bars[min(len(bars) - 1, int((p - lo) / (hi - lo) * (len(bars) - 1)))] for p in pts)


def render_dashboard_text(bot: dict, portfolio: dict, lb_row: dict | None = None) -> str:
    """Pure: bot + portfolio + leaderboard row -> full-screen telemetry panel (HTML)."""
    cash = portfolio.get("cash", 0)
    open_pos = portfolio.get("positions", [])
    pnl = 0.0
    for p in open_pos:
        cur = p.get("current_price") or p["entry_price"]
        q = p["quantity"]
        pnl += (cur - p["entry_price"]) * q if q >= 0 else (p["entry_price"] - cur) * abs(q)
    profit = cash + pnl - 100000
    ret = lb_row.get("total_profit_percent", profit / 1000) if lb_row else profit / 1000
    dd = lb_row.get("max_drawdown", 0) if lb_row else 0.0
    tcount = lb_row.get("trade_count", 0) if lb_row else 0
    rank = lb_row.get("rank", "–") if lb_row else "–"

    # today / 7d from profit history points (leaderboard history: {profit, recorded_at})
    hist = (lb_row or {}).get("history") or []
    def _profit_of(h):
        return float(h.get("profit", 0)) if isinstance(h, dict) else 0.0
    today = _profit_of(hist[-1]) - _profit_of(hist[-2]) if len(hist) >= 2 else None
    week = _profit_of(hist[-1]) - _profit_of(hist[0]) if len(hist) >= 2 else None

    line = "─" * 30
    def money(v, sign=True):
        return f"${v:+,.2f}" if sign else f"${v:,.2f}"

    pos_lines = []
    if not open_pos:
        pos_lines.append("  (no open positions — waiting for a setup)")
    for p in open_pos[:3]:
        cur = p.get("current_price") or p["entry_price"]
        q = p["quantity"]
        pp = (cur - p["entry_price"]) * q if q >= 0 else (p["entry_price"] - cur) * abs(q)
        lev = p.get("leverage") or 1.0
        icon = "⚡" if lev and lev > 1 else "📈"
        pos_lines.append(
            f"  {icon} <b>{p['symbol']}</b> {p['side'].upper()} {q}  "
            f"<code>{p['entry_price']:.2f} → {cur:.2f}</code>  {money(pp)}"
        )
        if lev and lev > 1:
            pos_lines.append(f"     {lev:g}x · stop {p.get('stop_loss')} · target {p.get('take_profit')}")
        else:
            pos_lines.append(f"     stop {p.get('stop_loss')} · target {p.get('take_profit')}")

    status = "🟢 RUNNING" if bot.get("is_running") else "⏸️ PAUSED"
    ago = _ago(bot.get("last_heartbeat"))
    trade_total = lb_row.get("trade_count", tcount) if lb_row else tcount
    fees_paid = lb_row.get("fees_paid", 0.0) if lb_row else 0.0
    win_rate = lb_row.get("win_rate", 0) if lb_row else 0

    return (
        f"<b>🤖 {bot['bot_name']} — LIVE TELEMETRY</b>\n"
        f"<code>{line}</code>\n"
        f"{status} · heartbeat {ago} · rank #{rank}\n\n"
        f"<b>💰 EQUITY &amp; P&amp;L</b>\n"
        f"  Equity        <code>{money(profit + 100000, sign=False)}</code>\n"
        f"  Total P&amp;L    <code>{money(profit)}</code>  ({ret:+.2f}%)\n"
        f"  Today         <code>{money(today) if today is not None else '—'}</code>\n"
        f"  7d            <code>{money(week) if week is not None else '—'}</code>\n"
        f"  Max drawdown  <code>{dd * 100:.2f}%</code>\n"
        f"  Cash free     <code>{money(cash, sign=False)}</code>\n\n"
        f"<b>📈 PERFORMANCE</b>\n"
        f"  Closed trades <code>{trade_total}</code>\n"
        f"  Win rate      <code>{win_rate}%</code>\n"
        f"  Fees paid     <code>{money(fees_paid)}</code>\n\n"
        f"<b>📡 POSITIONS ({len(open_pos)})</b>\n" + "\n".join(pos_lines) + "\n\n"
        f"<b>🔮 NEXT DECISION</b> in ~{max(0, int(bot.get('interval_sec', 120)))}s "
        f"· interval {bot.get('interval_sec', 120)}s\n"
        f"<code>{line}</code>"
    )


def render_positions_text(portfolio: dict) -> str:
    pos = portfolio.get("positions", [])
    lines = [USERBOT["positions_header"].format(n=len(pos))]
    if not pos:
        lines.append("No open positions. Your bot will trade when it sees a setup.")
    for p in pos[:5]:
        cur = p.get("current_price") or p["entry_price"]
        qty = p["quantity"]
        pnl = (cur - p["entry_price"]) * qty if qty >= 0 else (p["entry_price"] - cur) * abs(qty)
        lev = p.get("leverage") or 1.0
        lines.append(f"{'⚡' if lev and lev > 1 else '📈'} {p['symbol']}  {p['side'].upper()}  {qty}  "
                     f"entry {p['entry_price']:.2f}  now {cur:.2f}  {pnl:+,.2f}")
        if lev and lev > 1:
            lines.append(f"    {lev:g}x · stop {p.get('stop_loss')} · take {p.get('take_profit')}")
        else:
            lines.append(f"    stop {p.get('stop_loss')} · take {p.get('take_profit')}")
    if len(pos) > 5:
        lines.append(f"…and {len(pos) - 5} more")
    return "\n".join(lines)


class UserBotController:
    """Builds and tracks one Application per user bot."""

    def __init__(self, registry, platform, vault=None, agent_pool=None):
        self.registry = registry
        self.platform = platform
        self.vault = vault
        self.agent_pool = agent_pool
        self._apps: dict[int, Application] = {}
        self._lock = threading.Lock()

    # ---------------- lifecycle ----------------

    def start_bot(self, bot_id: int) -> bool:
        with self._lock:
            if bot_id in self._apps:
                return True
            bot = self.registry.get_bot(bot_id)
            if not bot:
                return False
            token = self.registry.bot_token(bot_id)
            if not token:
                return False
            app = Application.builder().token(token).build()
            self._register_handlers(app, bot)
            self._apps[bot_id] = app
            self.registry.update_bot(bot_id, is_running=1, last_heartbeat=utcnow())

        def _poll():
            import logging

            log = logging.getLogger("tg_bot")
            attempts = 0
            while attempts < 10:
                try:
                    app.run_polling(drop_pending_updates=True, stop_signals=())
                    return  # clean stop
                except Exception as exc:  # noqa: BLE001
                    attempts += 1
                    log.error("user bot %s polling failed (%s/10): %s", bot_id, attempts, exc)
                    if attempts >= 10:
                        break
                    import time as _t

                    _t.sleep(10 * attempts)  # backoff: 10s, 20s, ...
            self.registry.update_bot(bot_id, is_running=0, last_error="polling failed after 10 attempts")
            with self._lock:
                self._apps.pop(bot_id, None)

        threading.Thread(target=_poll, name=f"userbot-{bot_id}", daemon=True).start()
        return True

    def stop_bot(self, bot_id: int):
        with self._lock:
            app = self._apps.pop(bot_id, None)
        if app:
            app.stop()
            self.registry.update_bot(bot_id, is_running=0)

    def start_all(self):
        for bot in self.registry.all_bots():
            if not bot.get("paused"):
                self.start_bot(bot["id"])

    # ---------------- handlers ----------------

    def _register_handlers(self, app: Application, bot: dict):
        bot_id = bot["id"]
        platform_token = bot["platform_token"]
        tg_id = bot["tg_id"]

        async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """First-ever /start: welcome + key onboarding gate."""
            key = self.registry.get_active_key(tg_id)
            if key:
                await dash(update, context)
                return
            text = (
                f"👋 Welcome to {bot['bot_name']} — your AI trading bot.\n\n"
                "I watch live markets (BTC, ETH, US stocks, Forex) and trade on the "
                "AI-Trader paper platform with real prices. Every trade gets pushed here.\n\n"
                "To start trading I need one thing from you: your AI API key — it "
                "powers my decisions and you pay for your own model calls.\n\n"
                "⚠️ Paper trading only. No real money."
            )
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("🔑 Set AI Key", callback_data="key:start")],
                [telegram.InlineKeyboardButton("❓ How to get a key", callback_data="key:help")],
            ])
            if update.message:
                await update.message.reply_text(text, reply_markup=kb)
            else:
                await update.callback_query.message.edit_text(text, reply_markup=kb)

        async def key_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            await q.message.edit_text(
                "🔑 Where to get an AI API key:\n\n"
                "• OpenAI: platform.openai.com → API keys → sk-…\n"
                "• OpenRouter: openrouter.ai → keys → sk-or-…\n"
                "• Custom: any OpenAI-compatible endpoint (URL + key)\n\n"
                "The key only pays for YOUR bot's decisions. Tap Set AI Key when ready.",
                reply_markup=telegram.InlineKeyboardMarkup([
                    [telegram.InlineKeyboardButton("🔑 Set AI Key", callback_data="key:start")],
                ]),
            )

        async def key_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("OpenAI", callback_data="keyp:openai"),
                 telegram.InlineKeyboardButton("OpenRouter", callback_data="keyp:openrouter")],
                [telegram.InlineKeyboardButton("opencode-go", callback_data="keyp:opencode-go"),
                 telegram.InlineKeyboardButton("Custom URL", callback_data="keyp:custom")],
                [telegram.InlineKeyboardButton(CANCEL, callback_data="key:cancel")],
            ])
            await q.message.edit_text(
                "🔑 Which AI provider powers your bot?\n\n"
                "Your key pays for your own model calls. We test it before saving.",
                reply_markup=kb,
            )
            return K_PROVIDER

        async def key_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            provider = q.data.split(":", 1)[1]
            context.bot_data["key_provider"] = provider
            if provider == "custom":
                await q.message.edit_text(
                    "Send your provider URL (OpenAI-compatible), e.g.\n"
                    "https://your-provider.com/v1\n\nThen the model, then the key.",
                    reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(CANCEL, callback_data="key:cancel")]]),
                )
            else:
                labels = {"openai": "sk-…", "openrouter": "sk-or-…", "opencode-go": "gateway key"}
                await q.message.edit_text(
                    f"Paste your key ({labels.get(provider, 'API key')}):\n\n"
                    "It will be stored encrypted and shown masked (sk-•••4821).",
                    reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(CANCEL, callback_data="key:cancel")]]),
                )
            return K_KEY

        async def key_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
            provider = context.bot_data.get("key_provider", "openai")
            raw = (update.message.text or "").strip()
            if provider == "custom" and "key_provider_url" not in context.bot_data:
                context.bot_data["key_provider_url"] = raw
                await update.message.reply_text("Got it. Now the model (e.g. gpt-4o-mini):")
                return K_KEY
            if provider == "custom" and "key_provider_model" not in context.bot_data:
                context.bot_data["key_provider_model"] = raw
                await update.message.reply_text("Now paste your API key:")
                return K_KEY
            api_key = raw
            if len(api_key) < 8:
                await update.message.reply_text("❌ That doesn't look like a key. Paste the full key.")
                return K_KEY
            base = context.bot_data.get("key_provider_url")
            model = context.bot_data.get("key_provider_model")
            await update.message.reply_text("⏳ Testing your key…")
            try:
                model = validate_key(provider, api_key, base, model)
            except ProviderError as exc:
                msg = {"invalid": "❌ Provider rejected this key. Double-check it.",
                       "rate_limited": "⏳ Provider is rate-limited — wait a minute and retry.",
                       "network": f"⏳ Can't reach provider ({exc})."}.get(exc.kind, "❌ Key rejected.")
                await update.message.reply_text(msg)
                return K_KEY
            self.registry.store_key(tg_id, provider, api_key, base, model)
            context.bot_data.pop("key_provider", None)
            context.bot_data.pop("key_provider_url", None)
            context.bot_data.pop("key_provider_model", None)
            if self.agent_pool:
                try:
                    self.agent_pool.start(bot_id)
                except Exception:
                    pass
            await update.message.reply_text(
                f"✅ Key works ({provider}). Your bot can now trade — decisions start right away.\n\n"
                "Every trade, stop and summary will be pushed here 🔔",
            )
            await dash(update, context)
            return ConversationHandler.END

        async def key_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            await q.message.edit_text("Key setup canceled. Your bot stays paused until you add one.",
                                      reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("🔑 Set AI Key", callback_data="key:start")]]))
            return ConversationHandler.END

        key_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(key_start, pattern=r"^key:start$")],
            states={
                K_PROVIDER: [CallbackQueryHandler(key_provider, pattern=r"^keyp:")],
                K_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, key_input)],
            },
            fallbacks=[CallbackQueryHandler(key_cancel, pattern=r"^key:cancel$")],
            name="userbot_key_setup",
            allow_reentry=True,
        )

        async def dash(update: Update, context: ContextTypes.DEFAULT_TYPE):
            self.registry.update_bot(bot_id, last_heartbeat=utcnow())
            try:
                pf = self.platform.positions(platform_token)
            except Exception:
                await (update.message or update.callback_query.message).reply_text(
                    "⚠️ Platform offline, retrying…",
                    reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("↻ Retry", callback_data="sb:dash")]]))
                return
            lb_row = None
            if bot.get("agent_id"):
                try:
                    row = self.platform.agent_row(platform_token, bot["agent_name"])
                    if row:
                        lb_row = row
                except Exception:
                    pass
            b = self.registry.get_bot(bot_id)
            text = render_dashboard_text(b, pf, lb_row)
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("📊 P&L", callback_data="sb:pnl"),
                 telegram.InlineKeyboardButton("💰 Positions", callback_data="sb:pos")],
                [telegram.InlineKeyboardButton("🏦 Live Markets", callback_data="sb:live"),
                 telegram.InlineKeyboardButton("📡 Trades", callback_data="sb:trades")],
                [telegram.InlineKeyboardButton("🏆 Leaderboard", callback_data="sb:lb"),
                 telegram.InlineKeyboardButton("🤖 Bot", callback_data="sb:bot")],
                [telegram.InlineKeyboardButton("⚙️ Settings", callback_data="sb:settings"),
                 telegram.InlineKeyboardButton("📬 Inbox", callback_data="sb:inbox")],
                [telegram.InlineKeyboardButton("❓ Help", callback_data="sb:help")],
            ])
            if update.message:
                await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
            elif update.callback_query:
                await update.callback_query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

        async def pnl_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            try:
                pf = self.platform.positions(platform_token)
                lb_row = self.platform.agent_row(platform_token, bot["agent_name"]) if bot.get("agent_id") else None
            except Exception:
                await q.message.edit_text("⚠️ Platform offline.", reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            cash = pf.get("cash", 0)
            pos = pf.get("positions", [])
            pnl = sum((p.get("current_price") or p["entry_price"] - p["entry_price"]) * p["quantity"] if p["quantity"] >= 0
                      else (p["entry_price"] - (p.get("current_price") or p["entry_price"])) * abs(p["quantity"]) for p in pos)
            profit = cash + pnl - 100000
            ret = lb_row.get("total_profit_percent", profit / 1000) if lb_row else profit / 1000
            dd = lb_row.get("max_drawdown", 0) if lb_row else 0
            hist = (lb_row or {}).get("history") or []
            eq = []
            if hist:
                try:
                    eq = [100000 + float(h.get("profit", 0)) for h in hist]
                except Exception:
                    eq = []
            line = "─" * 30
            def money(v, sign=True):
                return f"${v:+,.2f}" if sign else f"${v:,.2f}"
            text = (
                f"<b>📊 P&amp;L DETAIL — {bot['bot_name']}</b>\n"
                f"<code>{line}</code>\n"
                f"<b>💰 ACCOUNT</b>\n"
                f"  Equity      <code>{money(profit + 100000, sign=False)}</code>\n"
                f"  Total P&amp;L  <code>{money(profit)}</code>  ({ret:+.2f}%)\n"
                f"  Max DD      <code>{dd * 100:.2f}%</code>\n"
                f"  Cash free   <code>{money(cash, sign=False)}</code>\n"
                f"  Open PnL    <code>{money(pnl)}</code>\n\n"
                f"<b>📈 EQUITY CURVE</b>\n"
                f"  <code>{_sparkline(eq or [100000])}</code>\n\n"
                f"<b>📡 POSITION COUNT</b>  {len(pos)} open · "
                f"{lb_row.get('trade_count', 0) if lb_row else 0} closed\n"
                f"<code>{line}</code>"
            )
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:pnl")],
                 [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            try:
                pf = self.platform.positions(platform_token)
            except Exception:
                await q.message.edit_text("⚠️ Platform offline.")
                return
            await q.message.edit_text(render_positions_text(pf), reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:pos")],
                 [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def live_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            lines = ["🏦 Live Markets  (real-time)\n"]
            markets = json.loads(bot["symbols"] or "{}")
            syms = []
            if markets.get("perps") or markets.get("spot"):
                syms += [("BTC", "crypto", "⚡" if markets.get("perps") else "₿"), ("ETH", "crypto", "⚡" if markets.get("perps") else "₿")]
            if markets.get("us-stock"):
                syms += [("AAPL", "us-stock", "📈"), ("NVDA", "us-stock", "📈"), ("SPY", "us-stock", "📈")]
            if markets.get("forex"):
                syms += [("EURUSD", "forex", "💱"), ("USDJPY", "forex", "💱"), ("GBPUSD", "forex", "💱")]
            for sym, market, icon in syms[:6]:
                try:
                    px = self.platform.price(platform_token, market, sym)
                    lines.append(f"{icon} {sym}  ${px:,.4f}")
                except Exception:
                    lines.append(f"{icon} {sym}  (market closed / unavailable)")
            lines.append("\n[↻ Now] refreshes live prices.")
            await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("↻ Now", callback_data="sb:live")],
                 [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            try:
                sigs = self.platform.signals(bot["agent_id"], limit=10) if bot.get("agent_id") else []
            except Exception:
                sigs = []
            lines = ["📡 Recent decisions\n"]
            if not sigs:
                lines.append("No trades yet — your bot will push every fill here.")
            for s in sigs[:10]:
                side = str(s.get("side") or s.get("action") or "?").upper()
                sym = s.get("symbol", "?")
                px = s.get("entry_price") or s.get("price") or 0
                qty = s.get("quantity") or 0
                when = str(s.get("created_at") or s.get("executed_at") or "")[11:19] or "?"
                lines.append(f"{when}  {side} {sym} {qty} @ ${px:,.4f}")
            await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:trades")],
                 [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            try:
                lb = self.platform.leaderboard(platform_token)
            except Exception:
                await q.message.edit_text("⚠️ Platform offline.")
                return
            lines = ["🏆 Leaderboard (live)\n"]
            for i, a in enumerate(lb.get("top_agents", [])[:10], 1):
                name = a.get("name", "?")
                pct = a.get("total_profit_percent", 0)
                mark = " ← you" if name == bot["agent_name"] else ""
                lines.append(f"{i}. {name}  {pct:+.2f}%{mark}")
            await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:lb")],
                 [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def bot_controls(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            if q.data == "sb:pause_yes":
                self.registry.update_bot(bot_id, is_running=0, paused=1)
                if self.agent_pool:
                    self.agent_pool.stop(bot_id)
                await q.message.edit_text(USERBOT["paused"], reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            if q.data == "sb:resume":
                self.registry.update_bot(bot_id, paused=0)
                self.start_bot(bot_id)
                if self.agent_pool:
                    self.agent_pool.start(bot_id)
                await q.message.edit_text(USERBOT["resumed"], reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            if q.data == "sb:pause":
                await q.message.edit_text(USERBOT["pause_confirm"].format(name=b["bot_name"]),
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("✅ Pause", callback_data="sb:pause_yes")], [telegram.InlineKeyboardButton("↩️ Cancel", callback_data="sb:dash")]]))
                return
            if q.data == "sb:delete_yes":
                self.stop_bot(bot_id)
                if self.agent_pool:
                    self.agent_pool.stop(bot_id)
                self.registry.delete_bot(bot_id, tg_id)
                await q.message.edit_text(USERBOT["deleted"])
                return
            if q.data == "sb:delete":
                await q.message.edit_text(USERBOT["delete_confirm"].format(name=b["bot_name"]),
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("✅ Yes, delete", callback_data="sb:delete_yes")], [telegram.InlineKeyboardButton("↩️ Keep it", callback_data="sb:dash")]]))
                return
            text = (f"🤖 {b['bot_name']} · bot status\n\n"
                    f"Status:    {'🟢 RUNNING' if b['is_running'] else '⏸️ PAUSED'}\n"
                    f"Heartbeat: {_ago(b['last_heartbeat'])}\n"
                    f"Profile:   {b['risk_profile']}\n"
                    f"Interval:  {b['interval_sec']}s\n"
                    f"Markets:   {b['symbols']}\n"
                    f"Leverage:  {b.get('leverage') or 1.0}x\n"
                    f"Last error: {b['last_error'] or 'none'}")
            kb = [[telegram.InlineKeyboardButton("⏸️ Pause", callback_data="sb:pause") if b["is_running"] else telegram.InlineKeyboardButton("▶️ Resume", callback_data="sb:resume")],
                  [telegram.InlineKeyboardButton("🗑️ Delete Bot", callback_data="sb:delete")],
                  [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]
            await q.message.edit_text(text, reply_markup=telegram.InlineKeyboardMarkup(kb))

        async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            if q.data.startswith("sb:set_interval"):
                seconds = int(q.data.rsplit(":", 1)[1])
                self.registry.update_bot(bot_id, interval_sec=seconds)
                await q.message.edit_text(f"Saved ✓ (interval {seconds}s)",
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(BACK, callback_data="sb:settings")]]))
                return
            text = (f"⚙️ Settings — {b['bot_name']}\n\n"
                    f"Interval:  {b['interval_sec']}s\n"
                    f"Risk:      {b['risk_profile']}\n"
                    f"Leverage:  {b.get('leverage') or 1.0}x\n"
                    f"AI key:    {'set ✓' if self.registry.get_active_key(tg_id) else 'not set'}")
            kb = [
                [telegram.InlineKeyboardButton(f"⏱ Interval: {b['interval_sec']}s", callback_data="sb:set_interval:120"),
                 telegram.InlineKeyboardButton("60s", callback_data="sb:set_interval:60"),
                 telegram.InlineKeyboardButton("5m", callback_data="sb:set_interval:300")],
                [telegram.InlineKeyboardButton("🔑 Change AI Key", callback_data="key:start")],
                [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")],
            ]
            await q.message.edit_text(text, reply_markup=telegram.InlineKeyboardMarkup(kb))

        async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            events = self.registry.recent_events(tg_id, 10)
            lines = ["📬 Inbox\n"]
            if not events:
                lines.append("No notifications yet. Every fill, stop and summary will land here.")
            for e in events:
                lines.append(f"{e['sent_at'][:16]} · {e['kind']}")
            await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def help_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            text = (
                "❓ Help\n\n"
                "• Paper trading = real prices, simulated money ($100k). No real risk.\n"
                "• Your bot decides every {interval}s and always uses stop-losses.\n"
                "• Your AI key pays for your own model calls.\n"
                "• Every trade is pushed here as a notification.\n\n"
                "Owners: use the master bot (Neko) to manage your network entry."
            ).format(interval=self.registry.get_bot(bot_id)["interval_sec"])
            await q.message.edit_text(text, reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        app.add_handler(CommandHandler("start", welcome))
        app.add_handler(CallbackQueryHandler(key_help, pattern=r"^key:help$"))
        app.add_handler(key_conv)
        app.add_handler(CallbackQueryHandler(dash, pattern=r"^sb:dash$"))
        app.add_handler(CallbackQueryHandler(pnl_detail, pattern=r"^sb:pnl$"))
        app.add_handler(CallbackQueryHandler(positions, pattern=r"^sb:pos$"))
        app.add_handler(CallbackQueryHandler(live_markets, pattern=r"^sb:live$"))
        app.add_handler(CallbackQueryHandler(trades, pattern=r"^sb:trades$"))
        app.add_handler(CallbackQueryHandler(leaderboard, pattern=r"^sb:lb$"))
        app.add_handler(CallbackQueryHandler(bot_controls, pattern=r"^sb:(pause|resume|pause_yes|delete|delete_yes)$"))
        app.add_handler(CallbackQueryHandler(settings, pattern=r"^sb:(settings|set_interval:\d+)$"))
        app.add_handler(CallbackQueryHandler(inbox, pattern=r"^sb:inbox$"))
        app.add_handler(CallbackQueryHandler(help_screen, pattern=r"^sb:help$"))