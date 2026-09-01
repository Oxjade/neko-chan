<div align="center">

# Neko 🤖

**An AI trader bot network — from paper evaluation to real on-chain execution.**

</div>

Neko is a self-contained AI trading platform in three layers:

1. **Paper-trading platform** — real market data (Hyperliquid, yfinance, Alpha Vantage, Polymarket) with simulated capital, fees, stops, leverage, funding, and honest statistics (F1, bootstrap CIs, walk-forward).
2. **Telegram bot network** — users bring their own bot token (BotFather) and AI API key; the master bot onboards them in minutes and their bot becomes a live dashboard with push notifications.
3. **Real-trading execution gateway** — the same AI decision engine routes orders to real venues: **Hyperliquid** (perps via non-custodial agent wallets), **Solana** (**Jupiter** swap/limit/perps + **xStocks** tokenized US stocks), and **Sui** (**DeepBook** spot/margin, **Aftermath** perps).

---

## ✨ Features

| Layer | Highlights |
|---|---|
| **Paper platform** | Real prices · 0.1% fees · stop-loss/take-profit auto-close · perps with leverage/liquidation/funding · forex (24/5) & US stocks (market hours enforced) · time-travel guard · live leaderboard |
| **AI agent** | Trend + sentiment decisions (Fear & Greed, news, market context) · hard risk guards (position caps, mandatory stops, daily trade limits) · every decision logged for evaluation |
| **Telegram network** | Master bot (`/addbot`: token → ownership proof → name) · per-user bots with full telemetry dashboard · push notifications with action buttons · AI-key onboarding |
| **Real trading** | Non-custodial (delegated keys only, zero withdrawal rights) · one order model across venues · risk guard enforced pre-signature · kill-switch flats everything · testnet-first rollout |

## 🔬 What the research says

The evaluation layer measures honestly what most trading platforms only claim:

- The 20-day crypto momentum strategy beat buy-and-hold in both 2021–23 and 2024–26 backtests (+374%/+203% on BTC) — **but** its next-day directional F1 is a coin flip, and its excess-return CI includes zero. Profits are market beta, not prediction.
- Active strategies on US stocks underperform buy-and-hold; forex shows no significant edge at 5m scale.
- Risk control (stops, caps, cash discipline) is what separates robust agents from lucky ones — matching the published AI-Trader benchmark findings.

The tools for reaching these conclusions are included: `research/scripts/` backtests with block-bootstrap CIs, walk-forward, and Bonferroni-corrected model searches.

## 🏗 Architecture

```
┌─ Telegram ────────────────────────────────────────────────┐
│  Neko master bot  →  user bots (their token, their AI key)│
└──────────┬────────────────────────────────────────────────┘
           │ decisions (trend + sentiment + risk guards)
┌──────────▼────────────────────────────────────────────────┐
│  AI-Trader paper platform  (real prices, simulated money) │
└──────────┬────────────────────────────────────────────────┘
           │ intents → risk guard → chain adapter → signed order
┌──────────▼────────────────────────────────────────────────┐
│  Execution gateway (non-custodial)                        │
│  Hyperliquid · Solana (Jupiter + xStocks) · Sui (DeepBook)│
└───────────────────────────────────────────────────────────┘
```

- `service/server/` — FastAPI paper-trading platform
- `service/tg_bot/` — master + user Telegram bots
- `service/agent/` — AI decision loop + sentiment feeds
- `service/execution/` — real-trading gateway (adapters, risk guard, ledger, kill-switch)
- `research/scripts/` — statistical evaluation & backtesting
- `docs/` — system designs (paper, telegram UX, real trading)

## 🚀 Quickstart

### Paper platform
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r service/requirements.txt

# server (API) + worker (prices, stops, settlements) in two terminals
python -m uvicorn main:app --app-dir service/server --port 8000
cd service/server && python worker.py
```
Trade via the API:
```bash
curl -X POST http://localhost:8000/api/claw/agents/selfRegister \
  -H 'Content-Type: application/json' -d '{"name":"MyAgent","password":"pass"}'
# → token
curl -X POST http://localhost:8000/api/signals/realtime \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"market":"crypto","symbol":"BTC","action":"buy","quantity":0.1,
       "price":0,"executed_at":"now","stop_loss_pct":5,"take_profit_pct":10}'
```

### Telegram bots
```bash
export TG_MASTER_TOKEN="<BotFather token>"
export TG_VAULT_MASTER_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
python service/tg_bot/main.py
```
Users: open your master bot → `/addbot` → paste their BotFather token → verify ownership → name their bot → done. Their bot asks for an AI API key on first open, then trades.

### Real trading (testnet → mainnet)
```bash
export TG_EXEC_MASTER_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export HL_TESTNET_MASTER_KEY=...   # testnet wallet keys
python scripts/hl_testnet_check.py
```
Mainnet is **impossible unless the operator deliberately sets `REAL_TRADING_ENABLED`** — by design.

## 🧪 Tests

```bash
python -m pytest service/server/tests/ tests/tg_bot/ tests/execution/ -q   # 265 tests
```

## 🔒 Security model

- **Non-custodial**: the platform never holds user funds. Trading keys are delegated (Hyperliquid agent wallets have zero withdrawal rights; Sui/Solana use dedicated funded wallets).
- Keys are Fernet-encrypted with separate master keys per scope (Telegram/AI vs execution); never logged, masked on screen.
- Risk guards are client-side and cannot be overridden by model output; a kill-switch flattens every chain.
- **Real trading is gated**: testnets first, then mainnet with $50–500 caps, scaling only after verified trades.

## 📚 Documentation

| Doc | Content |
|---|---|
| `docs/real-trading/system-design.md` | Multi-chain execution design (protocols, delegation, phases) |
| `docs/real-trading/wallet-funding-sync.md` | Wallets, deposits, RPC sync, fees, bot flows |
| `docs/telegram-bot/system-design.md` | Master/user bot architecture |
| `docs/telegram-bot/UX-design.md` | Complete UX spec (every screen, every button) |

## ⚠️ Disclaimer

Paper trading uses real market data with simulated money. Real trading involves substantial risk of loss. Nothing in this repository is financial advice; leverage can liquidate positions; tokenized equities carry issuer and market risks. Forex support is coming soon on all chains.

---

<div align="center"><sub>MIT licensed · built for evaluation first, real execution second</sub></div>