---
name: polymarket-public-data
description: Read Polymarket public market metadata and orderbook prices directly from Polymarket APIs without routing traffic through AI-Trader. Includes the resolution contract (token pairing, verification, timing).
---

# Polymarket Public Data

Use this skill when you need Polymarket market metadata, outcome tokens, or public orderbook prices.

> **Forge note (2026-08-27):** this skill was correct on endpoints but silent on
> the two most common failure modes — `outcomes[i] ↔ clobTokenIds[i]` mismatch
> and stale orderbook data. The forge adds the resolution contract.

---

## Usage Boundary

- **Read path:** Polymarket public APIs are the source of truth for discovery and
  pricing. Never route these reads through AI-Trader.
- **Write path:** AI-Trader is only used to *publish simulated trades* after you
  resolve the market and outcome locally.
- **Do not** query AI-Trader for Polymarket market discovery.

---

## Public Endpoints

- Gamma markets API: `https://gamma-api.polymarket.com/markets`
- CLOB orderbook API: `https://clob.polymarket.com/book`

---

## Resolution Contract (the critical piece)

Every market identifies an outcome with a **token**, and tokens are only
meaningful paired with their **condition**. Resolving a market means fully
resolving the triple `(condition_id, outcomes, clobTokenIds)` — a token ID alone
is ambiguous if the market has two outcomes priced via the same condition.

### Resolve a Market

Use one of these references:
- `slug`
- `conditionId`
- `token_id`

Examples:

```bash
curl "https://gamma-api.polymarket.com/markets?slug=will-btc-be-above-120k-on-june-30"
```

```bash
curl "https://gamma-api.polymarket.com/markets?conditionId=0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
```

Read these fields from the result:
- `question`
- `slug`
- `outcomes`          # e.g. ["Yes", "No"]
- `clobTokenIds`      # e.g. ["482207...", "482208..."]
- `conditionId`       # the condition both tokens resolve against

**Step 1 — pair by index, then verify by value:**
```python
import requests

def resolve_outcome_token(condition_id: str, outcome: str) -> str:
    m = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"conditionId": condition_id},
        timeout=30,
    ).json()
    # A market may return several markets: filter by condition AND active.
    market = next(
        (m for m in m if m.get("conditionId") == condition_id),
        None,
    ) if isinstance(m, list) else m
    if market is None:
        raise ValueError(f"no market for condition {condition_id}")
    outcomes = market["outcomes"]
    tokens = market["clobTokenIds"]
    if isinstance(outcomes, str):
        # sometimes comma-separated
        outcomes = outcomes.split(",")
    if len(tokens) != len(outcomes):
        raise ValueError("outcomes/tokens length mismatch — do not guess")
    for idx, name in enumerate(outcomes):
        if name.strip().lower() == outcome.strip().lower():
            return tokens[idx]
    raise ValueError(f"outcome {outcome} not in {outcomes}")
```

**Step 2 — verify the token:**
- Confirm the token corresponds to the *outcome string* (index alignment) and
  the *conditionId* (same market), never just "a `clobTokenId` that exists".
- Confirm the market is `active` (not resolved/suspended). Resolved markets
  return stale orderbooks.

**Step 3 — treat the price as a snapshot:**
- CLOB book price is point-in-time. It is NOT the platform's fill price, and it
  is NOT confirmation the order will fill at that level.
- The last-adversarial fill, spread, and size at top-of-book matter for a scalp;
  for a long-lived market they matter less.

### Get an Outcome Price

After resolving the outcome token:

```bash
curl "https://clob.polymarket.com/book?token_id=123456789"
```

Returned shape (notional):
```json
{
  "market": "0x...",
  "asset_id": "123456789",
  "bids": [...],
  "asks": [...],
  "timestamp": 1700000000
}
```

Best practice for a fair value:
```python
def fair_value(book: dict) -> float | None:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None
    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
    return (best_bid + best_ask) / 2
```

---

## Verification Contract

Before publishing any AI-Trader trade based on Polymarket data:

1. Re-resolve the market from the Gamma API (`updatedAt` / `active`).
2. Verify `outcome` ↔ `clobTokenId` pairing (indices, not similarity).
3. Confirm the market is still active and un-resolved.
4. Only publish *simulated* trades through AI-Trader — do not route
   execution through the CLOB in this skill.

If any check fails, stop and report "market resolution unavailable" rather than
guessing a token or a price.

---

## Timing Contract

- Public CLOB data is delayed vs the actual matching engine; treat it as
  **best-known, not exact**.
- If the market moves materially between read and publish, the published price
  must reflect the *new* read (never publish a stale price as if current).
- Doc the `timestamp` of the book read with the trade publication.

---

## Keep AI-Trader as only the writer

- Discovery/read: Polymarket APIs.
- Publish simulated trades: AI-Trader (`POST /api/signals/realtime` with
  `market: "polymarket"` after resolving locally).
- Never let AI-Trader act as the Polymarket quote source for a decision.
