"""Degen Telegram screens (§6). Pure renderers + thin PTB handlers.

Every screen is one editable message (§6.1). All actions are inline taps; the only
text verbs are `Track <addr>`, `Snipe <addr>` and a bare CA paste (§6.4). Card
lifecycle per §6.3c: commit deletes card+input, un-touched cards expire in 3 min.

Callback_data is capped at 64 bytes by Telegram — full curve ids/addresses NEVER
ride inline; every button carries a short `ui_ref` token resolved via the ledger.

Mount from userbot: `ui = DegenUI(...)`; register `ui.command_handlers()`,
`ui.callback_handlers()`, and route non-command text through `ui.handle_text`.
"""

from __future__ import annotations

import logging
import re
from html import escape as esc

from telegram import InlineKeyboardButton, InlineKeyboardMarkup as KB, Update


def B(text, data=None, url=None):
    """InlineKeyboardButton whose 2nd positional is callback_data (NOT url).

    InlineKeyboardButton(text, url=..., callback_data=...) makes `B("label", "cb")`
    a broken URL button; PTB rejects it (BadRequest: invalid url). Wrap it here so
    every degen button is a real callback button."""
    return InlineKeyboardButton(text, callback_data=data, url=url)
from telegram.ext import CallbackQueryHandler, CommandHandler

from . import constants as K
from .db import DegenLedger
from .launchpad import resolve_input
from .metrics import compute
from .validate import run_gauntlet

log = logging.getLogger(__name__)

CARD_TTL_S = 180
ADDR_RE = re.compile(r"(?i)^0x[0-9a-f]{1,64}")


class DegenUI:
    """bot_of(tg_id) -> bots row dict (registry lookup injected by userbot)."""

    def __init__(self, ch, ledger: DegenLedger, executor=None, bundles=None,
                 bot_of=None, ai_key_ok=None, wallet_addr=None):
        self.ch, self.led, self.ex, self.bundles = ch, ledger, executor, bundles
        self._bot_of = bot_of or (lambda tg: None)
        self._ai_ok = ai_key_ok or (lambda bot: bool(bot and bot.get("has_ai_key")))
        self._wallet_addr = wallet_addr or (lambda bot: "")
        # Degen is a VIEW on the shared main dashboard (§ user policy): the same
        # message, only the keyboard swaps. The mount injects `dash_render` =
        # the userbot's own dash() coroutine; without it we fall back to
        # rendering strip+keyboard standalone (tests / pre-mount).
        self.degen_view: set[int] = set()
        self.dash_render = None

    def degen_on(self, bot_id: int) -> bool:
        return int(bot_id) in self.degen_view

    def enter(self, bot_id: int) -> None:
        self.degen_view.add(int(bot_id))

    def exit(self, bot_id: int) -> None:
        self.degen_view.discard(int(bot_id))

    # ---------------- registration ----------------
    def command_handlers(self):
        return [CommandHandler("degen", self.cmd_degen)]

    def callback_handlers(self):
        return [CallbackQueryHandler(self.on_cb, pattern=r"^dg:")]

    # ---------------- helpers ----------------
    def _bot(self, update):
        return self._bot_of(update.effective_user.id)

    def _bid(self, update) -> int | None:
        b = self._bot(update)
        return int(b["id"]) if b else None

    def _ref(self, bid: int, kind: str, value: str) -> str:
        return self.led.make_ref(bid, kind, value)

    def _sched(self, bot_id, chat_id, mid, kind="card", curve_id="", ttl=CARD_TTL_S):
        if mid:
            self.led.schedule_delete(bot_id, chat_id, mid, kind, seconds=ttl,
                                     curve_id=curve_id)

    # ---------------- commands / text ----------------
    async def cmd_degen(self, update: Update, context):
        bot = self._bot(update)
        if not bot:
            await update.message.reply_text("⛔ No bot here yet — talk to @Neko_tradesbot.")
            return
        self.enter(int(bot["id"]))
        await self._render(update, context, bot)

    async def handle_text(self, update: Update, context) -> bool:
        """Returns True if handled. Track/Snipe verbs + CA paste (§6.3a)."""
        txt = (update.message.text or "").strip()
        bot = self._bot(update)
        if not bot:
            return False
        bid = int(bot["id"])
        cfg = self.led.get_config(bid)
        if not cfg.get("enabled"):
            return False
        if txt.lower().startswith("track ") and ADDR_RE.match(txt[6:]):
            self.led.track_wallet(bid, txt[6:].strip().split()[0])
            await update.message.reply_text("👀 Tracking that wallet — profile in Copy.",
                                            reply_markup=self._hub_kb())
            await self._purge_input(update)
            return True
        if txt.lower().startswith("snipe ") and ADDR_RE.match(txt[6:]):
            self.led.watch_deployer(bid, txt[6:].strip().split()[0])
            await update.message.reply_text("🪝 Watching that deployer — launches fire here.",
                                            reply_markup=self._hub_kb())
            await self._purge_input(update)
            return True
        if ADDR_RE.match(txt):
            st = resolve_input(self.ch, txt)
            self._current_bot = bot
            txt2, kb = await self.card(bot, cfg, st)
            msg = await update.message.reply_text(txt2, parse_mode="HTML", reply_markup=kb)
            self._sched(bid, msg.chat_id, msg.message_id, "card",
                        curve_id=getattr(st, "curve_id", ""))
            await self._purge_input(update)
            return True
        return False

    async def _purge_input(self, update):
        """§6.3c: the user's own message disappears with the card it spawned."""
        try:
            await update.message.delete()
        except Exception:
            pass

    async def _sweep(self, context, bot_id: int) -> None:
        """Delete cards/sheets past their 3-min TTL (receipts are never scheduled)."""
        if context is None or getattr(context, "bot", None) is None:
            return
        for row in self.led.due_deletes():
            if row["bot_id"] != bot_id or row["kind"] == "receipt":
                continue
            try:
                await context.bot.delete_message(row["chat_id"], row["message_id"])
            except Exception:
                pass
            self.led.clear_delete(row["chat_id"], row["message_id"])

    # ---------------- degen view (shared main dashboard + swapped buttons) ----
    def degen_strip(self, bot, cfg) -> str:
        """2-3 compact status lines appended to the MAIN dashboard text. Same
        typography as the dashboard: bold header, <code> figures, ─ rules."""
        bid = int(bot["id"])
        line = "─" * 26
        stats = self.led.snipe_stats(bid)
        wallets = self.led.bundle_wallets(bid)
        watch = self.led.watched_deployers(bid)
        pos = self.led.positions(bid)
        spend = self.led.spend_today(bid)
        caps = cfg.get("caps", {}) or {}
        if not cfg.get("enabled"):
            body = "  <b>OFF</b> — high-risk lane: snipe, copy, DCA, bundles 🐸"
        else:
            state = "<b>⚠️ KILLED</b>" if caps.get("killed") else "<b>ON</b>"
            body = (f"  {state} · spent today <code>{spend:.2f}</code> / "
                    f"budget <code>{cfg['budget_sui']:.0f} SUI</code> · "
                    f"open <code>{len(pos)}</code>\n"
                    f"  🪝 <code>{len(watch)}</code> deployers · fired "
                    f"<code>{stats['fired']}</code> · 🧺 <code>{len(wallets)}/20</code> "
                    f"wallets · venue <code>{cfg['launchpads']}</code>")
        return (f"\n<code>{line}</code>\n🎰 DEGEN\n<code>{line}</code>\n{body}")

    def degen_keyboard(self, bot, cfg):
        """The degen-mode button block (replaces the perps buttons in place)."""
        if not self._ai_ok(bot):
            return KB([[B("🔑 Connect AI Key", "key:start")],
                       [B("📊 Main Dashboard", "dg:main")]])
        if not cfg.get("enabled"):
            return KB([[B("🟢 Enable Degen", "dg:on")],
                       [B("📊 Main Dashboard", "dg:main")]])
        # Same rhythm as the main dashboard: at most 2 per row, full-width for
        # the primary action. Nothing 3-or-4-across.
        caps = cfg.get("caps", {}) or {}
        kill_row = [B("♻️ Release Kill", "dg:unkill")] if caps.get("killed") \
            else [B("🛑 KILL", "dg:kill")]
        return KB([
            [B("🎯 Buy a meme", "dg:buy")],
            [B("📊 Degen Positions", "dg:pos"), B("📋 Orders", "dg:orders")],
            [B("🪝 Sniper", "dg:sniper"), B("👥 Copy", "dg:copy")],
            [B("🧺 Bundle", "dg:bundle"), B("🛡 Risk", "dg:risk")],
            [B("🐸 Suipump", "dg:lp:suipump"), B("💣 Blast 🔒", "dg:lp:blast")],
            [B("⚡ Both venues", "dg:lp:both"), B("⏻ Disable", "dg:off")],
            kill_row,
            [B("📊 Main Dashboard", "dg:main")],
        ])

    async def _render(self, update, context, bot):
        """Repaint the SAME message: main dashboard + degen strip + degen kb.
        The mount injects dash_render (the userbot's own dash handler)."""
        if self.dash_render is not None:
            await self.dash_render(update, context)
            return
        # standalone fallback (tests / pre-mount): strip + kb only
        cfg = self.led.get_config(int(bot["id"]))
        txt = f"<b>🐾 {esc(bot.get('bot_name') or 'bot')}</b>{self.degen_strip(bot, cfg)}"
        kb = self.degen_keyboard(bot, cfg)
        if update.callback_query:
            await update.callback_query.edit_message_text(txt, parse_mode="HTML",
                                                          reply_markup=kb)
        else:
            await update.message.reply_text(txt, parse_mode="HTML", reply_markup=kb)

    def _hub_kb(self):
        # Back button for sections/cards: dg:hub repaints whatever view is
        # active (degen stays degen); exiting is the explicit Main Dashboard btn.
        return KB([[B("← Back", "dg:hub")]])

    _current_bot = None

    async def card(self, bot, cfg, st):
        """Token Card / wallet card / locked card router (§6.3a-b)."""
        bid = int(bot["id"])
        if st.kind == "wallet":
            w = f"<code>{esc(st.creator[:10])}…</code>"
            r_c = self._ref(bid, "wallet", st.creator)
            return (f"<b>👛 WALLET {w}</b>\nCopy it or watch its launches."), KB([[
                B("👥 Copy", f"dg:copyadd:{r_c}"),
                B("🪝 Watch", f"dg:watch:{r_c}"), B("← hub", "dg:hub")]])
        if st.kind == "blast_locked":
            return ("<b>💣 BLAST — 🔒 coming soon</b>\nNot trading yet: we enable "
                    "it only after auditing their contracts, same as Suipump."), \
                KB([[B("🔔 Ping me", "dg:blast_ping"), B("← hub", "dg:hub")]])
        if st.kind == "unknown":
            return ("⛔ Can't trade that.\n" + esc("; ".join(st.reasons)),
                    KB([[B("← hub", "dg:hub")]]))
        m = compute(self.ch, st)
        badge = {"curve": "🟢 LIVE on curve", "graduating": "⏳ GRADUATING — untradeable",
                 "pool": "🎨 GRADUATED → Aftermath", "generic": "🌐 generic token"}[st.kind]
        allowed, hp = await self._gauntlet(bot, st)
        head = (f"<b>{esc(st.symbol or st.token_type[:8])}</b> {badge}\n"
                f"price: {m.price_sui:.2e} SUI · mcap ≈ ${m.mcap_usd:,.0f}\n"
                f"grad: {m.progress_bps/100:.0f}% · left {m.grad_left_sui:.0f} SUI · "
                f"liq {m.liq_sui:.0f} SUI\n" + hp)
        r_curve = self._ref(bid, "curve", st.curve_id or st.token_type)
        chips = [B(x, f"dg:amt:{r_curve}:{x}") for x in ("0.2", "0.5", "1", "Max")]
        kb = [chips,
              [B("📉 5%", "dg:slp:5"), B("📊 10%", "dg:slp:10"), B("🤘 25%", "dg:slp:25")],
              [B("🚀 BUY", f"dg:buy:{r_curve}")] if allowed
              else [B("⛔ blocked", "dg:hub")],
              [B("🧺 Bundled buy", f"dg:burst:{r_curve}"), B("← hub", "dg:hub")]]
        return head, KB(kb)

    async def _gauntlet(self, bot, st):
        if st.kind not in ("curve", "pool", "graduating", "generic"):
            return False, ""
        g = run_gauntlet(self.ch, st)
        if g.allowed:
            hp = ("🟢 honeypot check ✓ live" if g.honeypot == "pass" else
                  "🟡 honeypot untested" if g.honeypot in ("untested", "skipped")
                  else "🟢 sellable ✓")
            extra = ("\n⚠ " + esc(" · ".join(g.risks))) if g.risks else ""
            return True, f"{hp}{extra}"
        return False, "⛔ " + esc(" · ".join(g.blocks))

    # ---------------- callback router ----------------
    async def on_cb(self, update: Update, context):
        q = update.callback_query
        await q.answer()
        bot = self._bot(update)
        if not bot:
            return
        bid = int(bot["id"])
        cfg = self.led.get_config(bid)
        data = q.data
        if data == "dg:on":
            if not self._ai_ok(bot):
                await q.answer("⛔ Connect your AI key first", show_alert=True)
                return
            self.led.set_config(bid, enabled=1)
            self.enter(bid)
            await self._render(update, context, bot)
        elif data == "dg:off":
            self.led.set_config(bid, enabled=0)
            self.exit(bid)
            await self._render(update, context, bot)
        elif data == "dg:main":
            self.exit(bid)
            await self._render(update, context, bot)
        elif data == "dg:hub":
            await self._render(update, context, bot)
        elif data.startswith("dg:lp:"):
            lp = data.split(":")[2]
            if lp == "blast" or (lp == "both" and not K.BLAST_LIVE):
                await q.answer("💣 Blast 🔒 coming soon — both = Suipump until then",
                               show_alert=True)
            else:
                self.led.set_config(bid, launchpads=lp)
                await self._render(update, context, bot)
        elif data == "dg:kill":
            self.led.set_config(bid, caps={**(cfg.get("caps") or {}), "killed": True})
            if self.ex:
                self.ex.kill(bid, True)
            await q.edit_message_text("🛑 KILL engaged: degen firing halted. Cancel armed "
                              "rungs + close positions below.",
                              reply_markup=KB([[B("📊 Positions", "dg:pos"),
                                                B("📋 Orders", "dg:orders"),
                                                B("♻️ Un-kill", "dg:unkill")]]))
        elif data == "dg:unkill":
            self.led.set_config(bid, caps={**(cfg.get("caps") or {}), "killed": False})
            if self.ex:
                self.ex.kill(bid, False)
            await self._render(update, context, bot)
        elif data.startswith(("dg:watch:", "dg:copyadd:")):
            _, kind, ref = data.split(":")
            rv = self.led.resolve_ref(ref, bid)
            if not rv:
                await q.edit_message_text("expired — reopen from hub.", reply_markup=self._hub_kb())
                return
            addr = rv[1]
            if kind == "watch":
                self.led.watch_deployer(bid, addr)
            else:
                self.led.track_wallet(bid, addr)
            await q.edit_message_text("✓ added. Configure in the matching screen.",
                              reply_markup=KB([[B("🪝 Sniper", "dg:sniper"),
                                                B("👥 Copy", "dg:copy"),
                                                B("← hub", "dg:hub")]]))
        elif data.startswith("dg:amt:"):
            await self._set_amount(q, bid, data)
        elif data.startswith("dg:buy:"):
            await self._confirm_buy(q, bot, cfg, bid, data.split(":")[2])
        elif data.startswith("dg:burst:"):
            await self._confirm_burst(q, bot, cfg, bid, data.split(":")[2])
        elif data.startswith(("dg:cconfirm:", "dg:cburst:")):
            await self._execute_commit(update, context, q, bot, cfg, bid, data)
        elif data == "dg:gen5":
            if self.bundles:
                made = self.bundles.generate(bid, 5)
                ws = self.led.bundle_wallets(bid)
                await q.edit_message_text(f"✅ {len(made)} new wallets · {len(ws)}/20 total\n"
                                  f"keys stored in your bot row, one slot each",
                                  reply_markup=KB([[B("🧺 Bundle", "dg:bundle"), B("← hub", "dg:hub")]]))
            else:
                await q.edit_message_text("Bundle manager offline", reply_markup=KB([[B("← hub", "dg:hub")]]))
        elif data == "dg:buy":
            await q.edit_message_text("🎯 Paste the token's contract address (or its "
                                      "launchpad link) — I'll open its card.",
                                      reply_markup=self._hub_kb())
        elif data in ("dg:pos", "dg:orders", "dg:sniper", "dg:copy", "dg:bundle", "dg:risk"):
            txt, kb = await self._section(bot, cfg, data[3:])
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
        else:
            await self._render(update, context, bot)

    async def _set_amount(self, q, bid, data):
        parts = data.split(":")
        ref, amt = parts[2], parts[3]
        self.led.set_config(bid, caps={**(self.led.get_config(bid).get("caps") or {}),
                                       "_amt_" + ref: amt})
        await q.edit_message_text(f"amount set {amt} SUI — press BUY.",
                          reply_markup=KB([[B("🚀 BUY", f"dg:buy:{ref}"),
                                            B("← hub", "dg:hub")]]))

    # ---------------- confirm flow (§6.3) ----------------
    async def _confirm_buy(self, q, bot, cfg, bid, ref):
        rv = self.led.resolve_ref(ref, bid)
        if not rv:
            await q.edit_message_text("expired.", reply_markup=self._hub_kb())
            return
        st = resolve_input(self.ch, rv[1])
        m = compute(self.ch, st)
        caps = cfg.get("caps", {}) or {}
        amt = float(caps.get("_amt_" + ref, min(0.5, float(caps.get("per_order", 0.5)))))
        txt = (f"Confirm BUY {esc(st.symbol or st.token_type[:8])} ≈ <b>{amt} SUI</b>\n"
               f"you get ≈ {amt / max(m.price_sui, 1e-18):,.0f} · fee 0.5% → Neko\n"
               f"⛔️ Cancel returns to hub.")
        kb = KB([[B("✅ Confirm", f"dg:cconfirm:{ref}:{amt}"),
                  B("⛔ Cancel", "dg:hub")]])
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
        self._sched(bid, q.message.chat_id, q.message.message_id, "confirm")

    async def _confirm_burst(self, q, bot, cfg, bid, ref):
        rv = self.led.resolve_ref(ref, bid)
        if not rv:
            await q.edit_message_text("expired.", reply_markup=self._hub_kb())
            return
        legs = self.bundles.funding_plan(bid, 3.0) if self.bundles else []
        txt = (f"💥 SPREAD-BURST into {esc(rv[1][:10])}…\n"
               f"3.0 SUI across {max(1, len(legs))} wallets (separate txs — not atomic)\n"
               f"⚠️ bundle fee 5.0 SUI → Neko")
        kb = KB([[B("✅ FIRE ALL", f"dg:cburst:{ref}"), B("⛔ Cancel", "dg:hub")]])
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)

    async def _execute_commit(self, update, context, q, bot, cfg, bid, data):
        parts = data.split(":")
        ref = parts[2]
        rv = self.led.resolve_ref(ref, bid)
        if not rv:
            await q.edit_message_text("expired.", reply_markup=self._hub_kb())
            return
        st = resolve_input(self.ch, rv[1])
        if parts[1] == "cburst":
            legs = self.bundles.funding_plan(bid, 3.0) if self.bundles else []
            res = (self.ex.spread_burst(bid, launchpad="suipump", curve_id=st.curve_id,
                                        token_type=st.token_type,
                                        curve_isv=st.curve_obj.get("shared_version", 0),
                                        total_sui=3.0, min_out_each=0, legs=legs)
                   if self.ex else {"ok": False, "error": "no executor"})
        else:
            amt = float(parts[3]) if len(parts) > 3 else 0.5
            res = (self.ex.buy(bid, launchpad="suipump", curve_id=st.curve_id,
                               token_type=st.token_type,
                               curve_isv=st.curve_obj.get("shared_version", 0),
                               sui_amount=amt, min_out=0, idem=f"ui{ref}:{amt}")
                   if self.ex else {"ok": False, "error": "no executor"})
        # §6.3c: sweep expired cards/sheets. We turn THIS message into the pinned
        # receipt via edit (do NOT delete the message we are about to edit).
        await self._sweep(context, bid)
        ok = res.get("ok")
        txt = ("✅ <b>FILL</b>\ndigest <code>" + esc(str(res.get("digest", ""))[:14]) +
               "…</code>") if ok else "❌ rejected: " + esc(str(res.get("error", "caps")))
        kb = KB([[B("📊 Positions", "dg:pos"), B("🎰 hub", "dg:hub")]])
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)

    # ---------------- sections ----------------
    async def _section(self, bot, cfg, name):
        bid = int(bot["id"])
        back = [B("← hub", "dg:hub")]
        if name == "pos":
            rows = self.led.positions(bid)
            if not rows:
                return "<b>📊 POSITIONS</b>\nNo open degen positions. Paste a CA to start.", \
                    KB([back])
            lines = ["<b>📊 POSITIONS</b>"]
            for p in rows:
                lines.append(f"· {esc(p['symbol'] or p['curve_id'][:8])} [{p['venue']}] "
                             f"{p['entry_sui']:.2f} SUI")
            return "\n".join(lines), KB([back])
        if name == "orders":
            rows = self.led.armed_orders(bid)
            lines = ["<b>📋 ARMED ORDERS</b>"]
            for o in rows:
                trig = f"@{o['target_price']:.1e}" if o["target_price"] else "now"
                lines.append(f"· #{o['id']} {o['intent']}/{o['otype']} {o['qty_sui']} SUI "
                             f"{trig} [{o['state']}]")
            return "\n".join(lines), KB([back])
        if name == "sniper":
            ws = self.led.watched_deployers(bid)
            lines = ["<b>🪝 SNIPER</b> — fire on watched deployers' launches",
                     "type: <code>Snipe 0x…</code>"]
            for w in ws:
                lines.append(f"· <code>{esc(w['deployer'][:10])}…</code> {w['mode']} "
                             f"{w['size_sui']} SUI · L{w['launches']} F{w['fails']}")
            return "\n".join(lines), KB([back])
        if name == "copy":
            ts = self.led.tracked_wallets(bid)
            lines = ["<b>👥 COPY</b> — notify-first by default",
                     "type: <code>Track 0x…</code>"]
            for t in ts:
                lines.append(f"· <code>{esc(t['target'][:10])}…</code> {t['size_pct']:.0f}% "
                             f"{t['mode']}")
            return "\n".join(lines), KB([back])
        if name == "bundle":
            ws = self.led.bundle_wallets(bid)
            lines = [f"<b>🧺 BUNDLE</b> — {len(ws)}/20 wallets"]
            for w in ws:
                lines.append(f"· #{w['slot']} <code>{esc(w['address'][:10])}…</code>")
            kb = [[B("+ generate ×5", "dg:gen5")], back]
            return "\n".join(lines), KB(kb)
        if name == "risk":
            caps = cfg.get("caps", {}) or {}
            return (f"<b>🛡 RISK &amp; CAPS</b>\nbudget {cfg['budget_sui']} SUI · "
                    f"per-order {caps.get('per_order', 2)} · daily loss "
                    f"{caps.get('daily_loss', 6)} · max open {caps.get('max_open', 8)}\n"
                    f"[+] [−] steppers wired in P5b"), KB([back])
        return "—", KB([back])

    async def on_gen_bundles(self, update, context):
        q = update.callback_query
        await q.answer()
        bid = self._bid(update)
        if bid and self.bundles:
            made = self.bundles.generate(bid, 5)
            await q.edit_message_text(f"✅ generated {len(made)} wallets (keys stored in your row)",
                              reply_markup=KB([[B("🧺 Bundle", "dg:bundle"),
                                                B("← hub", "dg:hub")]]))
