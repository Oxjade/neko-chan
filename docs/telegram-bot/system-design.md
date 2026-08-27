# AI-Trader Master Telegram Bot — System Design

Version: 1.0 (design only — no implementation yet)
Status: DRAFT for review before build

---

## 1. Goal

A Telegram **master bot** that lets any user spin up their own AI trading bot in
under 2 minutes:

> User opens our Telegram bot → onboards → submits **their own AI API key**
> (OpenAI-compatible, e.g. OpenAI / OpenRouter / opencode-go gateway key) →
> we validate the key, register an agent for them on the AI-Trader paper
> platform, add their bot to the master list, and run their agent with **their
> key paying for their model calls**.

The user then watches live P&L, positions, leaderboard rank, and trade
notifications — all through Telegram buttons. No coding, no terminal.

**Key constraint (from owner):** the submitted key is the *user's* API key that
enables *their* AI trading. It has nothing to do with the operator's own opencode
instance. The operator runs the infrastructure; users pay for their own LLM
calls through their submitted key.

---

## 2. Requirements

### 2.1 Functional

| ID | Requirement |
|----|-------------|
| F1 | `/start` onboarding: pitch, how it works, risk warning, path to get an API key |
| F2 | Initialize wizard (multi-step conversation): bot name → provider → API key → risk profile → markets → start |
| F3 | Key validation before acceptance (test chat-completion call with 5-token budget) |
| F4 | Auto-register agent on AI-Trader platform (POST /api/claw/agents/selfRegister) |
| F5 | Add user's bot to the master bot list (registry) |
| F6 | Per-user agent runner: decision loop using the user's key + their config (symbols, cadence, risk caps, mode) |
| F7 | Dashboard: P&L, return %, positions, leaderboard rank, bot status |
| F8 | Bot management: start / stop / edit settings / change API key / delete |
| F9 | Push notifications: trade fills, stop-loss/take-profit hits, liquidation, errors, daily summary |
| F10 | Help/FAQ with step-by-step "how to get an API key" guide |
| F11 | Master list view: all registered bots ranked (name, model, P&L, status) |

### 2.2 Non-functional

| ID | Requirement |
|----|-------------|
| N1 | API keys **encrypted at rest**, never logged, never echoed (masked on screen) |
| N2 | Multi-tenant isolation: one user's key/config never visible to another |
| N3 | Idempotent wizard: restarting the flow at any step resumes safely, no duplicate agents |
| N4 | Graceful degradation: platform down → bot shows "system offline" state, no crash |
| N5 | Latency: decision loop interval 60–600s (user-selectable); Telegram actions < 2s for menus |
| N6 | Scale target: 100 bots, ≤ 20 concurrent decision cycles/min on one host |
| N7 | Cost: operator pays zero LLM inference cost (user keys); operator pays only Telegram + hosting |
| N8 | Every user-facing string clear, consistent tone, no jargon without explanation |

### 2.3 Constraints

- Python 3.13 (venv at `.venv/`), existing AI-Trader platform at `http://127.0.0.1:8000`
- Existing `service/agent/live_agent.py` runner reused per user (adapted for generic OpenAI-compatible provider + per-user key)
- Self-hosted, local deployment first (polling mode); webhook is a later option

---

## 3. High-Level Architecture

```
                          Telegram Bot API (long polling)
                                    │
        ┌───────────────────────────▼───────────────────────────┐
        │               MASTER TELEGRAM BOT (service/tg_bot)    │
        │                                                       │
        │  main.py            bot entry, polling loop           │
        │  handlers/          /start, wizard, menu, admin       │
        │  store.py           registry: users, keys, bots       │
        │  key_vault.py       Fernet-encrypted key storage      │
        │  provider.py        key validation + LLM call helper  │
        │  platform_client.py AI-Trader REST wrapper            │
        │  agent_pool.py      spawn/stop/health-check runners   │
        │  notifier.py        push trade/status messages        │
        └──────────────┬──────────────────────┬────────────────┘
                       │ REST (platform)      │ subprocess per active bot
        ┌──────────────▼───────────────┐  ┌───▼──────────────────────────┐
        │ AI-Trader Platform API :8000 │  │ Per-user Agent Runner         │
        │ (existing, unchanged)        │  │ (live_agent.py + user config  │
        │ agents/signals/positions/    │  │  + user's API key via provider)│
        │ leaderboard/price            │  └───────────────────────────────┘
        └──────────────────────────────┘
```

**Data flow (init):**
1. User taps **Initialize** → wizard collects name, provider, key, risk profile, markets
2. Bot validates key (`provider.py` test call) → on success persists encrypted key + creates agent via platform → stores bot row → spawns runner (if started)
3. User gets "welcome to the bot list" + dashboard

**Data flow (running bot, every `interval` sec):**
1. Runner fetches prices + 5m context (platform price API / yfinance / Hyperliquid)
2. Runner calls LLM (user's key, user's provider) for JSON decision
3. Runner applies risk guards (cap, stops, daily limit)
4. Runner executes via platform `/api/signals/realtime`
5. Runner logs decision + notifies Telegram on fills/errors/stops

---

## 4. Data Model (SQLite, `registry.db`)

### `users`
| col | type | notes |
|-----|------|-------|
| tg_id | INTEGER PK | Telegram user id |
| tg_username | TEXT | display name |
| agent_name | TEXT UNIQUE | platform agent name |
| platform_token | TEXT | agent token (platform auth) |
| status | TEXT | `onboarding` / `active` / `paused` / `disabled` |
| created_at | TEXT | ISO |
| accepted_disclaimer | INTEGER | risk disclaimer seen |

### `api_keys`
| col | type | notes |
|-----|------|-------|
| id | INTEGER PK | |
| tg_id | INTEGER FK | one active key per user |
| provider | TEXT | `openai` / `openrouter` / `opencode-go` / `custom` |
| base_url | TEXT | NULL for presets |
| encrypted_key | BLOB | Fernet |
| key_hash | TEXT | sha256 for duplicate detection |
| model | TEXT | default model for provider |
| validated_at | TEXT | |
| last_used_at | TEXT | |
| revoked_at | TEXT | NULL = active |

### `bots`
| col | type | notes |
|-----|------|-------|
| id | INTEGER PK | |
| tg_id | INTEGER FK | |
| bot_name | TEXT | display name |
| symbols | TEXT | JSON list `["BTC:crypto",...]` |
| interval_sec | INTEGER | 60–600 |
| risk_profile | TEXT | `conservative` / `balanced` / `aggressive` (preset JSON in `risk_caps`) |
| risk_caps | TEXT | JSON: max_daily_trades, max_position_pct, force_stop_pct, active_mode |
| is_running | INTEGER | runner subprocess alive? |
| pid | INTEGER | runner pid |
| last_heartbeat | TEXT | runner pings |
| last_error | TEXT | |
| created_at | TEXT | |

### `events` (notification ledger, dedup)
| col | type | notes |
|-----|------|-------|
| id | INTEGER PK | |
| tg_id | INTEGER | |
| kind | TEXT | `trade_fill` / `stop_hit` / `error` / `daily_summary` |
| payload | TEXT | JSON |
| sent_at | TEXT | |

### Risk presets
- **Conservative:** max_daily_trades=4, max_position_pct=20, force_stop=5, active_mode=0
- **Balanced:** max_daily_trades=8, max_position_pct=30, force_stop=5, active_mode=1
- **Aggressive:** max_daily_trades=16, max_position_pct=40, force_stop=3, active_mode=1

---

## 5. API Contracts (master bot ⇄ platform)

Reuse the existing platform API unchanged:

| Use | Endpoint |
|-----|----------|
| Create agent | `POST /api/claw/agents/selfRegister` `{name, password}` → `{token, agent_id}` |
| Agent auth | `Authorization: Bearer <platform_token>` |
| Positions + cash | `GET /api/positions` |
| Leaderboard | `GET /api/profit/history` (filter by agent_name) |
| Trade execution | `POST /api/signals/realtime` `{market, symbol, action, quantity, price:0, executed_at:"now", stop_loss_pct?, take_profit_pct?, leverage?}` |
| Live price | `GET /api/price?market=&symbol=` (rate-limited 1/sec/agent) |

### Key validation contract (`provider.py`)
```
POST {base_url}/chat/completions
Authorization: Bearer {key}
{"model": "{model}", "messages":[{"role":"user","content":"say OK"}], "max_tokens": 5}
```
- HTTP 2xx + non-empty content → **VALID**
- 401/403 → invalid key; 429 → rate-limited (ask to retry); 5xx → provider issue (retry once)
- Presets: openai→`https://api.openai.com/v1` model `gpt-4o-mini`;
  openrouter→`https://openrouter.ai/api/v1` model `openrouter/auto`;
  opencode-go→operator-configured gateway base+model (validated by the operator at deploy);
  custom→user-provided base_url + model.

---

## 6. UI Specification — every screen, every button

### 6.1 `/start` (also `/menu`)
```
🤖 AI-Trader Telegram Bot

Run YOUR OWN AI trading bot — no code needed.
Your bot reads live markets (BTC, ETH, US stocks, Forex),
decides with AI, and trades on the paper platform with real prices.

⚠️ 100% PAPER TRADING. No real money. For evaluation only.

What would you like to do?

[ 🚀 Initialize My Bot ]   [ 📖 How It Works ]
[ 🏆 Leaderboard ]         [ ❓ Help ]
```

### 6.2 How It Works (paginated, 3 pages, [←] [→] [🏠 Main Menu])
- Page 1: what AI-Trader is + real prices, paper money
- Page 2: what YOUR bot does (5-min decisions, stops, caps)
- Page 3: risks + disclaimer → `[I understand]` required before init

### 6.3 Initialize wizard (ConversationHandler states)

| State | Prompt | Input |
|-------|--------|-------|
| NAME | "Name your bot (e.g. BitcoinWhale)" | text, validate 3–24 chars |
| PROVIDER | "Which AI provider?" `[OpenAI] [OpenRouter] [opencode-go] [Custom]` | button |
| KEY | "Paste your API key (sk-...)" — masked on reply | text, strip, `sk-` sanity check |
| KEY_DONE | validating animation `⏳ Checking your key...` → success ✅ / failure ❌ (re-try button) | auto |
| RISK | "Risk profile?" `[🛡️ Conservative] [⚖️ Balanced] [🚀 Aggressive]` + explainer line | button |
| MARKETS | "Which markets?" multi-select checkboxes `[BTC/ETH] [US Stocks] [Forex]` + `[✅ Done]` | buttons, toggles |
| START | "Start trading now?" `[▶️ Start Now] [⏸️ Not Yet]` | button |
| DONE | summary card + `[📊 Dashboard]` | auto |

- **Idempotence:** if a user re-runs `/start` mid-wizard, `entry_point` resumes at current state; a re-init after completion asks `[⚠️ This replaces your existing bot. Continue?]`
- **Cancellation:** every wizard step has `[❌ Cancel]` → returns to main menu, discards partial input.

### 6.4 Main dashboard (after init)
```
📊 Your bot: BitcoinWhale
Status: 🟢 RUNNING | Agent: BitcoinWhale
P&L: +$1,234.56 (+1.23%)  |  Rank: #3
Open positions: 1 (BTC long 0.1 @ $78,600)
Cash: $98,765.43

[ 📊 P&L Detail ]  [ 💰 Positions ]
[ 🏆 Leaderboard ]  [ 🤖 Bot Controls ]
[ 📡 Recent Signals ]  [ ❓ Help ]
```

### 6.5 Bot Controls menu
```
🤖 BitcoinWhale
🟢 Running (heartbeat 30s ago) | interval 120s | profile: Balanced
Symbols: BTC, ETH, AAPL, EURUSD
Last error: none

[ ▶️ Start ] [ ⏸️ Stop ] [ ⚙️ Settings ]
[ 🔑 Change API Key ] [ 🗑️ Delete Bot ] [ ↩️ Back ]
```
- **Stop** → confirms `[✅ Stop it] [↩️ Cancel]`; kills runner, marks paused.
- **Delete** → double confirm, revokes key, stops runner, marks disabled. *Platform agent record is retained (platform has no delete); bot row is soft-deleted.*
- **Settings** → `[🎯 Symbols] [⏱ Interval] [🛡 Risk Profile] [🧠 Trading Mode] [↩️ Back]` — each opens a small picker of options (interval: 1m/2m/5m/10m; mode: active scalper / conservative).

### 6.6 Leaderboard
```
🏆 Bot Leaderboard (live)
1. RiskAgent       +40.9%   #my bot 🔵
2. HindsightAgent  +1.38%
3. MyBot           +1.23%  ← you
...
[ ↻ Refresh ] [ 📊 My Dashboard ]
```
Top 10 + the user's row highlighted.

### 6.7 Notifications (pushed)
- `✅ FILL: BTC buy 0.1 @ $78,600 (stop 5%)`
- `🛑 STOP HIT: EURUSD sold at stop -$83.06`
- `⚠️ BOT ERROR: price fetch failed (will retry in 5m)`
- `📅 Daily summary: +$12.34, 3 trades, win rate 33%`

---

## 7. Security

| Threat | Mitigation |
|--------|-----------|
| Key theft at rest | Fernet (AES-128-CBC) encryption; `MASTER_KEY` env var (never committed); SQLite file mode 0600 |
| Key echo in chat | Only masked `sk-•••ab12` ever displayed; Telegram edit/delete of the message containing the paste |
| Key in logs | `logging` filters any 40+ char sk- token |
| Cross-tenant access | All queries keyed by `tg_id`; agent name collision → suffix `_2` |
| Duplicate keys | `key_hash` unique index; reuse blocked with "key already used by another bot" |
| Abuse / spam | per-user rate limit (max N init attempts/day), admin ban list |
| Runner isolation | one subprocess per bot, killed on stop/delete; no shared secrets in argv (keys passed via env or file with 0600) |
| LLM prompt injection | decisions constrained to strict JSON schema; risk guards are **client-side enforced regardless of model output** |

---

## 8. Error Handling & Retry

- Key validation: 401 → "key rejected by provider"; 429 → "provider rate-limited, try in 60s"; network → retry 2× exponential.
- Platform down: all platform calls wrapped; UI shows `⚠️ platform offline, retrying…`; runner pauses and retries with backoff, keeps last state.
- Runner crash: `agent_pool` health monitor restarts up to 3×/hour, then marks bot `error` and notifies user.
- LLM malformed response: retry once with "reply with valid JSON only"; after 2 failures → skip cycle (log `parse-failed`), never fabricate a trade.

## 9. Trade-offs (explicit)

| Decision | Chosen | Trade-off |
|----------|--------|-----------|
| Polling vs webhook | Polling (v1) | No public URL needed, simpler local deploy; adds ~0.5–2s latency, fine at our scale. Webhook later for production |
| Per-user runner | Subprocess per active bot | Isolation + crash-safety; heavier than threads (~40MB each). At 100 bots ≈ 4GB — acceptable on dev host; revisit at >200 bots with a worker pool |
| Key storage | Fernet + local SQLite | Simple, adequate for self-host; production path = KMS + Postgres |
| Registry DB | SQLite | Single-writer, fine < 1k bots; Postgres when multi-instance |
| Provider presets | 4 presets + custom URL | Covers the common cases; custom covers everything else |
| Reuse platform | Zero platform changes | Faster, lower risk; platform's own guards (time-travel, market hours, stops) already in place |

## 10. Scale estimate (v1)

- 100 users × ~15 decisions/day of active cycles → ~1,500 LLM calls/day paid by users
- Bot responses: < 200 msg/min peaks; Telegram free tier OK
- Storage: KBs per user; no retention concerns for years

## 11. What to revisit as it grows

1. Webhook deployment (public URL, TLS, setWebhook)
2. Postgres + KMS-managed keys
3. Worker pool for agent cycles (no per-bot subprocess)
4. Billing/usage dashboard per user (token counts per key)
5. Team mode: shared bots with role-based access
6. Real-money integration (broker API) behind a hard "not financial advice" gate

## 12. Build plan summary

See `PLAN.md` for the depth tree. Implementation order:
1. `key_vault.py` + `provider.py` (security-critical, standalone, testable)
2. `store.py` (registry) + `platform_client.py`
3. Wizard handlers + main menu (the full UX)
4. `agent_pool.py` + per-user runner adapter
5. Notifications + leaderboard/signals views
6. Admin commands + docs