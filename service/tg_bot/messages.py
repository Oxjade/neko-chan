"""All user-facing copy for master + user bots. Single source of truth."""

WELCOME_NEW = (
    "👋 Welcome to AI-Trader Bot Network!\n\n"
    "Run your own AI trading bot on a paper platform with real prices.\n"
    "You bring two keys — we do the rest:\n"
    "  1️⃣ A Telegram bot token (from @BotFather) → your channel\n"
    "  2️⃣ An AI API key → your bot's brain\n\n"
    "⚠️ Paper trading only. No real money. Not financial advice."
)

WELCOME_RETURNING = (
    "Welcome back! You have {n} bot(s): {names}.\n"
    "Manage them below."
)

HOW_IT_WORKS = {
    1: ("📖 How it works — 1/3\n\n"
        "Real market prices (BTC, ETH, US stocks, Forex) · paper money ($100k).\n"
        "Your bot reads markets, decides with AI, trades on the platform."),
    2: ("📖 How it works — 2/3\n\n"
        "What YOUR bot does:\n"
        "• Decides every 1–10 minutes (you pick)\n"
        "• Always has a stop-loss, position caps, daily trade limit\n"
        "• Every trade is recorded and scored on the leaderboard"),
    3: ("📖 How it works — 3/3\n\n"
        "⚠️ Paper only. No real money. Models make mistakes.\n"
        "Your AI key pays for your own model calls.\n"
        "You can delete your bot anytime."),
}

WIZARD = {
    "name": "🤖 Name your bot (3–24 chars, letters/numbers/space)\nExample: BitcoinWhale",
    "token": ("1️⃣ Send your Telegram bot token — create one first:\n"
              "  → open @BotFather → /newbot → copy the token (123456789:AA...)\n\nPaste it here:"),
    "token_ok": "✅ Found @{username}.",
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
    "verify_ok": "✅ Ownership verified.",
    "verify_bad": "❌ We didn't receive the code. Send it again to your bot and retry.",
    "verify_no_chat": ("⚠️ We haven't seen anything from @{username} yet.\n\n"
                       "Most likely you never pressed Start on it — open the bot in "
                       "Telegram, tap Start (this activates the chat), then send this code:\n\n"
                       "  {code}\n\nThen tap \"I sent it\"."),
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
                 "⚠️ Higher leverage = faster liquidation. Paper only, but it simulates real perp risk."),
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
             "  📊 Dashboard, P&L, leaderboard inside your bot"),
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
    "welcome_push": "🔔 You'll get every trade here. Tap 📊 P&L to start.",
    "positions_header": "Open positions ({n})",
    "close_confirm": "Close {symbol} {side} {qty} now?",
    "closed_ok": "✅ Closed {symbol} at {price} ({pnl}, fee ${fee})",
    "pause_confirm": "Pause {name}? Positions stay open, no new decisions.",
    "paused": "⏸️ Paused. No new decisions. Positions stay open.",
    "resumed": "▶️ Bot resumed.",
    "delete_confirm": "Really delete {name}? This stops it and removes your keys from our servers.",
    "deleted": "🗑️ Deleted. Goodbye. Your agent history stays on the platform.",
    "settings_saved": "Saved ✓ ({value})",
}

NOTIF = {
    "fill": "✅ FILL: {action} {symbol} {qty} @ ${price} ({leverage_text}stop {stop}%, target {take}%)",
    "stop": "🛑 STOP: {symbol} closed {pnl} ({pct}% stop)",
    "target": "🎯 TARGET: {symbol} closed +{pnl} ({pct}% target)",
    "liq": "💥 LIQUIDATED: {symbol} {lev}x at ${price} — margin lost.",
    "started": "▶️ Bot started ({interval}s cycle)",
    "paused": "⏸️ Bot paused.",
    "error_first": "⚠️ {message} — retrying. No trade this cycle.",
    "error_batch": "⚠️ Still retrying ({n} issues).",
    "daily": "📅 Today: {pnl} · {trades} trades · win {win}% · fees ${fees}",
    "weekly": "📈 Week: {pnl} · {trades} trades · win {win}% · rank #{rank}",
    "milestone": "🚀 +{pct}% ({equity} equity)",
    "milestone_down": "⚠️ -{pct}% — consider pausing",
    "back_online": "✅ Back online.",
}

ERRORS = {
    "unknown_market": "I only understand buttons. Tap 🏠 Home.",
    "unauthorized": "⛔ Unauthorized.",
    "platform_down": "⚠️ Our platform is down — try again in a few minutes.",
    "duplicate_token": "This bot is already registered.",
    "duplicate_key": "That key is already powering another bot.",
    "name_taken": "That name is taken — try {suggestion}.",
}


def mask_key(k: str) -> str:
    from key_vault import KeyVault

    return KeyVault.mask(k)