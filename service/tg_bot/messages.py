"""All user-facing copy for master + user bots. Single source of truth."""

WELCOME_NEW = (
    "🐾 <b>Neko-Chan</b> — your AI trading cat.\n\n"
    "I watch real markets, decide with AI, and trade the platform with "
    "real prices. You bring two keys — I do the rest:\n"
    "  1️⃣ A Telegram bot token (from @BotFather) → your channel\n"
    "  2️⃣ An AI API key → my brain\n\n"
    "⚠️ Trading involves real risk and real money. Not financial advice.\n"
    "(I'm a cat. I'm not a licensed advisor. I'm just better.)"
)

WELCOME_RETURNING = (
    "🐾 Neko-Chan missed you! You have {n} bot(s): {names}.\n"
    "Manage them below."
)

HOW_IT_WORKS = {
    1: ("📖 How it works — 1/3 🐾\n\n"
        "Real market prices (BTC, ETH, US stocks, Forex) · live execution.\n"
        "I read the markets, decide with AI, and trade the platform. "
        "Purr-fectly, most of the time."),
    2: ("📖 How it works — 2/3 🐾\n\n"
        "What YOUR bot does:\n"
        "• Decides every 1–10 minutes (you pick)\n"
        "• Always has a stop-loss, position caps, daily trade limit\n"
        "• Every trade is recorded and scored on the leaderboard\n\n"
        "I never chase my tail — or your losses."),
    3: ("📖 How it works — 3/3 🐾\n\n"
        "⚠️ Trading is risky. Models make mistakes — even cats.\n"
        "Your AI key pays for your own model calls.\n"
        "You can delete your bot anytime.\n\n"
        "Nine lives of risk management. That's the deal."),
}

WIZARD = {
    "name": "🤖 Name your bot (3–24 chars, letters/numbers/space)\nExample: BitcoinWhale",
    "token": ("1️⃣ Send your Telegram bot token — create one first:\n"
              "  → open @BotFather → /newbot → copy the token (123456789:AA...)\n\nPaste it here:"),
    "token_ok": "✅ Found @{username}. Good kitty.",
    "token_bad": "❌ That token didn't work. Check @BotFather — it looks like 123456789:AAExample...",
    "verify": ("2️⃣ Send this code TO your bot @{username}:\n\n"
               "  VERIFY-{code_digits}\n\n"
               "👉 You can send just the numbers (e.g. {code_digits}) or the full "
               "text — both work.\n"
               "Order matters: FIRST press Start on @{username} (a bot can't "
               "receive anything until you start it), THEN send the code above. "
               "Tap \"I sent it\" ONLY after the code is sent — we'll watch for up "
               "to 60 seconds.\n\n"
               "If the code expired, tap the button below for a fresh one."),
    "verify_ok": "✅ Ownership verified. That's the cat's seal of approval.",
    "verify_bad": "❌ We didn't receive the code. Send it again to your bot and retry.",
    "verify_no_chat": ("⚠️ We haven't seen anything from @{username} yet.\n\n"
                       "Most likely you never pressed Start on it — open the bot in "
                       "Telegram, tap Start (this activates the chat), then send this code:\n\n"
                       "  {code}\n\nThen tap \"I sent it\"."),
    "disclaimer": ("⚠️ IMPORTANT — READ BEFORE CONTINUING\n\n"
                   "This bot is an automated trading agent. Before you connect it, "
                   "please understand:\n\n"
                   "• This is NOT financial advice. No one at this platform is "
                   "giving you investment advice.\n"
                   "• The bot does NOT guarantee profit. It CAN lose money.\n"
                   "• You are trusting your funds to an automated agent. It will "
                   "make mistakes — sometimes costly ones.\n"
                   "• You alone are responsible for the money you allocate and "
                   "for any losses.\n"
                   "• Only trade with money you can afford to lose.\n\n"
                   "By continuing you confirm you have read and accept these "
                   "terms."),
    "disclaimer_accepted": "✅ Disclaimer accepted. Pawsitive progress.",
    "ai_key_provider": "3️⃣ Your AI API key — this powers your bot's decisions.\nPick a provider:",
    "ai_key_prompt": "Paste your {label} key:",
    "ai_key_ok": "✅ Key works ({provider}). Model: {model}",
    "ai_key_bad": "❌ Provider rejected this key. Double-check it.",
    "ai_key_ratelimit": "⏳ Provider is rate-limited — wait a minute.",
    "ai_key_network": "⏳ Can't reach provider ({reason}).",
    "risk": ("4️⃣ Risk profile\n"
             "🛡️ Conservative — few trades, tight stops, cash-preferred\n"
             "⚖️ Balanced — usual defaults, active mode\n"
             "🚀 Aggressive — max trades, wider size"),
    "interval": ("⏱ How often should your bot decide?\n"
                 "  1m = fastest, ~1,440 AI calls/day\n"
                 "  2m = recommended\n"
                 "  5m = calmer, fewer fees\n"
                 "  10m = quiet, ~144 calls/day"),
    "markets": ("5️⃣ Markets (toggle, ✅ = on)\n"
                "  ⚡ Perps — BTC/ETH with leverage 1–10x (margin, liquidation, funding)\n"
                "  ₿ Spot — BTC/ETH regular\n"
                "  📈 US Stocks — AAPL, NVDA…\n"
                "  💱 Forex — EURUSD, USDJPY…"),
    "leverage": ("⚖️ Leverage for Perps (1–10x)\n"
                 "⚠️ Higher leverage = faster liquidation. Real perp risk."),
    "start": ("Everything is set 🎉\n"
              "  Bot: {name}\n"
              "  Channel: @{username} (verified)\n"
              "  AI: {provider} · {key_masked}\n"
              "  Risk: {risk}\n"
              "  Interval: {interval}\n"
              "  Markets: {markets}"),
    "done": ("✅ {name} is registered!\n"
             "Your bot is live → open @{username} and press Start.\n\n"
             "What happens next:\n"
             "  🔔 Every trade/stop will be pushed to your bot\n"
             "  📊 Dashboard, P&L, leaderboard inside your bot\n"
             "🐾 Neko-Chan is on the clock. Literally."),
    "cancel": "Wizard canceled. Nothing was saved.",
    "timeout": "⏳ Wizard timed out. Resume with /start → Add My Bot.",
}

MENU = {
    "main_new": "🚀 Add My Bot",
    "main_return": "🤖 My Bots",
    "how": "📖 How It Works",
    "leaderboard": "🏆 Leaderboard",
    "help": "❓ Help",
}

USERBOT = {
    "dashboard": ("📊 {name}\n"
                  "🟢 RUNNING · last decision {ago}\n"
                  "P&L {pnl} ({pct})    rank #{rank}\n"
                  "Cash ${cash}\n"
                  "Open {open} position(s)"),
    "welcome_push": "🔔 Every trade lands here. Tap 📊 P&L to start.",
    "positions_header": "Open positions ({n})",
    "close_confirm": "Close {symbol} {side} {qty} now?",
    "closed_ok": "✅ Closed {symbol} at {price} ({pnl}, fee ${fee})",
    "pause_confirm": "Pause {name}? Positions stay open, no new decisions.",
    "paused": "⏸️ Paused. No new decisions. Positions stay open.",
    "resumed": "▶️ Bot resumed. The cat is back on the clock.",
    "delete_confirm": "Really delete {name}? This stops it and removes your keys from our servers.",
    "deleted": "🗑️ Deleted. Goodbye. Your agent history stays on the platform. Neko-Chan will remember you.",
    "settings_saved": "Saved ✓ ({value})",
    # ---- real trading (execution gateway) ----
    "wallet_header": "💼 Wallet — {name}",
    "wallet_disabled": ("💼 Wallet — {name}\n\n"
                        "⚪ Live execution is not enabled for this bot yet.\n"
                        "Your trading wallet isn't connected to a live venue.\n\n"
                        "When the operator enables live execution, your on-chain "
                        "wallet, balances, positions and the kill-switch will appear here."),
    "wallet_not_connected": ("💼 Wallet — {name}\n\n"
                             "⚪ No chain wallet is connected for this bot.\n"
                             "[connect via Bot Settings]"),
    "wallet_fund": ("💸 Fund this wallet\n\n"
                    "Send funds to this address on the {chain_label} network.\n\n"
                    "  <code>{address}</code>\n\n"
                    "• Native gas + USDC are both accepted\n"
                    "• Only send {chain_label} network assets to this address\n"
                    "• The bot becomes active once a deposit is detected"),
    "kill_title": ("🛑 KILL-SWITCH\n\n"
                   "This flattens ALL positions and cancels ALL open orders on "
                   "every chain for this bot, immediately.\n\n"
                   "⚠️ It cannot be undone automatically. Trading stays halted "
                   "until you release it."),
    "kill_no_exec": "🛑 Kill-switch: real trading is not enabled on this bot.",
    "kill_engaged": ("🛑 KILL-SWITCH ENGAGED\n\n"
                     "{summary}\n\n"
                     "All trading for this bot is halted. The cat is sitting this one out."),
    "kill_released": "✅ Kill-switch released. Trading can resume. I'm back, sweety.",
    "exec_risk": ("🛡️ Execution risk — {name}\n\n"
                  "{lines}\n\n"
                  "These hard caps are checked BEFORE any order is signed. They "
                  "cannot be overridden by the AI model. Or the cat."),
    "exec_risk_disabled": ("🛡️ Execution risk — {name}\n\n"
                           "Real trading is not enabled. These caps apply only "
                           "when the operator activates execution."),
    "real_badge": "🔐 LIVE",
    "paper_badge": "🧪 SIM",
}

NOTIF = {
    "fill": "✅ FILL {action} {symbol} {qty} @ ${price} ({leverage_text}stop {stop}%, target {take}%)",
    "stop": "🛑 STOP: {symbol} closed {pnl} ({pct}% stop). The cat saw it coming.",
    "target": "🎯 TARGET: {symbol} closed +{pnl} ({pct}% target). Neko-Chan called it.",
    "liq": "💥 LIQUIDATED: {symbol} {lev}x at ${price} — margin lost. Even cats miss sometimes.",
    "started": "▶️ Neko-Chan started ({interval}s cycle). Let's get this bag.",
    "paused": "⏸️ Paused. Cat is napping.",
    "error_first": "⚠️ {message} — retrying. No trade this cycle. The cat is unbothered.",
    "error_batch": "⚠️ Still retrying ({n} issues). Neko-Chan is patient.",
    "daily": "📅 Today: {pnl} · {trades} trades · win {win}% · fees ${fees}",
    "weekly": "📈 Week: {pnl} · {trades} trades · win {win}% · rank #{rank}",
    "milestone": "🚀 +{pct}% ({equity} equity). The cat smells green candles.",
    "milestone_down": "⚠️ -{pct}% — the cat suggests pausing. She's usually right.",
    "back_online": "✅ Back online. The cat stretched. Ready to trade.",
}

ERRORS = {
    "unknown_market": "I only understand buttons. Tap 🏠 Home. (I'm a cat, not a keyboard.)",
    "unauthorized": "⛔ Unauthorized. Neko-Chan says no.",
    "platform_down": "⚠️ Our platform is down. The cat is kneading the server. Try again soon.",
    "duplicate_token": "This bot is already registered. One cat per household, please.",
    "duplicate_key": "That key is already powering another bot. Each cat needs their own.",
    "name_taken": "That name is taken — try {suggestion}.",
}


def mask_key(k: str) -> str:
    from key_vault import KeyVault

    return KeyVault.mask(k)


# ---------------------------------------------------------------------------
# Human-readable error mapping. Platform/venue errors are terse technical
# strings; map the known ones to actionable copy so a non-technical user
# understands what happened and what to do. Unknown errors pass through with a
# short prefix.
# ---------------------------------------------------------------------------

_ERROR_MAP = (
    # platform (service/server) trade-validation errors
    ("Short position entry price is missing",
     "Couldn't open the short — the platform needs an entry price for shorts. "
     "This is a known platform quirk; the bot will keep trying with a valid price."),
    ("stop_loss_pct/take_profit_pct can only be set when opening (buy/short) a position",
     "Closing trades can't carry a stop/target — the close was sent without one and "
     "is safe to retry."),
    ("US market is closed", "US stocks only trade Mon–Fri 9:30–16:00 ET. Try again during market hours."),
    ("market is currently closed", "That market is closed right now — the bot will retry when it reopens."),
    ("Invalid quantity", "The bot asked for an invalid quantity. No trade was placed."),
    ("Quantity too large", "That position size is over the limit. The bot sized down."),
    ("Leverage must be between 1 and 10", "That leverage is out of range (1–10x). The bot stayed flat."),
    ("Leverage is only supported for crypto",
     "Leverage only applies to crypto perps — the bot traded without leverage instead."),
    ("Unable to fetch current price", "Couldn't get a live price for this market. No trade was placed."),
    ("Unable to fetch historical price", "Historical price unavailable — the bot skipped the backfill."),
    ("Invalid token", "Your bot's session expired. Reconnect it in the master bot."),
    ("Invalid price", "The trade had an invalid price. No order was placed."),
    ("Price too large", "The trade price was out of range. No order was placed."),
    ("already long in symbol", "Your bot is already long this symbol — it didn't double up."),
    ("already short in symbol", "Your bot is already short this symbol — it didn't double up."),
    ("daily trade limit reached", "Today's trade limit is hit. Your bot will resume tomorrow."),
    ("position size cap exceeded", "The position was too large for your risk settings. The bot stayed flat."),
    # execution / venue errors
    ("no adapter registered", "No trading venue is configured for this chain yet."),
    ("no wallet for bot", "No wallet is linked to this bot on that chain."),
    ("not configured", "This feature isn't configured on the trading backend yet."),
    ("killswitch engaged", "Trading is halted by the kill-switch."),
    ("duplicate idempotency_key", "This order was already placed — it wasn't sent twice."),
    ("timeout", "The trading venue timed out. The bot will retry."),
    ("rate limit", "The trading venue is rate-limiting. The bot will retry shortly."),
    ("connection", "Couldn't reach the trading venue. The bot will retry."),
)


def humanize_error(raw: str, max_len: int = 300) -> str:
    """Map a terse technical error to friendly, actionable copy.

    Falls back to the raw text (truncated) for unknown errors so the user is
    never left with nothing.
    """
    raw = (raw or "").strip()
    if not raw:
        return "Unknown error — no trade was placed."
    lowered = raw.lower()
    for needle, friendly in _ERROR_MAP:
        if needle.lower() in lowered:
            return friendly
    if len(raw) > max_len:
        return f"⚠️ {raw[:max_len]}…"
    return f"⚠️ {raw}"