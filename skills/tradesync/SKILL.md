---
name: ai-trader-tradesync
description: Sync your trading positions and trade records to AI-Trader copy trading platform. Includes idempotency, retry rules, and the synchronization contract for watchers of published signals.
---

# AI-Trader Trade Sync Skill

Share your trading signals with followers. Upload positions, trade history, and sync real-time trading operations.

> **Base URL:** all `curl`/`requests` target the **local Neko platform**
> at `$AI_TRADER_URL` (env var, default `http://127.0.0.1:8000`).
>
> **Forge note (2026-08-27):** the predecessor described the happy path only.
> Publishing is a *distributed* operation — the follower may read your signal
> before, during, or after your state change, and your own sync may retry.
> This version adds the contract that makes published signals *consistent and
> canonical*: idempotent writes, unambiguous types, ordering, and the "DB is
> source of truth" principle.
>
> **PRODUCTION REALITY (2026-09-01):** the Neko bot's user trades execute
> through the **execution gateway → Aftermath Perps** (mainnet/testnet), NOT
> the platform's simulated fills. The sync endpoints below publish the
> platform's signal/copy layer. For a real bot's positions the source of
> truth is the **execution ledger** (`exec_ledger.db`, tables
> `exec_orders`/`exec_fills`/`exec_positions`) synced from Aftermath via the
> gateway; the ledger + venue state, not `/api/signals/realtime`, reflect real
> PnL. When the gateway is not ready the agent holds (paper mode removed).

---

## Sync Contract (the invariant)

1. **The local DB is the source of truth,** not the sync feed. The published
   `realtime`/`position`/`trade` events are projections.
2. **Every write is idempotent.** A retried publish must not double-count, double
   reward, or double-copy. Send a client-generated `publish_id` (or use the
   `signal_id` you got back and don't re-publish the same operation twice).
3. **States, not opinions.** Publish what the account *did*, with a price and a
   timestamp. Never publish "I think BTC is a buy" as a `position` event.
4. **Repair, don't append.** If a previous publish failed or was lost, you repair
   by publishing the *current* state (a corrected `position` event), not by
   appending a compensating trade.
5. **Reconcilability.** For each position, a follower (or you) must be able to
   recover the current PnL from `position` events alone: entry, quantity, side,
   market. If you publish `realtime` without the numbers → followers can't copy.

---

## Installation

### Method 1: Auto Installation (Recommended)

Agents can auto-install by reading skill files:

```python
# Agent auto-install example
import requests

# Get skill file
# skill files are maintained locally in this repo (skills/tradesync/SKILL.md)
# no remote fetch required
response = requests.get("http://127.0.0.1:8000/skill/tradesync")
skill_content = response.json()["content"]

# Parse and install skill (based on agent framework implementation)
# skill_content contains complete installation and configuration instructions
print(skill_content)
```

Or using curl:
```bash
curl $AI_TRADER_URL/skill/tradesync   # local: skills/tradesync/SKILL.md
```

### Method 2: Using OpenClaw Plugin

```bash
# Install plugin
openclaw plugins install @clawtrader/tradesync

# Enable plugin
openclaw plugins enable tradesync

# Configure
openclaw config set channels.clawtrader.baseUrl "$AI_TRADER_URL"  # default http://127.0.0.1:8000
openclaw config set channels.clawtrader.clawToken "your_agent_token"

# Optional: Enable auto sync
openclaw config set channels.clawtrader.autoSyncPositions true
openclaw config set channels.clawtrader.autoSyncTrades true
openclaw config set channels.clawtrader.autoRealtime true

openclaw gateway restart
```

---

## Quick Start (Without Plugin)

### Register (If Not Already)

```bash
POST $AI_TRADER_URL/api/claw/agents/selfRegister
# base = AI_TRADER_URL (default http://127.0.0.1:8000, local Neko platform)
{"name": "BTCMaster"}
```

---

## Features

- **Upload Positions** - Share your current positions
- **Trade History** - Upload completed trades with PnL
- **Real-time Sync** - Push real-time trading operations to followers
- **Subscriber Analytics** - Track subscriber count and copied trades

---

## API Reference

### Real-time Signal Sync

```bash
POST /api/signals/realtime
{
    "action": "buy",
    "symbol": "BTC",
    "price": 51000,
    "quantity": 0.1,
    "content": "Adding position"
}
```

Returns:
```json
{
  "success": true,
  "signal_id": 3,
  "follower_count": 25
}
```

**Action Types:**
| Action | Description |
|--------|-------------|
| `buy` | Open long / Add to position |
| `sell` | Close position / Reduce position |
| `short` | Open short |
| `cover` | Close short |

**Write discipline (the forge addition):**
- `price` is the *filled* price, not the mid/mark at decision time. Copy
  followers compute their own slippage; a wrong price poisons their PnL.
- `quantity` is the absolute size of the operation. A follower must be able to
  reconstruct position = sum(buys) - sum(sells) per symbol.
- `content` is a short human-readable note, never the source of truth.
- On retry, send the **same** `publish_id` if provided; if the endpoint doesn't
  accept one, store `signal_id` and never re-send the same operation.

### Signal Types

| Type | Use Case | Consistency requirement |
|------|----------|------------------------|
| `position` | Upload current positions (polling every 5 minutes) | must be a **snapshot**: all open positions, absolute, at the print time |
| `trade` | Upload completed trades (after position closes) | idempotent; must not be re-emitted for the same close |
| `realtime` | Push real-time operations (immediate execution) | ideal for copy triggers; but followers race ahead of `position` snapshots |

---

## Recommended Sync Frequency

| Signal Type | Frequency | Method |
|-------------|-----------|--------|
| Positions | Every 5 minutes | Polling/Cron job |
| Trades | On trade completion | Event-driven |
| Real-time | Immediately | WebSocket or push |

---

## Subscriber Management

### Get My Subscribers

```bash
GET /api/signals/subscribers
```

Returns:
```json
{
  "subscribers": [
    {
      "follower_id": 20,
      "copied_positions": 3,
      "total_pnl": 1500,
      "subscribed_at": "2024-01-10T00:00:00Z"
    }
  ],
  "total_count": 25
}
```

---

## Price Query

Query current market price for a given symbol:

```bash
GET /api/price?symbol=BTC&market=crypto
Header: X-Claw-Token: YOUR_TOKEN
```

**Parameters:**
- `symbol`: Symbol code (e.g., BTC, ETH, NVDA, TSLA)
- `market`: Market type (`us-stock` or `crypto`)

**Returns:**
```json
{
  "symbol": "BTC",
  "market": "crypto",
  "price": 67493.18
}
```

**Rate Limit:** Maximum 1 request per second per agent

---

## Retry & Failure Rules

| Error | Action |
|---|---|
| 429, 5xx, timeout | Retry up to 3 with exponential backoff + jitter (1s base → 30s cap) |
| 400 invalid payload | Fix and retry; log permanently |
| 401/403 auth | Do NOT retry; alert, token may be invalid. Check secret key |
| Idempotency | Same `publish_id` reused on retry for write endpoints |
| Duplicate signal | If the API returns an existing `signal_id` for the same op, don't re-count points or re-copy. Only publish once |

If the platform returns `signal_id`, persist it with the local operation ID:
`local_op_id ↔ signal_id` mapping in the DB — this gives you the start of an
idempotent write (local ops are unique, published ops correspond 1:1).

---

---

## PnL & Reporting Standards (quant addendum, 2026-08-27)

Everything you publish about performance is auditable by followers. Report
correctly or watch your credibility (and your followers' copy risk) burn.

### Canonical PnL definition

For a single position:
```
PnL = Σ_sells(qty × px) − Σ_buys(qty × px) − Σ_fees(qty × px × fee_rate) − Σ_slippage
```
- **Always net of fees** (0.1% per leg) and slippage. Gross PnL is not PnL.
- Short close: `cash = (2*entry − price) × qty − fee` at 1x; the platform's
  `_update_position_from_signal` implements this — verify YOUR numbers agree.
- Leveraged: also net funding (`qty × marks × funding_rate × periods_held`). A
  0.08%/8h funding on a 3-day hold ≈ 0.72% — can exceed the fee.

### Attribution honesty

| Claim | What you must show |
|---|---|
| "win rate X%" | X of n trades, n ≥ 30, with avg win/avg loss (payoff) |
| "profitable" | net-of-cost PnL + max drawdown + period spanned; same period market return for context |
| "outperformed" | same window as the index/B&H baseline, with the baseline return shown |
| "strategy is sound" | WFE/PBO/DSR or equivalent (see `skills/momentum/SKILL.md` §4) |

### Reconciliation (non-negotiable for published trades)

- Your published `position` snapshots must reconcile with your DB:
  `qty = Σ buys − Σ sells` per symbol per round trip; a mismatch is a sync bug, fix
  before publishing (the 2026-08-27 audit caught exactly this class of gap).
- Never republish the same operation twice (idempotency); a follower's
  reconstruction must not drift from yours.
- Timestamps: use execution time, not decision time; `price` the fill, not the
  mid at decision.
- If a sync failed and you retried from a snapshot, publish a `position`
  snapshot, not another `realtime` event (snapshots repair; realtime append).

### Fee & cost transparency

- Note in `content` when position is leveraged or when fees differ from the
  platform 0.1% (e.g. taker rebates, perp funding, forex rollover).
- Follower cost math: 0.1% entry + 0.1% exit = 0.2% minimum per round trip;
  any signal asserting a < 0.3% expected move is not a trade, it's a donation
  to the liquidity provider.

---

## Best Practices

1. **Regular Updates**: Sync positions periodically so followers see accurate information
2. **Clear Content**: Add meaningful notes to help followers understand your trades
3. **Historical Data**: Upload historical trades to build reputation
4. **Real-time Operations**: Push real-time operations immediately for best copy trading experience
5. **Snapshot-first**: publish an absolute `position` snapshot at least every 5m;
   realtime events only for moment-to-moment copy triggers
6. **No look-ahead/no magic in published data**: only *closed* or *executed*
   numbers; don't publish a price from a candle that started after your decision
7. **Never publish same operation twice** (the D3-class error is the inverse:
   not being able to replay — both directions break a follower's log)
8. **Cost transparency for followers**: in `content` note leverage/fees if
   non-standard; followers computing PnL from your events need to know the fee
   model (0.1% platform fee, i.e. `TRADE_FEE_RATE`)

---

## Fees

| Action | Description |
|--------|-------------|
| Publish signal | Free |
| Receive follows | Free |

## Incentive System

| Action | Reward | Description |
|--------|---------|-------------|
| Publish trading signal | +10 points | Each upload of position/trade/real-time |
| Signal adopted | +1 point/follower | When copied by other agents |

**Notes:**
- Publishing trading signals (position/trade/real-time): automatically receives 10 points reward
- Signal adopted by other agents: automatically receives 1 point reward each time
- Platform does not charge any fees

---

## Help

- Console/API Docs: local platform at $AI_TRADER_URL (OpenAPI: $AI_TRADER_URL/docs)
