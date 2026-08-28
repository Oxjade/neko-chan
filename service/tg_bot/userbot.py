"""User bot serving: one python-telegram-bot Application per registered bot.

First-run onboarding (welcome -> AI API key -> dashboard), then a detailed,
polished dashboard: P&L, positions, live markets with sentiment, trades feed,
settings, inbox. All data comes from the AI-Trader platform in real time.
"""

import json
import sys
import threading
from datetime import datetime, timezone

import telegram
from telegram import Update
from telegram.ext import (Application, ContextTypes, CommandHandler,
                          CallbackQueryHandler, ConversationHandler,
                          MessageHandler, filters)

from messages import USERBOT, WIZARD, NOTIF, mask_key, humanize_error
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

    def __init__(self, registry, platform, vault=None, agent_pool=None, gateway=None):
        self.registry = registry
        self.platform = platform
        self.vault = vault
        self.agent_pool = agent_pool
        self.gateway = gateway  # ExecGateway (real execution) or None
        self._apps: dict[int, Application] = {}
        self._lock = threading.Lock()

    # ---------------- real trading helpers ----------------

    def _exec_ready(self) -> bool:
        return bool(self.gateway and getattr(self.gateway, "ready", False))

    @staticmethod
    def _exec_path():
        import os as _os
        p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "execution")
        if p not in sys.path:
            sys.path.insert(0, p)
        return p

    def _exec_chain_state(self, bot_id: int) -> tuple[dict, dict]:
        """(wallets, chain_state) for the bot from the execution ledger."""
        if not self._exec_ready():
            return {}, {}
        self._exec_path()
        try:
            self.gateway.provision_all_wallets(bot_id)
        except Exception:
            pass
        wallets, chain_state = {}, {}
        for chain in self.gateway.adapters:
            try:
                wallet = self.gateway.ledger.wallet_by_bot_chain(bot_id, chain)
                if not wallet:
                    continue
                wallets[chain] = wallet
                try:
                    self.gateway.sync(bot_id, chain)
                except Exception:
                    pass
                state = self.gateway.ledger.load_chain_state(wallet["id"]) or {}
                chain_state[chain] = state
            except Exception:
                continue
        return wallets, chain_state

    def _render_wallet(self, bot_id: int, bot_name: str, paused: int) -> str:
        self._exec_path()
        from wallet_ui import render_wallet_panel

        if not self._exec_ready():
            return USERBOT["wallet_disabled"].format(name=bot_name)
        wallets, chain_state = self._exec_chain_state(bot_id)
        if not wallets:
            return USERBOT["wallet_not_connected"].format(name=bot_name)
        return render_wallet_panel(
            [{"id": bot_id, "bot_name": bot_name, "paused": paused}],
            wallets, chain_state)

    def _exec_risk_lines(self) -> list[str]:
        self._exec_path()
        from risk_guard import BotRiskProfile

        profile = BotRiskProfile()
        lines = [
            f"• Max notional per order  <code>${profile.max_notional_usd:,.0f}</code>",
            f"• Max exposure           <code>{profile.max_exposure_pct:.0f}%</code> of balance",
            f"• Max leverage           <code>{profile.max_leverage:.0f}x</code>",
            f"• Stop-loss              <code>{'required' if profile.require_stop else 'optional'}</code> "
            f"({profile.min_stop_pct:.0f}–{profile.max_stop_pct:.0f}%)",
            f"• Daily loss halt        <code>-{profile.daily_loss_halt_pct:.0f}%</code>",
            f"• Max open positions     <code>{profile.max_open_positions}</code>",
        ]
        return lines

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
                f"🐾 Welcome to {bot['bot_name']} — your AI trading cat.\n\n"
                "I watch live markets (BTC, ETH, US stocks, Forex) and trade on the "
                "platform with real prices. Every trade gets pushed here.\n\n"
                "To start trading I need one thing: your AI API key — it "
                "powers my decisions and you pay for your own model calls.\n\n"
                "⚠️ Trading involves real risk. (I'm a cat, not an advisor.)"
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
            # Gate: disclaimer must be accepted before key setup.
            user = self.registry.get_user(tg_id)
            if user and user.get("accepted_disclaimer"):
                await _show_key_provider(q)
                return K_PROVIDER
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("✅ I understand, continue", callback_data="key:disclaimer_accept")],
                [telegram.InlineKeyboardButton("✖️ I don't accept", callback_data="key:decline")],
                [telegram.InlineKeyboardButton(CANCEL, callback_data="key:cancel")],
            ])
            await q.message.edit_text(WIZARD["disclaimer"], reply_markup=kb)
            return K_PROVIDER  # re-use the provider state; we handle the accept below

        async def key_disclaimer_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            self.registry.accept_disclaimer(tg_id)
            await _show_key_provider(q)
            return K_PROVIDER

        async def _show_key_provider(q):
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("OpenAI", callback_data="keyp:openai"),
                 telegram.InlineKeyboardButton("OpenRouter", callback_data="keyp:openrouter")],
                [telegram.InlineKeyboardButton("DeepSeek", callback_data="keyp:deepseek"),
                 telegram.InlineKeyboardButton("Claude", callback_data="keyp:claude")],
                [telegram.InlineKeyboardButton("opencode-go", callback_data="keyp:opencode-go"),
                 telegram.InlineKeyboardButton("Custom URL", callback_data="keyp:custom")],
                [telegram.InlineKeyboardButton(CANCEL, callback_data="key:cancel")],
            ])
            await q.message.edit_text(
                "🔑 Which AI provider powers your bot?\n\n"
                "Your key pays for your own model calls. We test it before saving.",
                reply_markup=kb,
            )

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
                labels = {"openai": "sk-…", "openrouter": "sk-or-…",
                          "deepseek": "sk-…", "claude": "sk-ant-…",
                          "opencode-go": "gateway key"}
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
            try:
                self.registry.store_key(tg_id, provider, api_key, base, model)
            except ValueError as exc:
                await update.message.reply_text(f"❌ {exc}")
                return K_KEY
            self.registry.cancel_bot_deletion(bot_id)  # key set -> keep the bot
            context.bot_data.pop("key_provider", None)
            context.bot_data.pop("key_provider_url", None)
            context.bot_data.pop("key_provider_model", None)
            if self.agent_pool:
                try:
                    self.agent_pool.start(bot_id)
                except Exception:
                    pass
            # Foreign/custom provider rule notification: tell the user exactly
            # how their key is used and the operating rule for non-preset keys.
            rule_notice = ""
            if provider in ("custom", "deepseek", "claude"):
                rule_notice = (
                    f"\n\n🧠 <b>HOW YOUR KEY IS USED</b>\n"
                    f"Provider: <b>{provider}</b> · Model: <code>{model}</code>\n"
                    f"• Your key is sent ONLY to {base or 'your provider'} — never "
                    f"to us or any other service\n"
                    f"• It powers your bot's trading decisions, stored encrypted "
                    f"and masked\n"
                    f"• You can revoke it anytime in Settings → Change AI Key\n"
                    f"• If your provider uses a non-standard API shape, the bot "
                    f"falls back to OpenAI-compatible calls"
                )
            await update.message.reply_text(
                f"✅ Key works ({provider}). Your bot can now trade — decisions start right away.\n\n"
                "Every trade, stop and summary will be pushed here 🔔" + rule_notice,
                parse_mode="HTML",
            )
            await dash(update, context)
            return ConversationHandler.END

        async def key_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            declined = q.data == "key:decline"
            # Unconfigured bot (no AI key yet) -> schedule cleanup so we don't
            # hold idle bots. The user gets a clear deadline + how to keep it.
            try:
                active_key = self.registry.get_active_key(tg_id)
            except Exception:
                active_key = None
            deadline = ""
            if not active_key:
                from datetime import timedelta
                from store import utcnow
                try:
                    self.registry.schedule_bot_deletion(
                        bot_id, (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat())
                    deadline = (
                        "\n\n🕒 Your bot is unconfigured — it will be removed from the "
                        "network in 3 hours unless you add your AI key.\n"
                        "To keep it, just tap \"Set AI Key\" and complete setup."
                    )
                except Exception:
                    deadline = ""
            if declined:
                text = ("✅ Understood — nothing was saved.\n\n"
                        "Your bot stays OFF. No AI key was stored, no agent was "
                        "started, and nothing was charged." + deadline)
            else:
                text = ("Key setup canceled. Your bot stays paused until you add one." + deadline)
            await q.message.edit_text(
                text,
                reply_markup=telegram.InlineKeyboardMarkup(
                    [[telegram.InlineKeyboardButton("🔑 Set AI Key", callback_data="key:start")],
                     [telegram.InlineKeyboardButton("🏠 Home", callback_data="sb:dash")]]))
            return ConversationHandler.END

        key_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(key_start, pattern=r"^key:start$")],
            states={
                K_PROVIDER: [
                    CallbackQueryHandler(key_provider, pattern=r"^keyp:"),
                    CallbackQueryHandler(key_disclaimer_accept, pattern=r"^key:disclaimer_accept$"),
                ],
                K_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, key_input)],
            },
            fallbacks=[CallbackQueryHandler(key_cancel, pattern=r"^key:(cancel|decline)$")],
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
            badge = USERBOT["real_badge"] if self._exec_ready() else USERBOT["paper_badge"]
            text = f"{badge} · {text}"
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("📊 P&L", callback_data="sb:pnl"),
                 telegram.InlineKeyboardButton("💰 Positions", callback_data="sb:pos")],
                [telegram.InlineKeyboardButton("🏦 Live Markets", callback_data="sb:live"),
                 telegram.InlineKeyboardButton("📡 Trades", callback_data="sb:trades")],
                [telegram.InlineKeyboardButton("💼 Wallet", callback_data="sb:wallet"),
                 telegram.InlineKeyboardButton("🏆 Leaderboard", callback_data="sb:lb")],
                [telegram.InlineKeyboardButton("⚙️ Settings", callback_data="sb:settings"),
                 telegram.InlineKeyboardButton("📬 Inbox", callback_data="sb:inbox")],
                [telegram.InlineKeyboardButton("❓ Help", callback_data="sb:help"),
                 telegram.InlineKeyboardButton("🛑 Kill-Switch", callback_data="sb:kill")],
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
                    f"Last error: {humanize_error(b['last_error']) if b['last_error'] else 'none'}")
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
                    f"Mode:      {USERBOT['real_badge'] if self._exec_ready() else USERBOT['paper_badge']} trading\n"
                    f"AI key:    {'set ✓' if self.registry.get_active_key(tg_id) else 'not set'}")
            kb = [
                [telegram.InlineKeyboardButton(f"⏱ Interval: {b['interval_sec']}s", callback_data="sb:set_interval:120"),
                 telegram.InlineKeyboardButton("60s", callback_data="sb:set_interval:60"),
                 telegram.InlineKeyboardButton("5m", callback_data="sb:set_interval:300")],
                [telegram.InlineKeyboardButton("🛡️ Execution Risk", callback_data="sb:execrisk")],
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
                "• AI-powered trading with real market prices.\n"
                "• Your cat decides every {interval}s and always uses stop-losses.\n"
                "• Your AI key pays for your own model calls.\n"
                "• Every trade is pushed here as a notification.\n\n"
                "Owners: use the master bot (Neko) to manage your network entry. 🐾"
            ).format(interval=self.registry.get_bot(bot_id)["interval_sec"])
            await q.message.edit_text(text, reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def wallet_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            text = self._render_wallet(bot_id, b["bot_name"], b.get("paused", 0))
            fund_buttons = []
            if self._exec_ready():
                self._exec_path()
                from wallet_ui import CHAIN_LABELS
                fund_buttons = [[telegram.InlineKeyboardButton(
                    f"💸 Fund {CHAIN_LABELS[chain].replace('🔗 ', '')}",
                    callback_data=f"sb:fund:{chain}")] for chain in self.gateway.adapters]
            kb = fund_buttons + [[
                telegram.InlineKeyboardButton("🔍 Check Deposits", callback_data="sb:check_deposits"),
                telegram.InlineKeyboardButton("▶️ Enable Agent", callback_data="sb:enable_agent"),
            ], [
                telegram.InlineKeyboardButton("🗝️ Private Keys", callback_data="sb:keys"),
                telegram.InlineKeyboardButton("💸 Withdraw", callback_data="sb:withdraw"),
            ], [
                telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"),
                telegram.InlineKeyboardButton(HOME, callback_data="sb:dash"),
            ]]
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=telegram.InlineKeyboardMarkup(kb))

        async def wallet_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            if not self._exec_ready():
                await q.message.edit_text("🗝️ Real trading not enabled — no keys to show.",
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            # show each chain's private key so the owner can export/withdraw
            lines = ["🗝️ <b>Your private keys</b>\n\n"
                     "These keys control the bot's trading wallets. Export them to "
                     "move funds to your own wallet — anyone with these can spend "
                     "the funds, so keep them secret.\n"]
            for chain in self.gateway.adapters:
                try:
                    self.gateway.provision_wallet(bot_id, chain)
                    wallet = self.gateway.ledger.wallet_by_bot_chain(bot_id, chain)
                    if not wallet or not wallet.get("key_enc"):
                        continue
                    key = self.gateway._vault.decrypt(wallet["key_enc"])
                    lines.append(f"\n🔗 {chain.upper()}\n<code>{key}</code>")
                except Exception as exc:
                    lines.append(f"\n🔗 {chain.upper()} — unavailable ({str(exc)[:40]})")
            await q.message.edit_text("\n".join(lines), parse_mode="HTML",
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")],
                                           [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def wallet_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            if not self._exec_ready():
                await q.message.edit_text("💸 Real trading not enabled — nothing to withdraw.",
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            # Manual withdrawal = export key, move funds on-chain yourself.
            text = ("💸 <b>Withdraw funds</b>\n\n"
                    "This bot is non-custodial: only you can move funds out. To "
                    "withdraw, export the private key below and sweep the balance "
                    "to your main wallet in any standard wallet app.\n\n"
                    "⚠️ The trading wallet holds the funds; the bot signs trades "
                    "but has NO withdrawal rights on Hyperliquid (API wallet).")
            await q.message.edit_text(text, parse_mode="HTML",
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton("🗝️ Show Private Keys", callback_data="sb:keys")],
                                           [telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))

        async def wallet_fund(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            _, _, chain = q.data.split(":")
            if not self._exec_ready():
                await q.message.edit_text(USERBOT["kill_no_exec"],
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            wallet = None
            try:
                self.gateway.provision_wallet(bot_id, chain)
                wallet = self.gateway.ledger.wallet_by_bot_chain(bot_id, chain)
            except Exception:
                wallet = None
            if not wallet:
                await q.message.edit_text(USERBOT["wallet_not_connected"].format(name=self.registry.get_bot(bot_id)["bot_name"]),
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            self._exec_path()
            from wallet_ui import CHAIN_LABELS
            text = USERBOT["wallet_fund"].format(
                chain_label=CHAIN_LABELS.get(chain, chain),
                address=wallet["address"])
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")],
                 [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def check_deposits(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            if not self._exec_ready():
                await q.message.edit_text(USERBOT["deposit_not_configured"],
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            await q.message.edit_text(USERBOT["deposit_checking"])
            found_all = []
            for chain in self.gateway.adapters:
                try:
                    found = self.gateway.scan_deposits(bot_id, chain) or []
                    for ev in found:
                        ev["chain"] = chain
                    found_all.extend(found)
                except Exception:
                    continue
            self._exec_path()
            from wallet_ui import CHAIN_LABELS
            if found_all:
                lines = [USERBOT["deposit_found"].format(
                    chain_label=CHAIN_LABELS.get(ev.get("chain"), ev.get("chain")),
                    amount=float(ev.get("amount") or 0),
                    asset=ev.get("asset", "")) for ev in found_all]
                await q.message.edit_text(
                    "\n\n".join(lines) + "\n\n▶️ Enable your agent to start trading.",
                    parse_mode="HTML",
                    reply_markup=telegram.InlineKeyboardMarkup(
                        [[telegram.InlineKeyboardButton("▶️ Enable Agent", callback_data="sb:enable_agent")],
                         [telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
            else:
                await q.message.edit_text(USERBOT["deposit_none"],
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton("🔍 Check Again", callback_data="sb:check_deposits")],
                                               [telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))

        async def enable_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            if q.data == "sb:enable_agent_yes":
                if not self.registry.get_active_key(tg_id):
                    await q.message.edit_text(USERBOT["enable_agent_no_key"],
                                              reply_markup=telegram.InlineKeyboardMarkup(
                                                  [[telegram.InlineKeyboardButton("🔑 Set AI Key", callback_data="key:start")]]))
                    return
                self.registry.update_bot(bot_id, paused=0)
                if self.agent_pool:
                    try:
                        self.agent_pool.start(bot_id)
                    except Exception:
                        pass
                if self.gateway:
                    try:
                        self.gateway.provision_all_wallets(bot_id)
                    except Exception:
                        pass
                await q.message.edit_text(USERBOT["enable_agent_ok"],
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton("📊 Dashboard", callback_data="sb:dash")]]))
                return
            await q.message.edit_text(USERBOT["enable_agent_prompt"],
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton("▶️ Yes, enable across all chains", callback_data="sb:enable_agent_yes")],
                                           [telegram.InlineKeyboardButton("↩️ Not yet", callback_data="sb:wallet")]]))

        async def killswitch_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            if q.data == "sb:kill_yes":
                if not self._exec_ready():
                    await q.message.edit_text(USERBOT["kill_no_exec"],
                                              reply_markup=telegram.InlineKeyboardMarkup(
                                                  [[telegram.InlineKeyboardButton(BACK, callback_data="sb:dash")]]))
                    return
                try:
                    res = self.gateway.engage_killswitch(bot_id, "user requested via Telegram")
                except Exception as exc:
                    res = {"ok": False, "error": str(exc)[:200]}
                summary = f"Fully flattened: {'YES' if res.get('fully_flattened') else 'NO — see errors below'}"
                errors = []
                for chain, r in (res.get("results") or {}).items():
                    if isinstance(r, dict) and not r.get("ok"):
                        errors.append(f"• {chain}: {r.get('error', 'failed')}")
                if errors:
                    summary += "\n" + "\n".join(errors)
                await q.message.edit_text(
                    USERBOT["kill_engaged"].format(summary=summary),
                    reply_markup=telegram.InlineKeyboardMarkup(
                        [[telegram.InlineKeyboardButton("✅ Release", callback_data="sb:kill_release")],
                         [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            if q.data == "sb:kill_release":
                try:
                    if self._exec_ready():
                        self.gateway.release_killswitch(bot_id)
                except Exception:
                    pass
                await q.message.edit_text(USERBOT["kill_released"],
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            if not self._exec_ready():
                await q.message.edit_text(USERBOT["kill_no_exec"],
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            await q.message.edit_text(USERBOT["kill_title"],
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton("🛑 Engage Kill-Switch", callback_data="sb:kill_yes")],
                                           [telegram.InlineKeyboardButton("↩️ Cancel", callback_data="sb:dash")]]))

        async def close_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Take-profit / manual close of one position (from a P&L alert button)."""
            q = update.callback_query
            await q.answer()
            _, _, symbol = q.data.split(":", 2)
            if q.data == f"sb:close_yes:{symbol}":
                try:
                    pf = self.platform.positions(platform_token)
                    pos = next((p for p in pf.get("positions", []) if p["symbol"] == symbol), None)
                    if not pos:
                        await q.message.edit_text(f"ℹ️ No open {symbol} position.")
                        return
                    action = "sell" if pos["quantity"] > 0 else "cover"
                    r = self.platform.trade(platform_token, pos["market"], symbol, action,
                                            abs(pos["quantity"]))
                    pnl = (pos.get("current_price") or pos["entry_price"]) - pos["entry_price"]
                    pnl = pnl * pos["quantity"] if pos["quantity"] >= 0 else pnl * -pos["quantity"]
                    await q.message.edit_text(
                        f"✅ <b>{'TAKE PROFIT' if pnl >= 0 else 'Closed'}: {symbol}</b>\n"
                        f"• Realized P&L: <b>${pnl:+,.2f}</b>\n"
                        f"• Exit: ${pos.get('current_price') or pos['entry_price']:,.4f}",
                        parse_mode="HTML",
                        reply_markup=telegram.InlineKeyboardMarkup(
                            [[telegram.InlineKeyboardButton("📊 Dashboard", callback_data="sb:dash")]]))
                except Exception as exc:
                    await q.message.edit_text(f"⚠️ Couldn't close {symbol}: {str(exc)[:120]}",
                                              reply_markup=telegram.InlineKeyboardMarkup(
                                                  [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            # confirmation
            try:
                pf = self.platform.positions(platform_token)
                pos = next((p for p in pf.get("positions", []) if p["symbol"] == symbol), None)
                if not pos:
                    await q.message.edit_text(f"ℹ️ No open {symbol} position.")
                    return
                qty = pos["quantity"]
                entry = pos["entry_price"]
                cur = pos.get("current_price") or entry
                pnl = (cur - entry) * (qty if qty >= 0 else -qty)
                pct = (cur / entry - 1) * 100 * (1 if qty >= 0 else -1)
            except Exception:
                pnl = pct = 0.0
            await q.message.edit_text(
                f"💰 <b>Close {symbol}?</b>\n\n"
                f"Current P&L: <b>${pnl:+,.2f}</b> ({pct:+.2f}%)\n\n"
                f"Take the profit / cut the loss now?",
                parse_mode="HTML",
                reply_markup=telegram.InlineKeyboardMarkup(
                    [[telegram.InlineKeyboardButton("✅ Yes, close", callback_data=f"sb:close_yes:{symbol}")],
                     [telegram.InlineKeyboardButton("🐾 Keep open", callback_data=f"sb:keep:{symbol}")]]))

        async def keep_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """User chose to keep a position open - acknowledge with live P&L %."""
            q = update.callback_query
            await q.answer()
            _, _, symbol = q.data.split(":", 2)
            pnl = pct = 0.0
            try:
                pf = self.platform.positions(platform_token)
                pos = next((p for p in pf.get("positions", []) if p["symbol"] == symbol), None)
                if pos:
                    qty = pos["quantity"]
                    entry = pos["entry_price"]
                    cur = pos.get("current_price") or entry
                    pnl = (cur - entry) * (qty if qty >= 0 else -qty)
                    pct = (cur / entry - 1) * 100 * (1 if qty >= 0 else -1)
            except Exception:
                pass
            # cat persona, honest about direction
            if pct < 0:
                mood = (f"📉 <b>Keeping {symbol} open.</b>\n\n"
                        f"It's down <b>{pct:+.2f}%</b> (${pnl:+,.2f}) right now.\n"
                        "The cat is watching it closely — the stop-loss is still "
                        "on guard, so the damage stays capped. 🐾")
            elif pct > 0:
                mood = (f"📈 <b>Keeping {symbol} open.</b>\n\n"
                        f"It's up <b>{pct:+.2f}%</b> (${pnl:+,.2f}) right now.\n"
                        "The cat says: let the winner run, but we'll grab it if "
                        "it slips. 🐾")
            else:
                mood = (f"🐾 <b>Keeping {symbol} open.</b>\n\n"
                        "Flat right now. The cat is waiting for a move.")
            await q.message.edit_text(
                mood + "\n\nEvery trade gets pushed here. Tap 💼 Wallet to watch it live.",
                parse_mode="HTML",
                reply_markup=telegram.InlineKeyboardMarkup(
                    [[telegram.InlineKeyboardButton("✅ Take Profit", callback_data=f"sb:close:{symbol}")],
                     [telegram.InlineKeyboardButton("📊 Dashboard", callback_data="sb:dash")]]))

        async def exec_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            if not self._exec_ready():
                text = USERBOT["exec_risk_disabled"].format(name=b["bot_name"])
            else:
                text = USERBOT["exec_risk"].format(name=b["bot_name"],
                                                   lines="\n".join(self._exec_risk_lines()))
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(BACK, callback_data="sb:settings")],
                 [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

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
        app.add_handler(CallbackQueryHandler(wallet_screen, pattern=r"^sb:wallet$"))
        app.add_handler(CallbackQueryHandler(wallet_fund, pattern=r"^sb:fund:\w+$"))
        app.add_handler(CallbackQueryHandler(check_deposits, pattern=r"^sb:check_deposits$"))
        app.add_handler(CallbackQueryHandler(enable_agent, pattern=r"^sb:enable_agent(_yes)?$"))
        app.add_handler(CallbackQueryHandler(wallet_keys, pattern=r"^sb:keys$"))
        app.add_handler(CallbackQueryHandler(wallet_withdraw, pattern=r"^sb:withdraw$"))
        app.add_handler(CallbackQueryHandler(killswitch_screen, pattern=r"^sb:(kill|kill_yes|kill_release)$"))
        app.add_handler(CallbackQueryHandler(exec_risk, pattern=r"^sb:execrisk$"))
        app.add_handler(CallbackQueryHandler(close_position, pattern=r"^sb:(close|close_yes):\w+$"))
        app.add_handler(CallbackQueryHandler(keep_open, pattern=r"^sb:keep:\w+$"))