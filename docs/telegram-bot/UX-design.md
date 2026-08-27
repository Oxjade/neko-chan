# AI-Trader Telegram — Complete UX Specification

Version: 2.0 (design only)
Covers: Master bot (onboarding) + User bot (dashboard) + Push notifications.
Every screen, every button, every back path, every error state.

---

## 0. Navigation Laws (apply to EVERY screen, both bots)

1. **No dead ends.** Every screen has exactly one `[🏠 Home]` and at most one `[↩️ Back]`. Home is reachable from anywhere in ≤ 2 taps.
2. **Cancel is always available** in every wizard step, and canceling discards nothing unexpected (partial data is kept and shown on re-entry).
3. **Primary action placement** is consistent: primary button bottom-left, secondary middle, danger/back bottom-right.
4. **Every button does something visible** — no silent taps; every action returns a confirmation message (`Saved ✓`, `Stopped`, `Error: ...`).
5. **No text-only replies.** Every bot response is a message + an inline keyboard (except typing prompts in the wizard, which always include `[❌ Cancel]`).
6. **Errors always have 2 exits**: `[↻ Retry]` and `[🏠 Home]`, never a dead error text.
7. **Message replacement**: stateful screens are **edited in place** (no stacking spam); list screens append or refresh.
8. **Rate discipline**: identical repeated taps are coalesced; duplicate commands within 2s return the existing screen without re-render.

---

## 1. MASTER BOT — full spec

### 1.1 `/start` (entry, also `/menu`)
```
🤖 AI-Trader Bot Network

Run your own AI trading bot on a paper platform with real prices.
You bring two keys — we do the rest:
  1️⃣ A Telegram bot token (from @BotFather)  → your channel
  2️⃣ An AI API key                            → your bot's brain

⚠️ Paper trading only. No real money. Not financial advice.

[ 🚀 Add My Bot ]   [ 📖 How It Works ]
[ 🏆 Leaderboard ]  [ ❓ Help ]
```
- New user → same screen + first-run extra line: `👋 Welcome! 2 minutes to your own bot.`
- Returning user → same screen + status line: `🤖 You have 1 bot (BitcoinWhale 🟢). [🤖 My Bots]` — button added to the menu.
- `/start` is **idempotent**: never restarts a wizard, never creates anything.

### 1.2 How It Works (3 pages, pagination)
```
Page 1/3 — What is this?
  Real market prices (BTC, ETH, US stocks, Forex) · paper money ($100k).
  Your bot reads markets, decides with AI, trades on the platform.
  [1/3 → Next]  [🏠 Home]

Page 2/3 — What YOUR bot does
  • Decides every 1–10 min (you pick)
  • Always has a stop-loss · position caps · daily trade limit
  • Trades are recorded and scored on the leaderboard
  [← Back]  [2/3 → Next]  [🏠 Home]

Page 3/3 — Before you start
  ⚠️ Paper only. No real money. Models make mistakes.
  Your AI key pays for your own model calls. You can delete your bot anytime.
  [← Back]  [✅ I Understand]  [🏠 Home]
```
- `✅ I Understand` → jumps straight into the Add My Bot wizard.
- `[🏠 Home]` works from any page.

### 1.3 Add My Bot wizard (7 states)

**Global rules**: every step has `[❌ Cancel]`. Cancel → `Wizard canceled. Nothing was saved.` + main menu. Re-entering the wizard after cancel resumes the LAST completed state (no re-typing). 5 minutes idle → `⏳ Wizard timed out. Resume with /start → Add My Bot.`

#### State 1 — BOT_NAME
```
🤖 Name your bot (3–24 chars, letters/numbers/space)
Example: BitcoinWhale
```
`[❌ Cancel]`
- Validation: empty → `Please send a name.`; wrong chars → `Only letters, numbers and spaces.`; taken name → `That name is taken — try BitcoinWhale2`.
- ✅ → save draft, → State 2.

#### State 2 — TG_TOKEN
```
1️⃣ Send your Telegram bot token — create one first:
  → open @BotFather → /newbot → copy the token (123456789:AA...)

Paste it here:
```
`[❌ Cancel]`
- **Validate**: `getMe(token)`
  - ✅ → `✅ Found @username.` → State 3.
  - ❌ invalid → `❌ That token didn't work. Check @BotFather — it looks like 123456789:AAExample...` `[↻ Retry] [❌ Cancel]`
  - ⏳ network → `⏳ Can't reach Telegram right now.` `[↻ Retry] [❌ Cancel]`
- **Duplicate check**: token already registered → `This bot is already registered.` `[🤖 My Bots] [🏠 Home]`

#### State 3 — OWNERSHIP_VERIFY
```
2️⃣ Prove it's yours — send this code TO your bot @username:

  VERIFY-3847

Type it in a chat with your own bot, then tap "I sent it".
```
`[✅ I sent it] [🔄 Resend code] [❌ Cancel]`
- We poll `getUpdates(your_token)` for 90s for the exact code.
  - ✅ seen → `✅ Ownership verified.` → State 4.
  - ❌ timeout → `❌ We didn't receive the code. Send it again to your bot and retry.` `[↻ Retry] [🔄 New code] [❌ Cancel]`
  - ⚠️ token now invalid (bot deleted in BotFather) → `⚠️ Your bot seems deleted. Re-create it in BotFather.` `[↻ Retry] [❌ Cancel]`

#### State 4 — AI_KEY
```
3️⃣ Your AI API key — this powers your bot's decisions.
Pick a provider:
[ OpenAI ] [ OpenRouter ] [ opencode-go ] [ Custom URL ]
```
`[❌ Cancel]`
- OpenAI → `Paste your sk-... key:`
- OpenRouter → `Paste your sk-or-... key:`
- opencode-go → `Paste your gateway key:`
- Custom → `Send: base URL` → `model name` → `key` (3 sub-prompts, each cancellable)
- **Pasted key handling**: message deleted after 10s, reply echoes `Key received: sk-•••4821 ✓`
- **Validate**: 5-token chat call to provider.
  - ✅ 2xx → `✅ Key works (OpenAI).` → State 5.
  - ❌ 401 → `❌ Provider rejected this key. Double-check it.` `[↻ Retry] [❌ Cancel]`
  - ⏳ 429 → `⏳ Provider is rate-limited — wait a minute.` `[⏳ Try again in 60s] [❌ Cancel]`
  - ❌ network → `⏳ Can't reach provider.` `[↻ Retry] [❌ Cancel]`
- **Duplicate**: same key hash already active → `That key is already powering another bot.` `[🔄 Use another] [🏠 Home]`

#### State 5 — RISK_PROFILE
```
4️⃣ Risk profile
[ 🛡️ Conservative ]  ⚠️ few trades, tight stops, cash-preferred
[ ⚖️ Balanced ]       usual defaults, active mode
[ 🚀 Aggressive ]     max trades, wider size
```
`[❌ Cancel]`
- Each button: `Selected: 🛡️ Conservative` + summary card + `[✅ Confirm] [↩️ Change] [❌ Cancel]`

#### State 6 — MARKETS
```
5️⃣ Markets (toggle, ✅ = on)
[ ✅ ⚡ Perps ]   BTC/ETH with leverage 1–10x (margin, liquidation, funding)
[ ⬜ ₿ Spot ]     BTC/ETH regular (no leverage)
[ ⬜ 📈 US Stocks ]  AAPL, NVDA, SPY…
[ ⬜ 💱 Forex ]   EURUSD, USDJPY…
[ ✅ Done ]
```
`[❌ Cancel]`
- At least one required → if none: `Pick at least one market.`
- `⚡ Perps` selected → **Leverage step** (State 6b):
```
Leverage for Perps (1–10x)
[ 1x ] [ 2x ] [ 5x ] [ 10x ]
⚠️ Higher leverage = faster liquidation. Paper only, but it simulates real perp risk.
[ ✅ Confirm ] [ ↩️ Change ]
```
- `✅ Done` → State 7.

#### State 7 — START
```
Everything is set 🎉
  Bot:     BitcoinWhale
  Channel: @username (verified)
  AI:      OpenAI · sk-•••4821
  Risk:    ⚖️ Balanced
  Interval: 2m
  Markets: ⚡ Perps (5x), 💱 Forex
  [note: ⚠️ liquidation simulated for perps — leverage cuts both ways]

[ ▶️ Start Now ]  [ ⏸️ Later ]
```
`[❌ Cancel]` (also shown)
- `▶️ Start Now` → register agent on platform → save all → spawn user bot → DONE.
- `⏸️ Later` → save all (bot paused) → DONE (different final line).

#### State 8 — DONE
```
✅ BitcoinWhale is registered!
Your bot is live → open @username and press Start.

What happens next:
  🔔 Every trade/stop will be pushed to your bot
  📊 Dashboard, P&L, leaderboard inside your bot

[ 🤖 My Bots ]  [ 🏠 Home ]
```

### 1.4 🤖 My Bots (registry view for the user)
```
Your bots:
🟢 BitcoinWhale  +$1,234.56  (rank #3)   [ 👁 View ]
⏸️ TestBot       paused                 [ 👁 View ]
[ ➕ Add Another Bot ]  [ 🏠 Home ]
```
- `👁 View` → **redirects to their own bot** + info card:
```
BitcoinWhale — all controls live in @username
Last heartbeat: 32s ago · interval 120s · profile Balanced
[ 🔗 Open @username ]  [ 🗑️ Remove from network ]  [ ↩️ Back ]
```
- `🗑️ Remove` → confirm → `Removed. Your agent history stays on the platform.` — deletes registry rows (encrypted keys wiped), stops worker.

### 1.5 🏆 Leaderboard (master mirror)
```
🏆 Bot Network Leaderboard (live)
 1. RiskAgent          +40.9%
 2. HindsightAgent     +1.38%
 3. BitcoinWhale ← you  +1.23%
 ...
[ ↻ Refresh ]  [ 📊 My Bots ]  [ 🏠 Home ]
```

### 1.6 ❓ Help
```
Help center
[ ❓ How do I get a bot token? ]  → step-by-step BotFather guide + [↩️ Back]
[ 🔑 What if my AI key is rejected? ]  → troubleshooting
[ 🛡️ What is paper trading? ]  → one-liner
[ ⚠️ I lost my bot in BotFather ]  → what to do
[ 💬 Contact support ]  → @support handle
[ 🏠 Home ]
```

### 1.7 `/admin` (operator only, env allowlist of tg_ids)
```
👑 Fleet — 42 bots · 17 running · 3 errors
🟢 BitcoinWhale  +$1,234   @username  agent:BV-1  [⏸] [🚫]
🔴 BrokenBot     error     @badbot     [🔁] [🚫]
[ ↻ Refresh ]  [ 🏠 Home ]
```
- `⏸` force-stop worker · `🔁` restart · `🚫` ban (blocks user id globally).
- Ban reason message: `This bot has been suspended. Contact support.`

---

## 2. USER BOT — full spec

Their bot = everything they live in. Every screen below is reachable from the dashboard; every screen has `[🏠 Home]`; secondary screens have `[↩️ Back]` to the previous screen.

### 2.1 `/start` → DASHBOARD
```
📊 BitcoinWhale
🟢 RUNNING · last decision 32s ago
P&L      +$1,234.56   (+1.23%)    rank #3
Cash     $98,765.43
Open     1 position

[ 📊 P&L ]  [ 💰 Positions ]  [ 🏆 Leaderboard ]
[ 📡 Trades ]  [ 🏦 Live Markets ]  [ 🤖 Bot ]
[ ⚙️ Settings ]  [ 📬 Inbox ]  [ ❓ Help ]
```
First-ever `/start` (from our pushed welcome) shows the same screen with one extra bubble: `🔔 You'll get every trade here. Tap 📊 P&L to start.`

### 2.1b 🏦 Live Markets (live data view)
```
🏦 Live Markets  (live · refreshed 3s ago)
⚡ BTC   $78,830  24h +1.2%  7d +13.7%   5m ▲▲▲▼▲ (1h +0.4%)
⚡ ETH   $2,504   24h +2.2%  7d +11.1%   5m ▲▼▲▲▲ (1h +0.8%)
📈 AAPL  $313.74  24h +0.3%  7d -1.4%    5m ▼▼·▲·  (1h -0.2%)
💱 EURUSD 1.1659  24h +0.1%  7d +0.4%    5m ···▲··  (1h +0.0%)

[ ⏱ Auto 30s ]  [ ↻ Now ]  [ ↩️ Back ]  [ 🏠 Home ]
```
- **Live data pipeline**: prices come from the AI-Trader platform (`/api/price` → Hyperliquid / yfinance / forex feeds); 5m trend candles from the runner's context (Hyperliquid 5m / yfinance 5m). All numbers are real-time market data.
- `[⏱ Auto 30s]` toggles auto-refresh (message edits in place every 30s); `[↻ Now]` forces a refresh.
- Symbols shown = the bot's configured universe only.
- Non-crypto rows show `closed` when their market is closed (US stocks after hours, forex weekend), not stale prices.

### 2.2 📊 P&L detail
```
P&L detail — BitcoinWhale
Total        +$1,234.56  (+1.23%)
Today        +$42.10
Max drawdown  -8.2%
Win rate      45% (20 trades)
Fees paid     $18.40
Equity: ▁▂▃▅▆▅▆▇▇▆▇█▇ (30d)

[ ↩️ Back ]  [ 🏠 Home ]
```
`[↻ Refresh]` lives as a reply action (tap message → menu) — no extra button.

### 2.3 💰 Positions
```
Open positions (2)
⚡ BTC   LONG   0.10   entry 78,600   now 78,900   +$30.00
     5x · liq ≈ 63,300 · stop 74,670 · target 84,288 · opened 2h ago
💱 EURUSD LONG   5,000  entry 1.1659  now 1.1660   +$0.50
     stop 1.1426 · target 1.2125 · opened 3h ago

[ ⏱ Auto 30s ]  [ ↻ Now ]  [ 🔒 Close BTC ]  [ ↩️ Back ]  [ 🏠 Home ]
```
- Live marks via platform positions + price API; auto-refresh toggle edits in place.
- Perp rows show leverage and liquidation level (⚠️ highlighted when within 5%).
- `🔒 Close` → `Close BTC long 0.1 now?` `[✅ Close] [↩️ Cancel]` → `✅ Closed at 78,900 (+$30.00, fee $7.89)`.
- >5 positions → first 5 + `…and N more`.

### 2.4 🏆 Leaderboard
```
🏆 Leaderboard (live)
 1. RiskAgent        +40.9%
 2. HindsightAgent   +1.38%
 3. BitcoinWhale     +1.23%  ← you
 ...
[ ↩️ Back ]  [ 🏠 Home ]
```

### 2.5 📡 Trades (recent decisions)
```
Recent decisions
22:41 ✅ BUY  BTC 0.10 @ 78,600 (stop 5%, target 7%)
      "5m trend flipped up after pullback — entering."
22:35 💤 HOLD — no trade
      "Range-bound, no edge. Staying out."
22:29 🛑 STOP EURUSD -$83.06
      "Hit 2% stop."

[ ↩️ Back ]  [ 🏠 Home ]
```
- Filters via reply menu: `All / Fills / Stops / Holds`.

### 2.6 🤖 Bot (status + controls)
```
BitcoinWhale · agent status
🟢 RUNNING · heartbeat 32s ago
Profile:   ⚖️ Balanced
Interval:  2m
Symbols:   BTC, ETH, AAPL, EURUSD
Trades today: 3/8
Last error: none

[ ⏸️ Pause ]  [ ▶️ Resume ]  [ 🔄 Restart ]
[ 🗑️ Delete Bot ]  [ ↩️ Back ]  [ 🏠 Home ]
```
- `⏸️ Pause` → confirm `[✅ Pause] [↩️ Cancel]` → `⏸️ Paused. No new decisions. Positions stay open.` — button pair flips to `[▶️ Resume]`.
- `🔄 Restart` → `Restarting…` → `✅ Restarted (0:05s ago)`.
- `🗑️ Delete Bot` → **double confirm**: `Really delete BitcoinWhale? This stops it and removes your keys from our servers.` `[✅ Yes, delete] [↩️ Keep it]` → `🗑️ Deleted. Goodbye. Your agent history stays on the platform.`

### 2.7 ⚙️ Settings — every picker
```
Settings
[ 🎯 Symbols ]      [ ⏱ Interval ]
[ 🛡️ Risk Profile ]  [ 🧠 Mode ]  [ ⚖️ Leverage ]
[ 🔑 Change AI Key ]  [ 🔗 Manage on Master ]
[ ↩️ Back ]  [ 🏠 Home ]
```
Each picker pattern: current value shown, options as buttons, tap = set, reply `Saved ✓ (value)` + same screen with new value marked.

```
🎯 Symbols   (currently: ⚡ Perps, 💱 Forex)
[ ✅ ⚡ Perps ]   [ ⬜ ₿ Spot ]   [ ⬜ 📈 US Stocks ]   [ ✅ 💱 Forex ]
[ ✅ Done ]  [ ↩️ Back ]

⚖️ Leverage   (currently: 5x, applies to Perps)
[ 1x ] [ 2x ] [ 5x ] [ 10x ]
⚠️ liquidity/liquidation warning line
[ ↩️ Back ]

⏱ Interval   (currently: 2m)
[ 1m ] [ 2m ] [ 5m ] [ 10m ]
[ ↩️ Back ]

🛡️ Risk Profile  (currently: ⚖️ Balanced)
[ 🛡️ Conservative ]  [ ⚖️ Balanced ]  [ 🚀 Aggressive ]
+ explainer line per profile
[ ↩️ Back ]

🧠 Mode  (currently: Active scalper)
[ ⚡ Active scalper ]  [ 🧘 Conservative ]
[ ↩️ Back ]

🔑 Change AI Key
Paste your new key:  → validate → `✅ Key updated (sk-•••9302)`
[ ↩️ Back ]

🔗 Manage on Master
Open the master bot to change ownership/delete the network entry.
[ 🔗 Open Master Bot ]  [ ↩️ Back ]
```

### 2.8 📬 Inbox (notification history)
```
📬 Inbox — today
22:41 ✅ FILL BUY BTC 0.10 @ 78,600 [🤖 Bot]
22:29 🛑 STOP EURUSD -$83.06        [📊 P&L]
22:00 📅 Daily: +$12.34 · 3 trades · win 33%
Filters: [ All ] [ Fills ] [ Stops ] [ Errors ] [ Summaries ]
[ ↩️ Back ]  [ 🏠 Home ]
```

### 2.9 ❓ Help (user bot)
```
Help
[ ❓ How it works ]  [ 🛡️ Paper trading? ]
[ ⚠️ Why am I losing? ]  [ 🔑 Key problems ]
[ 💬 Support ]  [ ↩️ Back ]  [ 🏠 Home ]
```

---

## 3. PUSH NOTIFICATIONS — full event matrix

Delivery: instant `sendMessage` to the user's own bot; dedup ledger keyed `(tg_id, kind, ref_id)`; delivery failure (bot blocked/deleted) → queue summary only, no crash.

| Event | Priority | Message template | Attached buttons |
|---|---|---|---|
| Fill (buy/sell/short/cover) | high | `✅ FILL: BUY BTC 0.10 @ $78,600 (5x, stop 5%, target 7%)` | `[💰 Positions] [⏸ Pause]` |
| Stop-loss hit | high | `🛑 STOP: EURUSD closed -$83.06 (2% stop)` | `[📊 P&L] [🤖 Bot]` |
| Take-profit hit | high | `🎯 TARGET: BTC closed +$412.50 (7% target)` | `[📊 P&L] [🤖 Bot]` |
| Liquidation (if leveraged) | critical | `💥 LIQUIDATED: BTC 5x at $79,695 — margin lost.` | `[🤖 Bot] [❓ Help]` |
| Bot started/stopped | medium | `▶️ Bot started (120s cycle)` / `⏸️ Bot paused` | `[🤖 Bot]` |
| Error (first) | medium | `⚠️ Price feed failed — retrying. No trade this cycle.` | `[🔁 Retry Now] [⏸️ Pause]` |
| Error (repeated) | low | `⚠️ Still retrying (3 issues).` — replaces first, no spam | `[🤖 Bot]` |
| Daily summary 20:00 | low | `📅 Today: +$12.34 · 3 trades · win 33% · fees $4.12` | `[📊 P&L]` |
| Weekly report Sun 18:00 | low | `📈 Week: +$85.10 · 18 trades · win 44% · rank #3` | `[🏆 Leaderboard]` |
| Milestone ±10% | low | `🚀 +10% ($110k equity)` / `⚠️ -10% — consider pausing` | `[📊 P&L]` |

**Quiet hours** (`⚙️ Settings → 🌙 Quiet hours`): 22:00–08:00 all events silent except stops/liquidation; queued items delivered as one morning digest.
**Per-kind toggles**: fills / summaries / milestones can be muted independently; stops and liquidation are always on.

---

## 4. FLOW DIAGRAMS

### 4.1 New user, happy path (master)
```
/start → [Add My Bot] → name → token (validated) → ownership code (verified)
→ AI key (validated) → risk → markets → [Start Now]
→ agent registered on platform → user bot worker spawned
→ "Open @username" → user opens their bot → dashboard + first push: "You'll get every trade here 🔔"
```

### 4.2 Cancel anywhere
```
State N → [❌ Cancel] → "Wizard canceled. Nothing was saved." → main menu
Re-enter → resumes at State N (draft kept), token/key NOT re-asked unless invalid.
```

### 4.3 Key failure loop
```
AI key 401 → [↻ Retry] → paste again → 401 again → "Double-check the key." → [🔄 New key] [❌ Cancel]
```

### 4.4 Bot deleted in BotFather (mid-life)
```
Worker sendMessage fails 401 → notify master-side → user sees on master:
"⚠️ @username is gone — BotFather deleted it or the token changed."
[ 🔗 Re-verify token ] → re-runs State 2–3 only → worker resumes
```

### 4.5 User bot fully deleted by user
```
[🗑️ Delete Bot] → confirm → confirm → worker stopped, keys wiped
→ master shows bot removed from network → platform agent remains (history kept)
```

---

## 5. EDGE-CASE CATALOG (must all behave)

| # | Case | Behavior |
|---|---|---|
| E1 | Duplicate /start | returns current screen, idempotent |
| E2 | Random text outside wizard | `I only understand buttons. Tap [🏠 Home].` |
| E3 | Token with spaces/typos | trimmed, error + example shown |
| E4 | Same token twice | blocked at registry (duplicate) |
| E5 | Same AI key twice | blocked (key hash) |
| E6 | Provider rate limit | 60s cooldown with timer text |
| E7 | Platform offline during init | retry 3×, then `⚠️ Our platform is down — try again in a few minutes.` `[↻ Retry]` |
| E8 | Platform offline while running | runner pauses with backoff; user gets one error push, then silence until recovery push `✅ Back online.` |
| E9 | User blocks their own bot | pushes fail silently; daily summary retries; master shows `⚠️ unresponsive` |
| E10 | Agent name collision | auto-suffix `_2`, shown in summary |
| E11 | Wizard idle 5 min | timeout message, draft kept |
| E12 | Edited message instead of new | ignored, same screen re-echoed |
| E13 | Emoji/HTML injection in bot name | sanitized (letters/numbers/space only) |
| E14 | Very long pasted key | max length check, masked |
| E15 | Multiple bots per user | allowed (one token per bot), each listed in My Bots |
| E16 | Stop while a decision is mid-flight | runner graceful: current cycle completes or aborts safely, then stops |
| E17 | Currency of leaderboard | always live, `[↻ Refresh]` 60s cache |
| E18 | User taps a notification button twice | idempotent handlers, no double actions |
| E19 | Day rollover of trade caps | counter resets at 00:00 UTC |
| E20 | /admin from non-admin | `⛔ Unauthorized.` |
| E21 | Live Markets outside market hours | US stocks/forex rows show `closed` badge with next-open time, never stale prices |
| E22 | Price provider down (Live Markets) | row shows `unavailable` + `[↻ Retry]` on the screen; auto-refresh skips the row |
| E23 | User enables Perps without setting leverage | defaults 1x (spot-like), leverage picker shows a hint dot |
| E24 | Leverage changed while a perp position is open | change applies to **new** entries only; open position keeps its original leverage (shown) |

---

## 6. COPY & TONE RULES

- Active voice, ≤ 20 words per sentence, one idea per bubble.
- No jargon without a one-line explanation on first use.
- Money always formatted: `+$1,234.56` (sign always present), percents `(+1.23%)`.
- States: `🟢 RUNNING / ⏸️ PAUSED / 🔴 ERROR / 🟡 STARTING`.
- Errors name the fix, not just the problem: `❌ Token invalid — copy the full token from @BotFather, it starts with numbers:`.
- Emojis: max 1 per line, functional (fill=✅, stop=🛑, profit=🎯/🚀, warning=⚠️).