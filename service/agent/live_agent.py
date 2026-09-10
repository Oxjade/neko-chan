"""
Live LLM trading agent for AI-Trader.

Connects an opencode-go model to the running AI-Trader platform:
  1. Fetches live prices + portfolio state from the platform API
  2. Asks the LLM for a structured decision (JSON)
  3. Enforces client-side risk guards (daily trade limit, position size cap,
     mandatory stop-loss, market hours)
  4. Executes through POST /api/signals/realtime (live trading, real prices)
  5. Logs every decision to research/exports/live_agent_log.csv so F1 and
     profitability can be measured with the same statistical rigor as the
     offline backtests (evaluate_live_agent.py).

Usage:
  python service/agent/live_agent.py --once          # single decision cycle
  python service/agent/live_agent.py                 # loop forever (interval env)

Env:
  LIVE_AGENT_MODEL            default opencode-go/deepseek-v4-flash
  LIVE_AGENT_SYMBOLS          default "BTC,ETH"
  LIVE_AGENT_INTERVAL         seconds between cycles, default 300
  AI_TRADER_URL               default http://127.0.0.1:8000
  LIVE_AGENT_MAX_DAILY_TRADES default 4
  LIVE_AGENT_MAX_POSITION_PCT default 30  (percent of equity per symbol)
  LIVE_AGENT_FORCE_STOP_PCT   default 8   (mandatory stop on new opens)
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

AGENT_DIR = Path(__file__).resolve().parents[1]  # service/
# Per-bot session isolation: each agent subprocess is launched with its own
# LIVE_AGENT_BOT_ID (see agent_pool), so decisions + the peek cache must be
# written to a bot-specific file. A shared single file let one bot's agent
# clobber another's decisions and leaked cross-bot data into every Peek view.
_BOT_SLOT = os.getenv("LIVE_AGENT_BOT_ID", "0") or "0"
_EXPORT_DIR = Path(__file__).resolve().parents[2] / "research" / "exports"
LOG_PATH = _EXPORT_DIR / f"live_agent_log_bot{_BOT_SLOT}.csv"
CACHE_PATH = _EXPORT_DIR / f"live_agent_cache_bot{_BOT_SLOT}.json"
TOKEN_FILE = Path(__file__).resolve().parents[2] / "service" / "agent" / ".agent_token"

MODEL = os.getenv("LIVE_AGENT_MODEL", "opencode-go/deepseek-v4-flash")
# Minimum seconds between AI-key calls. The AI key is paid/rate-limited, so
# the agent only asks the model every LLM_COOLDOWN_SECONDS; in between it picks
# the best scenario deterministically (quant math). Default 900s = 15 min.
LLM_COOLDOWN_SECONDS = int(os.getenv("LIVE_AGENT_LLM_COOLDOWN", "900"))
_last_llm_at: float = 0.0
# universe as symbol:market pairs, e.g. "BTC:crypto,ETH:crypto,AAPL:us-stock,EURUSD:forex"
UNIVERSE = [
    (s.strip().split(":")[0], (s.strip().split(":")[1] if ":" in s else "crypto"))
    for s in os.getenv("LIVE_AGENT_SYMBOLS", "BTC,ETH").split(",")
    if s.strip()
]
INTERVAL = int(os.getenv("LIVE_AGENT_INTERVAL", "120"))
BASE_URL = os.getenv("AI_TRADER_URL", "http://127.0.0.1:8000")
MAX_DAILY_TRADES = int(os.getenv("LIVE_AGENT_MAX_DAILY_TRADES", "12"))
# Per-user watchlist: comma-separated symbols the user typed "watch <ASSET>" for.
# These are PREPENDED to the universe so the agent always considers them first.
WATCHED = [s.strip().upper() for s in os.getenv("LIVE_AGENT_WATCHLIST", "").split(",") if s.strip()]
# PRIORITY WATCH: 'watch <ASSET> now' — analyze THIS symbol first and take
# its next valid setup, overriding cooldown/one-shot/direction locks. Cleared
# (via the registry) once the trade is taken.
PRIORITY = os.getenv("LIVE_AGENT_PRIORITY", "").strip().upper()
MAX_POSITION_PCT = float(os.getenv("LIVE_AGENT_MAX_POSITION_PCT", "45"))
FORCE_STOP_PCT = float(os.getenv("LIVE_AGENT_FORCE_STOP_PCT", "5"))
# 1 = active scalper mode: hold a position most of the time (long/short), trade often.
# 0 = conservative mode: cash-preferred, only trade on clear setups.
ACTIVE_MODE = os.getenv("LIVE_AGENT_ACTIVE_MODE", "1").strip() in {"1", "true", "yes", "on"}
# STRATEGY selects the decision engine:
#   "momentum20" (DEFAULT): deterministic, validated 20d momentum + funding-carry
#     overlay from quant_strategy.py. Long only when 20d return > 2%, stop 8% /
#     take 24%, risk 1%/trade, half-Kelly, vol-targeted. Cash is the default. This
#     is the strategy the skills actually validate (backtest_risk_controlled.py).
#   "scalper": the old LLM 5m active scalper. NOT validated - the audit
#     (agent_evaluation_report.md) measured it at base-rate accuracy, negative
#     after fees. Kept only for A/B.
STRATEGY = os.getenv("LIVE_AGENT_STRATEGY", "momentum20").strip().lower()
# What kind of trader the user wants to be. Filters which horizon targets the
# engine can pick: scalp (minutes) / intraday (hours) / swing (days) / auto.
# A scalp trader never sees 4% swing targets.
TRADER_TYPE = os.getenv("LIVE_AGENT_TRADER_TYPE", "scalp").strip().lower()
# 5-minute data window (hours) fed to the scalp scenario engine. The engine
# derives drift/vol/RSI from THIS window so 5-min momentum (not 30-day daily
# drift) decides long vs short. 288 bars/day, so 6h = 72 bars (plenty for a
# lookback of 20, cheap on the RPC).
SCENARIO_5M_HOURS = int(os.getenv("LIVE_AGENT_SCENARIO_5M_HOURS", "6"))
# Conviction floor: a scenario only becomes a candidate when its conviction
# (P(win) * EV) clears this bar. Below it the move is noise - we hold cash
# instead of posting a low-conviction decision. Only ONE best trade is ever
# posted per cycle (across all watched tokens), picked from the floor-crossers.
CONVICTION_FLOOR = float(os.getenv("LIVE_AGENT_CONVICTION_FLOOR", "0.0008"))
# LIMIT-ENTRY OFFSET (basis points): entry is placed this far inside the
# current market so the fill happens immediately even if price moved since the
# LLM built the scenario, and the resting portion gets maker pricing. Top-of-
# book spread on liquid perps is ~0.5-2 bps; 2 bps is a safe immediate-fill
# offset that still avoids most of the taker spread cost (web research).
ENTRY_OFFSET_BPS = float(os.getenv("LIVE_AGENT_ENTRY_OFFSET_BPS", "2"))
# Peak-price tracker for trailing stops (persisted to disk so it survives
# agent restarts — without this a restart resets the tracker and a winning
# position that was up 20% loses its peak, potentially missing the trail exit).
# Per-bot isolated: each bot's trailing high is independent (otherwise bot A's
# peak would leak into bot B's exit logic).
_trailing_high: dict[str, float] = {}
TRAILING_HIGH_PATH = _EXPORT_DIR / f"trailing_high_cache_bot{_BOT_SLOT}.json"
try:
    if TRAILING_HIGH_PATH.exists():
        import json as _json
        _trailing_high = _json.loads(TRAILING_HIGH_PATH.read_text(encoding="utf-8"))
except Exception:
    pass
# Per-user LLM credentials (set by the Telegram bot network). When LIVE_AGENT_API_KEY
# is set, decisions call the provider API directly instead of the opencode CLI.
LIVE_AGENT_API_KEY = os.getenv("LIVE_AGENT_API_KEY", "")
LIVE_AGENT_PROVIDER = os.getenv("LIVE_AGENT_PROVIDER", "openai")
LIVE_AGENT_BASE_URL = os.getenv("LIVE_AGENT_BASE_URL", "")
LIVE_AGENT_LEVERAGE = float(os.getenv("LIVE_AGENT_LEVERAGE", "20"))
# Max leverage is PER ASSET, per venue. VERIFIED from Aftermath
# /api/perpetuals/all-markets marginRatioInitial (maxLev = 1/IMR), 2026-09-01:
# BTC/ETH/SOL/XAUT 20x, most crypto+equity 10x, commodities/small 5x. The bot
# clamps any user-selected leverage to the venue/asset max so it never submits
# a leverage the venue rejects.
AFTERMATH_MAX_LEVERAGE = {
    "BTC": 20, "ETH": 20, "SOL": 20, "XAUT": 20,
    "SUI": 10, "HYPE": 10, "XRP": 10, "UNI": 10, "XMR": 10, "ZEC": 10,
    "MON": 10, "XAG": 10, "US500": 10, "GOOGL": 10, "NVDA": 10, "TSLA": 10,
    "INTC": 10, "MU": 10, "MRVL": 10, "PUMP": 10, "SPCX": 10, "LIT": 10,
    "WTI": 5, "AMC": 5, "DRAM": 5, "LLY": 5, "IOVA": 5, "SNDK": 5, "CHIP": 5,
}


def liq_distance_pct(lev: float, max_lev: float) -> float:
    """% adverse price move until the venue liquidates a position at leverage `lev`.

    Verified live from Aftermath /perpetuals/all-markets (2026-09-04):
    BTC/ETH/SOL IMR 0.05 (20x max) with MMR 0.025; SUI/XRP/HYPE IMR 0.1 (10x)
    with MMR 0.05 - i.e. MMR = IMR/2 = 1/(2*max_lev). A position liquidates
    when its margin ratio falls to MMR:
        margin_ratio(lev) - adverse_move = MMR
        adverse_move = 1/lev - 1/(2*max_lev)
    At max leverage that is 1/(2*max_lev): 2.5% for 20x markets, 5% for 10x.
    Funding burn (measured ~0.004%/h ~= 0.1%/day of notional) erodes this
    further on interday holds."""
    if max_lev <= 0 or lev <= 0:
        return 0.0
    return max(0.0, (1.0 / lev - 1.0 / (2.0 * max_lev)) * 100.0)


# Conviction-based leverage: applies ONLY to 20x-leverage markets
# (BTC/ETH/SOL/XAUT). 13x at floor conviction, scaling linearly to the venue
# cap (20x) as conviction doubles. Loss per stop-out = notional x stop%, which
# leverage does NOT change - leverage only moves the liquidation line and the
# margin locked, so the floor is safe as long as stops stay <= 4% (liq at 13x
# sits 5.19% against entry, MMR = IMR/2 verified live; 5.19/4 = 1.3x buffer).
# 10x-cap markets (SUI/XRP/HYPE) are NOT subject to this floor - they stay on
# the liq-safe margin-fit leverage.
CONVICTION_LEV_FLOOR = float(os.getenv("LIVE_AGENT_MIN_LEVERAGE", "13.0"))
CONVICTION_LEV_CAP_MULT = 2.0  # conviction 2x floor -> full venue cap


def clamp_leverage(symbol: str, market: str, lev: float,
                   stop_pct: float | None = None) -> float:
    """Clamp requested leverage so the STOP always fires before the venue can.

      1. venue/asset cap (BTC/ETH/SOL 20x, SUI/XRP/HYPE 10x, most others 5x)
      2. liquidation safety: the stop must sit inside 80% of the liq distance
         (the remaining 20% covers funding burn + fees on multi-day holds).
         At 20x liquidation sits 2.5% against the entry (MMR = IMR/2,
         verified live) - a stop the venue outruns is not a stop: the
         position dies in a partial-liquidation cascade (0.5% liquidation
         fee + slippage) while the bot still shows it open.
    The 13x conviction floor (20x markets only) is applied by the caller
    (balance_aware_size) which also verifies liq safety. 1x if not a perp."""
    if market != "crypto":
        return 1.0
    if lev <= 1:
        return 1.0
    cap = AFTERMATH_MAX_LEVERAGE.get(symbol.upper(), 10)
    lev = min(lev, float(cap))
    if stop_pct and stop_pct > 0:
        # LIQ SAFETY: the stop must fire before liq, with a 20% buffer for
        # funding burn + fees on multi-day holds. liq_dist(lev, cap) =
        # 1/lev - 1/(2*cap) >= stop*1.25  =>  lev <= 1 / (stop*1.25 + 1/(2*cap))
        safe = 1.0 / (stop_pct / 100.0 * 1.25 + 1.0 / (2.0 * cap))
        lev = min(lev, max(1.0, safe))
    return max(1.0, lev)


def conviction_leverage(symbol: str, market: str, conviction: float,
                        stop_pct: float | None = None,
                        user_cap: float | None = None) -> float:
    """Leverage measured by conviction - 13x floor on 20x-leverage markets.

    Scales linearly from CONVICTION_LEV_FLOOR (13x at floor conviction) to
    the effective cap as conviction doubles above the floor, then liq-safety
    clamped via clamp_leverage. Conviction below the floor keeps the 13x
    floor (it is a floor, not a target). 10x-cap markets are exempt: they
    use the liq-safe margin-fit leverage as before.

    The USER'S Settings leverage is the hard CAP (user_cap): conviction can
    never trade above what the user set. user_cap defaults to the venue cap
    (legacy behaviour) when not provided."""
    if market != "crypto":
        return 1.0
    venue_cap = AFTERMATH_MAX_LEVERAGE.get(symbol.upper(), 10)
    cap = float(user_cap) if (user_cap and user_cap > 1) else float(venue_cap)
    # never above the venue's own limit, whatever the user set
    cap = min(cap, float(venue_cap))
    if cap < CONVICTION_LEV_FLOOR:
        # cap below the 13x floor (e.g. user chose 5x on a 20x market, or a
        # 10x venue): the USER CAP WINS - no conviction floor is forced on
        # top of an explicit lower setting. Liq-safe margin-fit base.
        return clamp_leverage(symbol, market, max(cap, 2.25), stop_pct=stop_pct)
    f = 0.0
    if conviction > 0:
        f = max(0.0, min(1.0, (conviction - CONVICTION_FLOOR) / CONVICTION_FLOOR))
    req = CONVICTION_LEV_FLOOR + f * (cap - CONVICTION_LEV_FLOOR)
    return clamp_leverage(symbol, market, req, stop_pct=stop_pct)
# Real execution through the chain adapters (execution gateway). Requires
# REAL_TRADING_ENABLED=1 AND per-chain keys in the gateway env. Default off:
# the agent stays paper-only on the platform. When on, orders route through
# the VenueRouter -> chain adapter -> real venue, risk-guarded + ledgered.
EXEC_ENABLED = os.getenv("LIVE_AGENT_EXECUTION", "0").strip() in {"1", "true", "yes", "on"}
EXEC_BOT_ID = int(os.getenv("LIVE_AGENT_BOT_ID", "1") or "1")
# TRADING MODE: "paper" (default for every new bot - $1,000 virtual USDC,
# virtual fills at live prices with the REAL cost model) or "live" (real
# venues). Toggled from the Telegram dashboard, read by the agent each boot.
TRADING_MODE = os.getenv("LIVE_AGENT_TRADING_MODE", "paper").strip().lower()
_paper_gw_instance = None


def _request_paper_approval(decision_key: str, symbol: str, direction: str,
                            qty: float, price: float, leverage: float,
                            stop_pct: float, take_pct: float,
                            reasoning: str = "",
                            timeout_s: int | None = None) -> str:
    """Send a Take/Reject approval card to the user and wait for the verdict.

    Writes the pending decision to paper_decisions (shared registry.db), sends
    the Telegram message with ✅/❌ buttons, then polls the row. Returns
    'taken' | 'rejected' | 'timeout'. The userbot's button handlers flip the
    row status; the agent only reads.

    Timeout (default PAPER_APPROVAL_TIMEOUT, 300s) auto-executes so the bot
    stays autonomous when the user is away."""
    if timeout_s is None:
        timeout_s = int(os.getenv("PAPER_APPROVAL_TIMEOUT", "300"))
    side = "LONG" if direction == "long" else "SHORT"
    reason = reasoning[:140]
    text = (
        f"🎯 <b>Neko-Chan wants to {side} {_esc(symbol)}</b>\n\n"
        f"   {'🟢' if direction == 'long' else '🔴'} <b>{side}</b> <code>{qty:g}</code> "
        f"{_esc(symbol)} @ <code>${price:,.4f}</code>\n"
        f"   Leverage <b>{leverage:g}x</b> · ⛔ Stop {stop_pct:.1f}% · 🎯 Take {take_pct:.1f}%\n"
        f"   ⏱ Auto-executes in ~{timeout_s // 60} min if ignored\n"
        + (f"\n   💡 {_esc(reason)}" if reason else "")
    )
    kb = {"inline_keyboard": [[
        {"text": "✅ Take", "callback_data": f"papertake:{decision_key}"},
        {"text": "❌ Reject", "callback_data": f"paperreject:{decision_key}"},
    ]]}
    sent_ok = False
    try:
        import requests as _r
        r = _r.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                    json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
                          "reply_markup": kb},
                    timeout=15)
        sent_ok = r.status_code == 200
    except Exception as exc:
        print(f"[paper] approval notify failed: {exc}")
    if not sent_ok:
        return "timeout"  # can't ask -> auto-execute
    # poll the shared DB for the verdict
    _tg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tg_bot")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        try:
            from paper_store import PaperStore
            db_path = os.path.join(_tg, "registry.db")
            row = PaperStore(db_path).get_decision(decision_key)
            if row and row.get("status") == "rejected":
                return "rejected"
            if row and row.get("status") == "taken":
                return "taken"
        except Exception:
            continue
    return "timeout"


def _clear_priority(bot_id: int) -> None:
    """Clear the bot's priority_watch demand (agent-side, direct DB write to
    the same registry.db the userbot reads). WAL + short transaction: safe
    against the bot process's concurrent writes."""
    _tg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tg_bot")
    import sqlite3 as _sq
    for attempt in range(3):
        try:
            con = _sq.connect(os.path.join(_tg, "registry.db"), timeout=5)
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("UPDATE bots SET priority_watch='' WHERE id=?", (bot_id,))
            con.commit()
            con.close()
            return
        except _sq.OperationalError:
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError("registry busy - priority_watch not cleared")


def _paper_gateway():
    """Lazily-built paper gateway per agent process. Portfolio lives in the
    registry DB (paper_* tables) so the dashboard can render it."""
    global _paper_gw_instance
    if _paper_gw_instance is None:
        _tg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tg_bot")
        if _tg not in sys.path:
            sys.path.insert(0, _tg)
        from paper_gateway import PaperGateway
        from paper_store import PaperStore
        db_path = os.path.join(_tg, "registry.db")
        _paper_gw_instance = PaperGateway(PaperStore(db_path))
    return _paper_gw_instance
_exec_gateway = None


# ---------------------------------------------------------------- platform client

def _get_token() -> str:
    if os.getenv("LIVE_AGENT_TOKEN"):
        return os.getenv("LIVE_AGENT_TOKEN")
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    name = os.getenv("LIVE_AGENT_NAME", "LiveAgent")
    r = requests.post(f"{BASE_URL}/api/claw/agents/selfRegister",
                      json={"name": name, "password": "live-agent-pass-2026"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"register failed: {r.text[:200]}")
    token = r.json()["token"]
    TOKEN_FILE.write_text(token)
    print(f"[agent] registered as {name}")
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def get_portfolio(token: str) -> dict:
    r = requests.get(f"{BASE_URL}/api/positions", headers=_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def get_price(token: str, symbol: str, market: str) -> float:
    r = requests.get(f"{BASE_URL}/api/price", headers=_headers(token),
                     params={"market": market, "symbol": symbol}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"price {symbol}: {r.text[:120]}")
    return float(r.json()["price"])


# Aftermath public perp CLOB orderbook (no API key required for reads). Used to
# price Sui-based perp analysis/trades directly from the venue's own book
# instead of routing through Hyperliquid.
# Aftermath is the primary perp market-data source. Overridable via env
# (AFTERMATH_API_BASE or AFTERMATH_TESTNET_API_BASE) for staging/self-host.
AFTERMATH_API = os.getenv("AFTERMATH_API_BASE", "https://aftermath.finance/api").rstrip("/")
AFTERMATH_TESTNET_API = os.getenv("AFTERMATH_TESTNET_API_BASE",
                                  "https://testnet.aftermath.finance/api").rstrip("/")
AFTERMATH_MARKET_SYMBOLS = {
    "BTC": "BTC/USD:USDC", "ETH": "ETH/USD:USDC", "SOL": "SOL/USD:USDC",
    "SUI": "SUI/USD:USDC", "HYPE": "HYPE/USD:USDC", "XRP": "XRP/USD:USDC",
    "UNI": "UNI/USD:USDC", "XMR": "XMR/USD:USDC", "ZEC": "ZEC/USD:USDC",
    "MON": "MON/USD:USDC", "XAUT": "XAUT/USD:USDC", "XAG": "XAG/USD:USDC",
    "WTI": "WTI/USD:USDC", "US500": "US500/USD:USDC", "GOOGL": "GOOGL/USD:USDC",
    "NVDA": "NVDA/USD:USDC", "TSLA": "TSLA/USD:USDC", "INTC": "INTC/USD:USDC",
    "MU": "MU/USD:USDC", "MRVL": "MRVL/USD:USDC", "SNDK": "SNDK/USD:USDC",
    "AMC": "AMC/USD:USDC", "DRAM": "DRAM/USD:USDC", "LLY": "LLY/USD:USDC",
    "IOVA": "IOVA/USD:USDC", "SPCX": "SPCX/USD:USDC", "PUMP": "PUMP/USD:USDC",
    "CHIP": "CHIP/USD:USDC", "LIT": "LIT/USD:USDC",
}


def fetch_aftermath_price(symbol: str) -> float | None:
    """Mid price from Aftermath's public orderbook for a perp base symbol.

    Returns None when the market is unknown or the venue is unreachable, so
    the caller can fall back to the platform/Hyperliquid price.
    """
    base = (symbol or "").upper()
    try:
        import requests as _r
        # Resolve the market id (chId) from the CCXT markets catalog.
        r = _r.get(f"{AFTERMATH_API}/ccxt/markets", timeout=10)
        if r.status_code != 200:
            return None
        markets = r.json()
        ch_id = None
        for m in markets if isinstance(markets, list) else []:
            if str(m.get("base") or "").upper() == base and m.get("swap"):
                ch_id = m.get("id")
                break
        if not ch_id:
            return None
        r = _r.post(f"{AFTERMATH_API}/ccxt/orderbook", json={"chId": ch_id}, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        best_bid = max((float(x[0]) for x in bids if x and len(x) > 1), default=None)
        best_ask = min((float(x[0]) for x in asks if x and len(x) > 1), default=None)
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2.0
        if best_bid is not None:
            return best_bid
        if best_ask is not None:
            return best_ask
        return None
    except Exception:
        return None


def get_history(symbol: str, market: str, days: int = 30) -> pd.DataFrame:
    """Daily-ish close history for trend context (prompt only).

    crypto -> Hyperliquid candleSnapshot (fast, no yfinance churn).
    us-stock/forex -> yfinance daily (HL has no such markets).
    """
    if market == "crypto":
        import requests as _r
        import time as _t
        now_ms = int(_t.time() * 1000)
        interval_ms = {"1": 3600 * 1000, "7": 7 * 3600 * 1000,
                       "30": 30 * 3600 * 1000, "365": 365 * 3600 * 1000}
        iv = "1d" if days >= 7 else "1h"
        start = now_ms - max(days, 1) * 24 * 3600 * 1000
        r = _r.post("https://api.hyperliquid.xyz/info", json={
            "type": "candleSnapshot",
            "req": {"coin": symbol, "interval": iv,
                    "startTime": start, "endTime": now_ms},
        }, timeout=15)
        closes = [float(c["c"]) for c in r.json()]
        if not closes:
            return pd.DataFrame(columns=["Close"])
        return pd.DataFrame({"Close": closes}).dropna()
    import yfinance as yf

    if market == "forex":
        ticker = f"{symbol}=X"
    else:
        ticker = symbol
    df = yf.download(ticker, period=f"{days}d", interval="1d",
                     progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Close" not in df.columns or df.empty:
        return pd.DataFrame(columns=["Close"])
    return df[["Close"]].dropna()


def execute_trade(token: str, symbol: str, market: str, action: str, quantity: float,
                  stop_loss_pct=None, take_profit_pct=None, leverage=None,
                  ref_price: float = 0.0) -> dict:
    """PAPER MODE REMOVED (2026-09): the bot only trades real venues.

    This function is retained only as a defensive stub - the agent never calls
    it (real orders route through the execution gateway). If ever invoked it
    reports the removal explicitly instead of fabricating a paper fill."""
    return {"ok": False, "error": "paper mode removed - real execution required"}


# ---------------------------------------------------------------- profitability gate

TRADE_FEE_RATE = 0.001          # platform fee per leg (fees.py TRADE_FEE_RATE)
GATE_ENABLED = os.getenv("LIVE_AGENT_PROFIT_GATE", "1").strip() in {"1", "true", "yes", "on"}

# OPTION-TWO HYBRID (mean-reversion) thresholds. Only valid in a SIDEWAYS regime
# where the range is intact; fade a stretched bar back to the mean, never a
# trend. See skills/funding-carry/SKILL.md §8 (hybrid) for the evidence note.
MEAN_REV_TREND_CAP = 0.0015     # |1h drift| below this = ranging; else trending
MEAN_REV_Z = 1.25               # |z-score| to trigger a mean-revert fade
MEAN_REV_RSI_LO = 70.0          # RSI >= this with z>=+1.25 -> mean-revert SHORT
MEAN_REV_RSI_HI = 30.0          # RSI <= this with z<=-1.25 -> mean-revert LONG
MEAN_REV_Z_HARD = 2.0           # hard z trigger even if RSI disagrees
# Funding-carry: only surface a carry signal to the LLM when net APY is high
# enough that the funding could plausibly compensate a bull-market price drift
# on an unhedged short. 15% is deliberately strict (only NEAR-class names clear
# it); surfacing 7% carries would just invite naked shorts that lose to price.
CARRY_MIN_APY = 0.15
# Risk-based sizing (replaces the LLM's freeform quantity when on).
# size_units = (equity x risk_pct) / (entry x stop_pct)
RISK_SIZE_ENABLED = os.getenv("LIVE_AGENT_RISK_SIZING", "1").strip() in {"1", "true", "yes", "on"}
RISK_PER_TRADE_PCT = float(os.getenv("LIVE_AGENT_RISK_PCT", "1.0"))  # % of equity at risk per trade
KELLY_FRACTION = float(os.getenv("LIVE_AGENT_KELLY_FRACTION", "0.5"))  # half-Kelly ceiling
TARGET_VOL_ANNUAL = 0.20        # portfolio target vol (vol-tar sizing cap)


def compute_risk_size(equity_val: float, entry_price: float, stop_pct: float,
                      take_pct: float, realized_vol: float | None = None) -> tuple[float, str]:
    """Risk-derived position size (units) + attribution string.

    1. Risk-per-trade (primary):      risk$ = equity x risk_pct
       units = risk$ / (entry x stop_pct)
    2. Kelly ceiling (never exceed):  f* = p - (1-p)/R with p=0.60* (observed
       directional accuracy has been 60-66% in trend-following mode; R = take/stop
       = 2 at 4/8). Half-Kelly -> cap = equity x f_halved / (entry x stop_pct).
    3. Vol-targeting (adaptive):      units x (target_vol / realized_vol), capped
       halve when realized > 2x target.
    * p is a documented prior from the running audit, not a claim of skill.
    """
    risk_amt = equity_val * RISK_PER_TRADE_PCT / 100.0
    stop_dist = entry_price * stop_pct / 100.0
    if stop_dist <= 0:
        return 0.0, "stop_dist<=0"
    units = risk_amt / stop_dist

    # Kelly ceiling: p prior 0.60, R = take_pct/stop_pct (>=2 typical)
    R = max(1.0, take_pct / stop_pct) if take_pct and stop_pct else 2.0
    p_prior = 0.60
    f_star = p_prior - (1 - p_prior) / R
    f_used = max(0.0, f_star * KELLY_FRACTION)
    kelly_units = (equity_val * f_used) / stop_dist if f_used > 0 else float("inf")
    # vol-target cap
    vol_mult = 1.0
    if realized_vol is not None and realized_vol > TARGET_VOL_ANNUAL * 1.5:
        vol_mult = TARGET_VOL_ANNUAL / realized_vol
        vol_mult = max(0.25, min(1.0, vol_mult))

    units = min(units, kelly_units) * vol_mult
    return units, (f"risk {RISK_PER_TRADE_PCT:.1f}% -> {units:.6f} units "
                   f"(kelly {f_used:.2f}, vol x{vol_mult:.2f})")


# REACHABLE + COST-AWARE stop/target levels for INTERDAY trading.
# Two constraints (both measured 2026-09-04 on live data):
#  1. REACHABILITY: BTC daily vol ~2-3% -> 3-8% price moves resolve in
#     hours-to-days; the old 4-24% take floors produced targets that needed
#     weeks and never hit.
#  2. COST SURVIVAL: the platform's 0.5% per-fill fee = ~1.05% round trip.
#     Backtests show every trade with a target under ~3.2% (costs > 1/3 of
#     the target) is net-negative regardless of entry quality - so the take
#     floor is 3.2% and R stays >= 2.0 to keep the required win rate <= ~47%.
# These are PRICE distances: at venue leverage (2-16x, liq-safe) they map to
# double-digit PnL on margin while targets stay reachable.
INTERDAY_STOP_MIN_PCT = float(os.getenv("LIVE_AGENT_STOP_MIN_PCT", "2.0"))
# 4% ceiling: keeps the 13x conviction-leverage floor liq-safe on 20x markets
# (liq at 13x = 5.19% out; 5.19/4 = 1.3x buffer). 10x markets cap at 4% too
# (liq at 10x = 5% out, exactly 1.25x buffer).
CONVICTION_LEV_STOP_CAP = float(os.getenv("LIVE_AGENT_STOP_MAX_PCT", "4.0"))
INTERDAY_STOP_MAX_PCT = CONVICTION_LEV_STOP_CAP
INTERDAY_TAKE_MIN_PCT = float(os.getenv("LIVE_AGENT_TAKE_MIN_PCT", "3.2"))
INTERDAY_TAKE_MAX_PCT = float(os.getenv("LIVE_AGENT_TAKE_MAX_PCT", "12.0"))
INTERDAY_MIN_RR = float(os.getenv("LIVE_AGENT_MIN_RR", "2.0"))


def clamp_risk_levels(stop_pct: float, take_pct: float) -> tuple[float, float]:
    """Clamp scenario stop/take percentages into cost-aware, liq-safe bounds.

    stop: [2%, 4%] - the 4% ceiling keeps the 13x conviction-leverage floor
    liq-safe on 20x markets (liquidation at 13x sits 5.19% against entry,
    5.19/4 = 1.3x buffer, MMR = IMR/2 verified live 2026-09-04).
    take: [3.2%, 12%] (>= 3x the 1.05% round-trip cost) and always >= 2R so
    the required win rate stays at or below the measured trend-confirmed
    ceiling (~47%).
    """
    stop = max(INTERDAY_STOP_MIN_PCT, min(float(stop_pct or 3.0), CONVICTION_LEV_STOP_CAP))
    take = max(INTERDAY_TAKE_MIN_PCT, min(float(take_pct or stop * INTERDAY_MIN_RR),
                                          INTERDAY_TAKE_MAX_PCT))
    if take < stop * INTERDAY_MIN_RR:
        take = min(stop * INTERDAY_MIN_RR, INTERDAY_TAKE_MAX_PCT)
    return round(stop, 2), round(take, 2)


def balance_aware_size(equity_val: float, cash: float, entry_price: float,
                       stop_pct: float, symbol: str,
                       market: str = "crypto",
                       conviction: float = 0.0,
                       p_win: float = 0.0) -> tuple[float, float, str]:
    """Conviction-scaled position sizing + leverage for a perp trade.

    PERP SIZING MODEL (not stock-style equity risk):
      - The AMOUNT scales with conviction: higher conviction = larger position.
        Base exposure = 15% of balance; every doubling of conviction above the
        floor adds exposure up to the hard 45% cap.
      - The LEVERAGE scales with conviction (13x floor on 20x markets) up to
        the USER'S Settings leverage cap, liq-safety clamped for the stop.
      - Hard caps: notional NEVER exceeds 45% of the total balance, and the
        margin required (notional/lev) must fit inside the wallet cash.

    Returns (units, leverage, attribution).
    """
    balance = max(float(cash or 0), float(equity_val or 0), 0.0)
    if entry_price <= 0 or balance <= 0:
        return 0.0, 1.0, "no balance or price"
    # CONVICTION-SCALED EXPOSURE: base 15% at the floor; +15% per conviction
    # doubling (floor 0.0008 -> 15%, 0.0016 -> 30%, 0.0032+ -> 45% cap).
    floor = max(CONVICTION_FLOOR, 0.0008)
    if conviction > 0:
        doubling = max(0.0, min(2.0, abs(conviction) / floor))
        exposure = min(0.45, 0.15 * (1.0 + doubling))
    else:
        # No conviction supplied (cooldown/deterministic path): use p_win as a
        # weak proxy - higher win probability gets more size, capped at 45%.
        if p_win > 0:
            exposure = min(0.45, 0.15 + max(0.0, p_win - 0.30) * 2.0)
        else:
            exposure = 0.15
    notional = balance * exposure
    units = notional / entry_price
    # LEVERAGE MEASURED BY CONVICTION — TRANSPARENT: scales from the 13x
    # floor toward the USER'S Settings leverage as the hard CAP (never
    # beyond it), then liq-safety clamped vs this trade's stop. Every
    # decision message shows the exact value chosen and why.
    # - 20x-cap markets (BTC/ETH/SOL): conviction scaling 13x -> user cap
    # - 10x-cap markets (SUI/XRP/HYPE): liq-safe margin-fit, no 13x floor
    # Loss per stop-out = notional x stop% is unchanged by leverage -
    # leverage only moves the liquidation line, and clamp_leverage keeps
    # it beyond stop x 1.25 (verified: at 13x liq sits 5.19% out vs the 4%
    # stop cap; at 20x 2.5% vs tight conviction stops).
    lev = conviction_leverage(symbol, market, conviction, stop_pct=stop_pct,
                              user_cap=LIVE_AGENT_LEVERAGE)
    # margin must still fit inside the balance at this leverage
    if lev > 1 and notional > balance * 0.95 * lev:
        units = (balance * 0.95 * lev) / entry_price
        notional = units * entry_price
    # hard cap: notional <= 45% of total balance (MAX_POSITION_PCT = 45)
    if notional > balance * MAX_POSITION_PCT / 100.0:
        units = (balance * MAX_POSITION_PCT / 100.0) / entry_price
        notional = units * entry_price
    return units, lev, (f"conviction-size {units:.6f}u (~${notional:,.0f} notional = "
                        f"{exposure*100:.0f}% of ${balance:,.0f} balance @ "
                        f"{lev:g}x lev (conviction {conviction:.3f}, your cap "
                        f"{min(LIVE_AGENT_LEVERAGE, AFTERMATH_MAX_LEVERAGE.get(symbol.upper(), 10)):g}x))")


def market_stats(symbol: str, market: str) -> dict:
    """Regime + trend read from the agent's 5m window (candidates for the gate).

    - trend_ratio: |1h net change|/100 as a decimal bias (sign = direction)
    - regime: documented classifier (bull | bear | sideways) from 4h lookback:
        forward-implied 20-bar move > +0.2% bullish, < -0.2% bearish else sideways
      (vol classifier omitted here - keep the gate deterministic and cheap).
    """
    try:
        if market == "crypto":
            import requests as _r
            now_ms = int(time.time() * 1000)
            r = _r.post("https://api.hyperliquid.xyz/info", json={
                "type": "candleSnapshot",
                "req": {"coin": symbol, "interval": "5m",
                        "startTime": now_ms - 4 * 3600 * 1000, "endTime": now_ms},
            }, timeout=15)
            closes = [float(c["c"]) for c in r.json()]
        else:
            import yfinance as yf
            ticker = f"{symbol}=X" if market == "forex" else symbol
            df = yf.download(ticker, period="5d", interval="5m", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            closes = [float(v) for v in df["Close"].dropna().tolist()]
        if len(closes) < 12:
            return {"regime": "sideways", "trend_ratio": 0.0, "closes": closes, "realized_vol": None}
        trend = closes[-1] / closes[-12] - 1  # ~1h reference
        lookback = closes[-48] if len(closes) >= 48 else closes[0]
        move20 = closes[-1] / lookback - 1
        if move20 > 0.002:
            regime = "bull"
        elif move20 < -0.002:
            regime = "bear"
        else:
            regime = "sideways"
        # realized vol (annualized from recent 5m returns) for vol-target sizing
        rv = 0.0
        try:
            rets = [(closes[i] / closes[i - 1] - 1) for i in range(max(1, len(closes) - 48), len(closes))]
            if len(rets) >= 2:
                import statistics
                rv = statistics.pstdev(rets) * (12 * 24 * 365) ** 0.5
        except Exception:
            pass
        # Mean-reversion indicators (option-two hybrid): in a SIDEWAYS regime we
        # fade the move rather than chase it. z_score = how many stdevs the last
        # close is from the 20-bar mean; rsi14 = 0..100 overbought/oversold.
        z = 0.0
        rsi = 50.0
        try:
            window = closes[-20:]
            mu = sum(window) / len(window)
            sd = (sum((c - mu) ** 2 for c in window) / len(window)) ** 0.5 if len(window) > 1 else 0.0
            z = (closes[-1] - mu) / sd if sd > 0 else 0.0
            gains = losses = 0.0
            for i in range(1, len(window)):
                diff = window[i] - window[i - 1]
                if diff > 0:
                    gains += diff
                else:
                    losses -= diff
            n = len(window) - 1
            if n > 0:
                ag, al = gains / n, losses / n
                rsi = 100.0 - 100.0 / (1.0 + (ag / al if al > 0 else float("inf")))
        except Exception:
            pass
        return {"regime": regime, "trend_ratio": trend, "closes": closes,
                "realized_vol": rv if rv else None,
                "z_score": z, "rsi14": rsi}
    except Exception:
        return {"regime": "sideways", "trend_ratio": 0.0, "closes": []}


def profitability_gate(action: str, symbol: str, market: str, prices: dict,
                       positions: list, cached_data: dict) -> tuple[bool, str]:
    """Symmetric trend gate: trade direction WITH the trend, both ways.

    Rules:
      1. REGIME-DIRECTION: bull -> longs allowed (shorts blocked: against trend);
         bear -> shorts allowed (longs blocked); sideways -> both blocked (fee burn).
      2. FEE FLOOR: |expected move (1h)| >= ~0.30% before entry (churn filter vs
         0.22% round-trip cost).
      3. TREND ALIGNMENT: short must align with negative 1h trend; long with positive.

    Exits (sell/cover) are NEVER blocked. Returns (allowed, reason).
    """
    if action in ("sell", "cover"):
        return True, ""  # exits always allowed

    regime = str(cached_data.get("regime", "sideways"))
    wanted_long = action == "buy"
    wanted_short = action == "short"
    if regime.startswith("bear"):
        if wanted_long:
            return False, f"regime={regime}: longs blocked in bear (trend down)"
        wanted_short = True  # shorts are the trend direction
    elif regime.startswith("bull"):
        if wanted_short:
            return False, f"regime={regime}: shorts blocked in bull (trend up)"
        wanted_long = True
    else:
        # OPTION-TWO HYBRID: in a SIDEWAYS regime we do NOT chase momentum (that
        # bleeds fee). Instead we MEAN-REVERT: fade a stretched move toward the
        # range mean. This is the 65-75% win-rate low-SB regime the 2026
        # literature (Vantixs/Lunefi/quantinsti) flags as high-value but only
        # while the market is genuinely ranging, never in a trend.
        tr = cached_data.get("trend_ratio")
        z = cached_data.get("z_score", 0.0)
        rsi = cached_data.get("rsi14", 50.0)
        # Only fade when the range is intact (small 1h drift) and the bar is
        # genuinely stretched. z<0 / RSI<30 -> mean-revert LONG; z>0 / RSI>70
        # -> mean-revert SHORT.
        choppy = (tr is None) or (abs(tr) < MEAN_REV_TREND_CAP)
        stretched_long = (z <= -MEAN_REV_Z and rsi <= MEAN_REV_RSI_HI) or (z <= -MEAN_REV_Z_HARD)
        stretched_short = (z >= MEAN_REV_Z and rsi >= MEAN_REV_RSI_LO) or (z >= MEAN_REV_Z_HARD)
        if not choppy:
            return False, f"regime={regime}: 1h drift {tr*100:+.2f}% too strong for mean-reversion (avoid fading a trend)"
        if wanted_long and stretched_long:
            return True, f"regime={regime}: mean-revert LONG (z={z:.2f}, rsi={rsi:.0f})"
        if wanted_short and stretched_short:
            return True, f"regime={regime}: mean-revert SHORT (z={z:.2f}, rsi={rsi:.0f})"
        return False, f"regime={regime}: no mean-reversion setup (z={z:.2f}, rsi={rsi:.0f}; need z<=-{MEAN_REV_Z} long / z>={MEAN_REV_Z} short)"

    tr = cached_data.get("trend_ratio")
    if tr is not None:
        expected_move = abs(tr) * 100
        cost_floor = (TRADE_FEE_RATE * 4) * 100  # 0.4% round trip incl. slippage
        if 0 < expected_move < cost_floor * 0.75:
            return False, f"expected move {expected_move:.2f}% too small vs risk (no-trade zone)"
        # trend alignment: long wants tr > 0, short wants tr < 0
        aligned = (tr > 0 if wanted_long else tr < 0)
        if not aligned and abs(tr) > 1e-6:
            return False, f"1h trend {tr*100:+.2f}% opposes requested {('long' if wanted_long else 'short')} (trend alignment)"
    return True, ""


# ---------------------------------------------------------------- skills (loaded before LLM)


def _load_skill_context() -> str:
    """Load the strategy skills into the LLM context BEFORE any decision.

    Reads the repo's skills (momentum + funding-carry), condenses each to its
    binding spec, and returns a prompt block. This is the 'skill is loaded
    before anything' guarantee - the LLM never decides without seeing the
    strategy it is implementing.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]  # agent/ -> service/ -> repo root
    skills_root = repo_root / "skills"
    wanted = {"momentum", "funding-carry"}
    blocks = []
    for name in ("momentum", "funding-carry"):
        path = skills_root / name / "SKILL.md"
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        lines = []
        capture = False
        for line in text.splitlines():
            if line.strip().startswith("## ") or line.strip().startswith("# "):
                capture = line.strip().startswith("## ")  # capture sections, skip title
            if not capture:
                continue
            lines.append(line)
        body = "\n".join(lines).strip()
        # collapse blank runs
        body = re.sub(r"\n{3,}", "\n\n", body)
        # keep it bounded so we don't blow the context window
        if len(body) > 4000:
            body = body[:4000] + "\n…[truncated]"
        blocks.append(f"=== SKILL: {name.upper()} ===\n{body}")
    if not blocks:
        return ""
    return "\n\n".join(blocks)


def _opencode_completion(system: str, user: str) -> dict:
    """Use the opencode-go gateway (our own model) via the `opencode run` CLI.

    This bypasses OpenRouter entirely - no external key, no free-tier rate
    limit. The gateway model is set by MODEL (opencode-go/deepseek-v4-flash).
    """
    try:
        import subprocess as _sp
        proc = _sp.run(
            ["opencode", "run", "-m", MODEL, f"{system}\n\n{user}"],
            capture_output=True, text=True, timeout=120,
        )
        out = (proc.stdout or "").strip()
        if not out:
            return {"action": "hold", "quantity": 0,
                    "reasoning": "opencode-gateway: empty response"}
        start, end = out.find("{"), out.rfind("}")
        if start == -1 or end == -1:
            return {"action": "hold", "quantity": 0,
                    "reasoning": f"parse-failed: {out[:120]}"}
        return json.loads(out[start:end + 1])
    except Exception as exc:  # noqa: BLE001
        return {"action": "hold", "quantity": 0,
                "reasoning": f"opencode-gateway error: {exc}"}


def _provider_completion(system: str, user: str) -> dict:
    """Provider-aware JSON decision call (Claude + OpenAI-compatible).

    - claude:     Anthropic /v1/messages, x-api-key header (NOT OpenAI shape)
    - openai / openrouter / deepseek / custom: OpenAI /chat/completions,
      Bearer auth. OpenRouter uses the same shape; deepseek-chat uses the
      OpenAI-compatible endpoint.
    The active model may be a reasoning model that spends tokens on `reasoning`
    before emitting the final JSON in `content`. We pass a generous budget +
    low temperature, and fall back to a safe hold if content is empty or
    unparseable.
    """
    # No external API key -> use the opencode-go gateway (our own model) via the
    # `opencode run` CLI, which is not rate-limited by OpenRouter.
    if not LIVE_AGENT_API_KEY:
        return _opencode_completion(system, user)
    import requests as _requests

    prov = (LIVE_AGENT_PROVIDER or "openai").strip().lower()

    # ---- Anthropic (Claude): /v1/messages + x-api-key ----
    if prov == "claude":
        base = (LIVE_AGENT_BASE_URL or "https://api.anthropic.com/v1").rstrip("/")
        url = f"{base}/messages"
        headers = {
            "x-api-key": LIVE_AGENT_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": MODEL,
            "max_tokens": int(os.getenv("LIVE_AGENT_MAX_TOKENS", "4000")),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            resp = _requests.post(url, headers=headers, json=body, timeout=300)
            if resp.status_code != 200:
                return {"action": "hold", "quantity": 0,
                        "reasoning": f"llm-http-{resp.status_code}: {resp.text[:120]}"}
            data = resp.json()
            content = ""
            for block in data.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    content = block["text"]
                    break
            if not content or not content.strip():
                return {"action": "hold", "quantity": 0,
                        "reasoning": "llm-empty: claude returned no text"}
        except Exception as exc:  # noqa: BLE001
            return {"action": "hold", "quantity": 0, "reasoning": f"llm-error: {exc}"}
    else:
        # ---- OpenAI / OpenRouter / DeepSeek / custom: /chat/completions ----
        base = (LIVE_AGENT_BASE_URL or "https://openrouter.ai/api/v1").rstrip("/")
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {LIVE_AGENT_API_KEY}",
                   "Content-Type": "application/json"}
        body = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        if int(os.getenv("LIVE_AGENT_MAX_TOKENS", "0")) > 0:
            # Clamp to the provider's accepted range (observed InvalidParameter
            # error when >393216 on deepseek-v4-flash via b.ai gateway).
            body["max_tokens"] = min(int(os.getenv("LIVE_AGENT_MAX_TOKENS", "0")), 393216)
        try:
            resp = _requests.post(url, headers=headers, json=body, timeout=300)
            if resp.status_code != 200:
                return {"action": "hold", "quantity": 0,
                        "reasoning": f"llm-http-{resp.status_code}: {resp.text[:120]}"}
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
            if not content or not content.strip():
                return {"action": "hold", "quantity": 0,
                        "reasoning": "llm-empty: reasoning model returned no content"}
        except Exception as exc:  # noqa: BLE001
            return {"action": "hold", "quantity": 0, "reasoning": f"llm-error: {exc}"}

    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        return {"action": "hold", "quantity": 0,
                "reasoning": f"parse-failed: {content[:120]}"}
    return json.loads(content[start : end + 1])


def ask_model(prompt: str) -> dict:
    """Ask the model for a JSON decision: provider API (user key) or opencode CLI."""
    if ACTIVE_MODE:
        system = (
            "You are an ACTIVE trader on a live trading platform (real prices, real execution). "
            "Your mandate: BE IN THE MARKET most of the time, in BOTH directions. Each cycle you "
            "pick long, short, or flat for ONE symbol based on the 5-minute trend and 30d context. "
            "Rules: hold a position at least 70% of cycles; switch long/short when the 5m trend flips; "
            "go flat only on sharp reversals or extreme uncertainty. "
            "Always set stop_loss_pct 3-8 on new entries. "
            "Trade the trend BOTH ways: in a downtrend (5m/1h down, 30d weak/negative) OPEN A SHORT "
            "rather than staying flat; in an uptrend buy. Never force a short against an uptrend or a "
            "long against a downtrend. "
            "You ALWAYS reply with a single valid JSON object, no markdown, no extra text:\n"
            '{"action":"buy|short|sell|cover|hold","symbol":"<symbol>","quantity":<number>,'
            '"stop_loss_pct":<number>,"take_profit_pct":<number>,"reasoning":"<1 sentence>"}\n'
            "buy=OPEN a long, short=OPEN a short, sell=close a long, cover=close a short, "
            "hold=stay as you are. Greed: 1. Do not stop trading a winning trend because you are already "
            "exposed - scaling in is allowed up to the cap. "
            "Use quantity that respects the position cap shown in the prompt."
        )
    else:
        system = (
            "You are a disciplined crypto futures trading agent. "
            "You ALWAYS reply with a single valid JSON object, no markdown, no extra text:\n"
            '{"action":"buy|sell|hold","symbol":"BTC|ETH","quantity":<number>,"stop_loss_pct":<number|0>,'
            '"take_profit_pct":<number|0>,"reasoning":"<1-2 sentences>"}\n'
            "Rules: action=hold means no trade. quantity 0 for hold. "
            "When buying, always set stop_loss_pct between 3 and 15. "
            "Do not overtrade. Prefer cash in uncertainty. Never chase after big green candles."
        )
    if LIVE_AGENT_API_KEY:
        return _provider_completion(system, prompt)
    cmd = ["opencode", "run", "-m", MODEL, f"{system}\n\n{prompt}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = (proc.stdout or "").strip()
    # strip any code fences / trailing prose
    start, end = out.find("{"), out.rfind("}")
    if start == -1 or end == -1:
        return {"action": "hold", "quantity": 0, "reasoning": f"parse-failed: {out[:120]}"}
    try:
        decision = json.loads(out[start : end + 1])
    except json.JSONDecodeError:
        return {"action": "hold", "quantity": 0, "reasoning": f"parse-failed: {out[start:end+1][:120]}"}
    return decision


# ---------------------------------------------------------------- risk guards

def daily_trade_count() -> int:
    if not LOG_PATH.exists():
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    count = 0
    with open(LOG_PATH, encoding="utf-8") as f:
        next(f, None)  # header
        for line in f:
            if not line.startswith(today):
                continue
            parts = line.split(",")
            action = parts[3] if len(parts) > 3 else (parts[2] if len(parts) > 2 else "")
            fill_ok = parts[8] if len(parts) > 8 else (parts[7] if len(parts) > 7 else "")
            if action in ("buy", "sell", "short", "cover") and fill_ok.strip() == "True":
                count += 1
    return count


def traded_symbols_today() -> set[str]:
    """Symbols that already got a FILLED trade today (either direction).

    One trade per token per day: after the bot opens AND fills a position on a
    symbol, that symbol is off-limits until tomorrow - the agent moves on to
    the next watched token instead of flipping direction on the same token.
    """
    traded: set[str] = set()
    if not LOG_PATH.exists():
        return traded
    today = datetime.now(timezone.utc).date().isoformat()
    with open(LOG_PATH, encoding="utf-8") as f:
        next(f, None)  # header
        for line in f:
            if not line.startswith(today):
                continue
            parts = line.split(",")
            symbol = (parts[1] if len(parts) > 1 else "").strip().upper()
            action = parts[3] if len(parts) > 3 else (parts[2] if len(parts) > 2 else "")
            fill_ok = parts[8] if len(parts) > 8 else (parts[7] if len(parts) > 7 else "")
            if symbol and action in ("buy", "sell", "short", "cover") and fill_ok.strip() == "True":
                traded.add(symbol)
    return traded


# DIRECTION LOCK + ONE-SHOT RULE (one good trade discipline, web-validated
# 2026-09-04: OneTradeJournal one-trade-a-day - a stop-out ends the session
# for that symbol; re-entry or direction flip on the same day is a violation).
# ONE attempt per symbol per day: pick the best setup, take the shot, then
# make NO further decisions on that asset - win or lose it waits until
# tomorrow. (Env-tunable for operators who want a retry.)
MAX_ENTRY_ATTEMPTS = int(os.getenv("LIVE_AGENT_MAX_ENTRY_ATTEMPTS", "1"))


def entry_directions_today() -> dict[str, set[str]]:
    """{symbol: {directions attempted today}} - filled or not. The direction
    a symbol was last attempted with is the only one allowed for the day."""
    dirs: dict[str, set[str]] = {}
    if not LOG_PATH.exists():
        return dirs
    today = datetime.now(timezone.utc).date().isoformat()
    with open(LOG_PATH, encoding="utf-8") as f:
        next(f, None)
        for line in f:
            if not line.startswith(today):
                continue
            parts = line.split(",")
            symbol = (parts[1] if len(parts) > 1 else "").strip().upper()
            action = parts[3] if len(parts) > 3 else (parts[2] if len(parts) > 2 else "")
            if symbol and action in ("buy", "short"):
                dirs.setdefault(symbol, set()).add(action)
    return dirs


def entry_attempts_today(symbol: str) -> int:
    """Number of ENTRY decisions made for a symbol today (any outcome)."""
    if not LOG_PATH.exists():
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    n = 0
    with open(LOG_PATH, encoding="utf-8") as f:
        next(f, None)
        for line in f:
            if not line.startswith(today):
                continue
            parts = line.split(",")
            sym = (parts[1] if len(parts) > 1 else "").strip().upper()
            action = parts[3] if len(parts) > 3 else (parts[2] if len(parts) > 2 else "")
            if sym == symbol.upper() and action in ("buy", "short"):
                n += 1
    return n


def equity(portfolio: dict, prices: dict) -> float:
    cash = portfolio.get("cash", 100000.0)
    for p in portfolio.get("positions", []):
        px = prices.get(p["symbol"]) or p.get("current_price") or p["entry_price"]
        qty = p["quantity"]
        if qty >= 0:
            cash += qty * px
        else:
            cash += abs(qty) * (2 * p["entry_price"] - px)
    return cash


# ---------------------------------------------------------------- prediction tracking
# Per-bot isolated: each bot logs its own predictions so calibration is not
# polluted by another bot's trades.
PRED_LOG = _EXPORT_DIR / f"predictions_bot{_BOT_SLOT}.csv"


def _log_prediction(decision: dict, p_win: float, R: float, ev: float,
                    drift_annual: float, vol_annual: float,
                    entry: float = 0.0, stop: float = 0.0, target: float = 0.0) -> None:
    """Log one trade's predicted probability + levels at entry for calibration."""
    try:
        PRED_LOG.parent.mkdir(parents=True, exist_ok=True)
        fresh = not PRED_LOG.exists()
        ts = datetime.now(timezone.utc).isoformat()
        with open(PRED_LOG, "a", encoding="utf-8") as f:
            if fresh:
                f.write("ts,symbol,direction,entry,stop,target,p_win,R,ev,drift,vol,status\n")
            f.write(f"{ts},{decision.get('symbol')},"
                    f"{'long' if decision.get('action') in ('buy',) else 'short'},"
                    f"{entry},{stop},{target},"
                    f"{p_win:.4f},{R:.4f},{ev:.4f},{drift_annual:.4f},{vol_annual:.4f},open\n")
    except Exception:
        pass


# ---------------------------------------------------------------- real execution (gateway)


def _get_exec_gateway():
    """Lazy-built execution gateway. None unless LIVE_AGENT_EXECUTION=1 AND ready."""
    global _exec_gateway
    if _exec_gateway is None and EXEC_ENABLED:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "execution"))
            # Per-bot network selection: set the testnet env vars before building
            # the gateway so the adapters hit the right RPC/API endpoints.
            _network = os.getenv("LIVE_AGENT_NETWORK", "mainnet").strip().lower()
            if _network not in ("mainnet", "testnet"):
                _network = "mainnet"
            is_testnet = _network != "mainnet"
            for _chain_var in ("HL", "SOL", "SUI"):
                os.environ[f"EXEC_{_chain_var}_TESTNET"] = "1" if is_testnet else "0"
            os.environ["EXEC_SUI_NETWORK"] = _network
            from gateway import ExecGateway
            gw = ExecGateway.build()
            if gw.ready:
                gw.provision_all_wallets(EXEC_BOT_ID)
                print(f"[exec] gateway ready: chains={list(gw.adapters.keys())}")
                _exec_gateway = gw
                _expand_universe_from_gateway(gw)
            else:
                print("[exec] LIVE_AGENT_EXECUTION=1 but gateway not ready (keys not configured) - staying paper")
        except Exception as exc:
            print(f"[exec] gateway init failed (staying paper): {exc}")
            _exec_gateway = None
    return _exec_gateway


def _expand_universe_from_gateway(gw) -> None:
    """When the active perp venue is Sui/Aftermath, watch that chain's top 5
    assets (long + short) so the agent analyzes tokens actually available on
    the user's chosen chain. Best-effort: any failure leaves the configured
    universe untouched."""
    try:
        chain = os.getenv("LIVE_AGENT_CHAIN", "sui").strip().lower()
        if "sui" not in gw.adapters and chain == "sui":
            # gateway not ready; fall back to the default Aftermath top-5
            return
        aftermath = getattr(gw.adapters.get("sui"), "aftermath", None)
        listed = []
        if aftermath is not None:
            listed = [m.get("base", "").upper() for m in aftermath.markets() if m.get("base")]
            listed = [s for s in listed if s][:10]
        if not listed:
            # static fallback for the chain's known perp markets
            listed = {
                "sui": ["BTC", "ETH", "SOL", "SUI", "HYPE"],
                "solana": ["BTC", "ETH", "SOL", "SUI", "DOGE"],
                "hyperliquid": ["BTC", "ETH", "SOL", "SUI", "HYPE"],
            }.get(chain, ["BTC", "ETH"])
        # only override when the user has not pinned an explicit perp universe
        pinned = [s for s, m in UNIVERSE if m == "crypto"]
        if pinned:
            return
        top5 = listed[:5]
        current = {s.upper() for s, _ in UNIVERSE}
        added = 0
        for sym in top5:
            sym = sym.upper()
            if sym not in current:
                UNIVERSE.append((sym, "crypto"))
                current.add(sym)
                added += 1
        if added:
            print(f"[exec] chain {chain}: watching top {len(top5)} perp assets "
                  f"({', '.join(top5)})")
    except Exception as exc:
        print(f"[exec] universe expansion skipped: {exc}")


def _resolve_real_venue(symbol: str, market: str, gw) -> tuple[str, str] | None:
    """(chain, venue) for a symbol/market in real mode, or None if unsupported."""
    adapters = gw.adapters
    if market == "crypto":
        if "hyperliquid" in adapters:
            return "hyperliquid", "hl-perp"
        if "solana" in adapters:
            return "solana", "jup-perp"
        if "sui" in adapters and getattr(adapters["sui"], "aftermath", None) is not None:
            return "sui", "aftermath-perp"
        return None
    if market == "us-stock":
        if "solana" in adapters:
            return "solana", "xstocks-spot"
        return None
    return None  # forex is COMING-SOON on all chains


def get_real_portfolio(gw, bot_id: int) -> dict:
    """Positions + cash from the execution ledger (synced from on-chain).

    'cash' is the trading equity available to size new trades: wallet USDC
    PLUS Aftermath perp collateral PLUS unrealized PnL. The perp deposits live
    inside the Aftermath account, not the wallet, so counting only on-chain
    wallet USDC made the agent conclude 'no balance or price' the moment funds
    were deposited to the venue. Any read failure degrades to the ledger value.
    """
    positions, cash = [], 0.0
    for chain in gw.adapters:
        try:
            gw.sync(bot_id, chain)
        except Exception:
            pass
        wallet = gw.ledger.wallet_by_bot_chain(bot_id, chain)
        if not wallet:
            continue
        # Real Aftermath balance (perp collateral + unrealized PnL) if the
        # gateway wired an aftermath adapter for this chain.
        aftermath = None
        try:
            aftermath = getattr(gw.adapters.get(chain), "aftermath", None)
        except Exception:
            aftermath = None
        if aftermath is not None:
            try:
                cash += float(aftermath.collateral() or 0.0)
                pos = aftermath.positions()
                rows = pos.get("data") or []
                if isinstance(rows, dict):
                    rows = rows.get("positions") or []
                for p in rows if isinstance(rows, list) else []:
                    try:
                        cash += float((p or {}).get("unrealizedPnl") or 0.0)
                    except Exception:
                        pass
            except Exception:
                pass
        state = gw.ledger.load_chain_state(wallet["id"]) or {}
        balances = state.get("balances") or {}
        ledger_cash = float(balances.get("USDC") or balances.get("USD") or 0.0)
        # Fallback: ledger can be stale (0) after a fresh sync or if GraphQL
        # objects were empty. Use the adapter's direct on-chain USDC balance
        # and sui_equity as authoritative when ledger shows 0.
        if ledger_cash == 0:
            try:
                ad = gw.adapters.get(chain)
                if ad is not None and hasattr(ad, "get_balance"):
                    onchain = float(ad.get_balance("USDC") or 0.0)
                    if onchain > 0:
                        ledger_cash = onchain
            except Exception:
                pass
        if ledger_cash == 0:
            try:
                from sui_equity import sui_equity as _eq
                # need bot dict for sui_equity - reconstruct minimal
                _b = {"wallet_addr": wallet.get("address") or "", "network": getattr(gw.adapters.get(chain), "network", "mainnet")}
                _snap = _eq(_b)
                onchain2 = float(_snap.get("usdc") or 0.0) + float(_snap.get("collateral") or 0.0)
                if onchain2 > ledger_cash:
                    ledger_cash = onchain2
            except Exception:
                pass
        cash += ledger_cash
        for p in state.get("positions") or []:
            side = str(p.get("side") or p.get("coin", "")).lower()
            qty = float(p.get("qty") or p.get("szi") or 0.0)
            symbol = str(p.get("symbol") or p.get("coin") or "?")
            positions.append({
                "symbol": symbol,
                "side": "long" if qty > 0 else "short",
                "quantity": qty,  # signed: positive long, negative short
                "entry_price": float(p.get("entry") or p.get("entry_px") or 0.0),
                "current_price": None,
                "chain": chain,
            })
    return {"cash": cash, "positions": positions}


def route_real_order(gw, bot_id: int, symbol: str, market: str, action: str,
                     qty: float, stop_pct: float, take_pct: float,
                     ref_price: float, leverage: float) -> dict:
    """Route one agent decision through the execution gateway (real venue).

    ENTRY IS A LIMIT ORDER at a small offset inside the current market so the
    fill happens immediately (and gets maker priority + maker fee when it
    rests). Research: limit entries avoid the bid-ask spread cost entirely and
    often qualify for reduced maker fees (professional traders default to
    limits on entries). The offset is ~ENTRY_OFFSET_BPS below/above the
    reference price - small enough to fill instantly, but it no longer depends
    on the exact price the LLM saw (BTC may have moved since the scenario).

    CLOSES are REDUCE-ONLY limit orders placed to CROSS the spread (sell
    slightly below market, buy slightly above): a reduce-only resting order
    can never flip the position, and a crossing price guarantees the exit
    fills immediately when protecting capital is the priority.
    """
    resolved = _resolve_real_venue(symbol, market, gw)
    if not resolved:
        return {"ok": False, "error": f"no real venue for {symbol} [{market}]"}
    chain, venue = resolved
    is_entry = action in ("buy", "short")
    side = "buy" if action in ("buy", "cover") else "sell"
    # leverage clamped to venue cap AND liquidation-safe vs this stop distance
    lev = clamp_leverage(symbol, market, leverage, stop_pct=stop_pct if is_entry else None)
    _entry_off = ENTRY_OFFSET_BPS / 10000.0
    if is_entry:
        # cross-inside slightly: fills immediately, earns maker pricing on the rest
        limit_price = round(ref_price * (1 - _entry_off) if side == "buy"
                            else ref_price * (1 + _entry_off), 6)
    else:
        # CLOSE: cross the spread so the exit cannot rest unfilled
        limit_price = round(ref_price * (1 - _entry_off) if side == "sell"
                            else ref_price * (1 + _entry_off), 6)
    intent_kw = dict(
        chain=chain, venue=venue, symbol=symbol, side=side, qty=qty,
        order_type="limit",
        limit_price=limit_price,
        reduce_only=not is_entry,
        # closes are always 1x with no stop/target re-armed
        leverage=lev if is_entry else 1.0,
        idempotency_key=(
            f"agent:{os.getenv('LIVE_AGENT_NAME', 'agent')}:{symbol}:{action}:{int(time.time() * 1000)}"
        ),
    )
    if is_entry:
        if stop_pct:
            intent_kw["stop_loss"] = round(
                ref_price * (1 - stop_pct / 100) if action == "buy"
                else ref_price * (1 + stop_pct / 100), 6)
        if take_pct:
            intent_kw["take_profit"] = round(
                ref_price * (1 + take_pct / 100) if action == "buy"
                else ref_price * (1 - take_pct / 100), 6)
    from order_model import OrderIntent
    intent = OrderIntent(**intent_kw)
    fill = gw.route_and_sync(bot_id, intent, ref_price)
    if isinstance(fill, dict):
        fill.setdefault("order_type", "limit")
        fill.setdefault("limit_price", intent_kw.get("limit_price"))
    return fill


# ---------------------------------------------------------------- log

# Optional direct-to-user error notifications: when a fill fails the agent can
# push a human-friendly message to the bot's own Telegram chat. Empty token =
# silent (standalone run, or token not injected by the bot network).
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")


def humanize_error(raw: str, max_len: int = 300) -> str:
    """Small local copy of the bot's error mapper (kept import-free)."""
    raw = (raw or "").strip()
    if not raw:
        return "Unknown error - no trade was placed."
    lowered = raw.lower()
    for needle, friendly in (
        ("Short position entry price is missing",
         "Couldn't open the short - the platform needs an entry price for shorts. "
         "The bot will keep trying with a valid price."),
        ("stop_loss_pct/take_profit_pct can only be set when opening (buy/short)",
         "Closing trades can't carry a stop/target - the close was sent safely without one."),
        ("market is currently closed", "That market is closed right now - the bot will retry when it reopens."),
        ("US market is closed", "US stocks only trade Mon–Fri 9:30–16:00 ET."),
        ("daily trade limit reached", "Today's trade limit is hit. Your bot resumes tomorrow."),
        ("position size cap exceeded", "The position was too large for your risk settings - the bot stayed flat."),
        ("rate limit", "The trading venue is rate-limiting. The bot will retry shortly."),
        ("timeout", "The trading venue timed out. The bot will retry."),
    ):
        if needle.lower() in lowered:
            return friendly
    if len(raw) > max_len:
        return f"⚠️ {raw[:max_len]}…"
    return f"⚠️ {raw}"


# Dedup: only surface each error kind once per window. Stored in a shared
# file (not in-memory) so even multiple concurrent agent processes cannot send
# the same message twice - each process checks+claims the lock file atomically.
_last_notified: dict[str, float] = {}
_NOTIFY_LOCK = threading.Lock()
_NOTIFY_LOCK_FILE = Path(__file__).resolve().parents[1] / "agent" / ".notify_dedup.json"
NOTIFY_DEDUP_SECONDS = int(os.getenv("LIVE_AGENT_NOTIFY_DEDUP", "3600"))


def _notify_claim(key: str) -> bool:
    """Atomically claim a notification key. Returns True only if no OTHER
    process (or this one) has claimed it within NOTIFY_DEDUP_SECONDS."""
    now = time.time()
    with _NOTIFY_LOCK:
        if now - _last_notified.get(key, 0.0) < NOTIFY_DEDUP_SECONDS:
            return False
        try:
            stamps: dict = {}
            if _NOTIFY_LOCK_FILE.exists():
                try:
                    stamps = json.loads(_NOTIFY_LOCK_FILE.read_text(encoding="utf-8"))
                except Exception:
                    stamps = {}
            # Prune stale keys so the file stays small.
            stamps = {k: v for k, v in stamps.items() if now - v < NOTIFY_DEDUP_SECONDS * 2}
            if now - stamps.get(key, 0.0) < NOTIFY_DEDUP_SECONDS:
                return False
            stamps[key] = now
            _NOTIFY_LOCK_FILE.write_text(json.dumps(stamps), encoding="utf-8")
            _last_notified[key] = now
            return True
        except Exception:
            # If the file can't be written, fall back to in-memory only.
            _last_notified[key] = now
            return True


def _schedule_delete(bot_token: str, chat_id: int, message_id: int,
                     ttl: int = 300) -> None:
    """Delete a sent Telegram message after ttl seconds (background thread)."""
    def _delete():
        time.sleep(ttl)
        try:
            import requests as _r
            _r.post(f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                    json={"chat_id": chat_id, "message_id": message_id}, timeout=10)
        except Exception:
            pass
    threading.Thread(target=_delete, daemon=True).start()


def notify_error(message: str, kind: str = "error") -> None:
    """Push one human-friendly message to the user's bot chat (best-effort).

    kind: 'error' | 'rate_limit' | 'llm' | 'venue' - picks the right copy so
    the user knows exactly what's happening (AI key rate-limited vs venue
    down vs a trade rejected). Each (kind, message) is sent AT MOST once per
    NOTIFY_DEDUP_SECONDS via a shared lock file, so no message is ever
    duplicated - even by concurrent agent processes. The chat message
    auto-deletes after 3 minutes (TTL) to keep the chat clean.
    """
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    key = f"{kind}:{humanize_error(message)[:60]}"
    if not _notify_claim(key):
        return
    text = humanize_error(message)
    if kind == "rate_limit":
        text = ("⏳ <b>Your AI key hit a rate limit</b>\n\n"
                "The model provider is limiting requests (free-tier daily cap "
                "or too many calls). Your bot is holding - it will keep running "
                "on the quantitative engine until the limit resets.\n\n"
                "• Free tier: daily cap resets at midnight UTC\n"
                "• Add credits / switch models to lift the cap")
    elif kind == "llm":
        text = (f"🧠 <b>AI model hiccup</b>\n\n{humanize_error(message)}\n\n"
                "Your bot stays safe - it falls back to the quant engine and "
                "keeps trading. No fake trades.")
    try:
        import requests as _r
        r = _r.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if r.status_code == 200:
            mid = r.json().get("result", {}).get("message_id")
            if mid:
                _schedule_delete(TG_BOT_TOKEN, TG_CHAT_ID, mid)
    except Exception:
        pass


def notify_trade(symbol: str, action: str, qty: float, price: float,
                 stop_pct: float, take_pct: float, leverage: float,
                 reasoning: str = "") -> None:
    """Pop-up to the user right BEFORE a real/paper order goes out: the trade
    the LLM + quant just decided on. Uses the shared dedup lock so repeated
    cycles never double-post the same symbol/action/direction signal. The chat
    message auto-deletes after 3 minutes (TTL)."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    side = "LONG" if action in ("buy", "cover") else "SHORT"
    kind = "trade"
    key = f"{kind}:{symbol}:{action}"
    if not _notify_claim(key):
        return
    pad = f"{reasoning[:140]}" if reasoning else ""
    text = (
        f"🎯 <b>Neko-Chan decided: {side} {_esc(symbol)}</b>\n\n"
        f"   🚦 LIMIT <b>{qty:g}</b> {_esc(symbol)} @ <code>${price:,.4f}</code>\n"
        f"   Leverage <b>{leverage:g}x</b> · Stop {stop_pct:.1f}% · Take {take_pct:.1f}%\n"
        f"{f'   {_esc(pad)}' if pad else ''}"
    )
    try:
        import requests as _r
        r = _r.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if r.status_code == 200:
            mid = r.json().get("result", {}).get("message_id")
            if mid:
                _schedule_delete(TG_BOT_TOKEN, TG_CHAT_ID, mid)
    except Exception:
        pass


def _esc(v) -> str:
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def log_decision(row: dict):
    """Append one decision row to the CSV log AND update the JSON cache.

    The CSV is the append-only research/eval record. The JSON cache holds ONLY
    the latest decision per symbol (overwritten every cycle) so Peek shows the
    current state, not accumulated history - when a new trade is selected the
    old rows are replaced, not stacked.
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_HEADER = "ts,symbol,direction,action,price,quantity,stop_pct,take_pct,fill_ok,reasoning,error,leverage\n"
        fresh = not LOG_PATH.exists()
        with open(LOG_PATH, "a+", encoding="utf-8") as f:
            if fresh:
                f.write(LOG_HEADER)
            else:
                # SCHEMA GUARD: if the file exists but its header lacks the
                # current columns (a pre-direction file), rebuild it with the
                # new header preserving every existing data row so appends
                # never shift columns again.
                f.seek(0)
                first = f.readline()
                if first.strip() != LOG_HEADER.strip():
                    import os as _os
                    body = f.read()
                    with open(LOG_PATH, "w", encoding="utf-8") as g:
                        g.write(LOG_HEADER)
                        g.write(body)  # keep all existing rows
            ts = datetime.now(timezone.utc).isoformat()
            f.write(f"{ts},{row.get('symbol')},{row.get('direction')},{row.get('action')},{row.get('price')},"
                    f"{row.get('quantity')},{row.get('stop_pct')},{row.get('take_pct')},"
                    f"{row.get('fill_ok')},\"{str(row.get('reasoning','')).replace('\"','\"\"')}\","
                    f"{str(row.get('error','')).replace(',',';')},{row.get('leverage')}\n")
    except Exception as exc:
        print(f"[agent log] failed to append decision row: {exc}", file=sys.stderr)

    # Cache: keep ONLY the latest decision per symbol. A hold is logged with an
    # empty symbol, so it's stored under a fixed "__hold__" key. When the agent
    # selects a new best trade the previous rows are overwritten in place.
    try:
        cache = {}
        if CACHE_PATH.exists():
            try:
                cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                cache = {}
        key = (str(row.get("symbol") or "").upper()) or "__hold__"
        cache[key] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": row.get("symbol"),
            "direction": row.get("direction"),
            "action": row.get("action"),
            "price": row.get("price"),
            "quantity": row.get("quantity"),
            "stop_pct": row.get("stop_pct"),
            "take_pct": row.get("take_pct"),
            "leverage": row.get("leverage"),
            "lev_why": row.get("lev_why"),
            "fill_ok": row.get("fill_ok"),
            "reasoning": str(row.get("reasoning", "")),
            "error": str(row.get("error", "")),
        }
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[agent log] failed to update cache: {exc}", file=sys.stderr)


# ---------------------------------------------------------------- main

def fetch_5m_context(symbol: str, market: str, hours: int = 1) -> str:
    """Recent 5m candles -> compact string like 'up,up,down,flat' plus 1h change."""
    try:
        if market == "crypto":
            import requests as _r

            coin = symbol
            now_ms = int(time.time() * 1000)
            r = _r.post("https://api.hyperliquid.xyz/info", json={
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": "5m",
                        "startTime": now_ms - hours * 3600 * 1000, "endTime": now_ms},
            }, timeout=15)
            closes = [float(c["c"]) for c in r.json()]
        else:
            import yfinance as yf

            ticker = f"{symbol}=X" if market == "forex" else symbol
            df = yf.download(ticker, period="1d", interval="5m", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            closes = [float(v) for v in df["Close"].dropna().tolist()]

        if len(closes) < 3:
            return "5m: insufficient data"
        moves = []
        for i in range(1, len(closes)):
            d = (closes[i] / closes[i - 1] - 1) * 100
            moves.append("up" if d > 0.05 else ("down" if d < -0.05 else "flat"))
        last = "".join("▲" if m == "up" else ("▼" if m == "down" else "·") for m in moves[-12:])
        chg1h = (closes[-1] / closes[0] - 1) * 100
        return f"5m[{hours}h]: {last} ({chg1h:+.2f}% over {hours}h)"
    except Exception as exc:
        return f"5m: unavailable ({exc})"


def fetch_interval_closes(symbol: str, market: str, interval: str,
                          bars: int = 200) -> list[float]:
    """Closes on an arbitrary candle interval (1h intraday, 1d swing) for the
    trend-following model. crypto -> Hyperliquid candleSnapshot (1h/1d);
    others -> yfinance. Returns [] on failure.
    """
    try:
        if market == "crypto":
            import requests as _r
            now_ms = int(time.time() * 1000)
            # startTime must span enough bars for the interval. Hyperliquid's
            # candleSnapshot uses millisecond timestamps; we need ~bars * interval_ms.
            mult = {"5m": 300000, "1h": 3600000, "4h": 4 * 3600000, "1d": 86400000}
            span_ms = bars * mult.get(interval, 3600000)
            r = _r.post("https://api.hyperliquid.xyz/info", json={
                "type": "candleSnapshot",
                "req": {"coin": symbol, "interval": interval,
                        "startTime": now_ms - span_ms,
                        "endTime": now_ms},
            }, timeout=15)
            closes = [float(c["c"]) for c in r.json()]
        else:
            import yfinance as yf
            ticker = f"{symbol}=X" if market == "forex" else symbol
            period = "1mo" if interval == "5m" else "2y"
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            closes = [float(v) for v in df["Close"].dropna().tolist()]
        return [c for c in closes if c > 0]
    except Exception:
        return []


def fetch_5m_closes(symbol: str, market: str,
                    hours: int = SCENARIO_5M_HOURS) -> list[float]:
    """Recent 5-minute closes for the scalp scenario engine's long/short read.

    crypto -> Hyperliquid candleSnapshot "5m"; others -> yfinance 5m. Returns
    [] on failure (caller falls back to the daily series).
    """
    try:
        if market == "crypto":
            import requests as _r
            now_ms = int(time.time() * 1000)
            r = _r.post("https://api.hyperliquid.xyz/info", json={
                "type": "candleSnapshot",
                "req": {"coin": symbol, "interval": "5m",
                        "startTime": now_ms - hours * 3600 * 1000,
                        "endTime": now_ms},
            }, timeout=15)
            closes = [float(c["c"]) for c in r.json()]
        else:
            import yfinance as yf
            ticker = f"{symbol}=X" if market == "forex" else symbol
            df = yf.download(ticker, period="1d", interval="5m",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            closes = [float(v) for v in df["Close"].dropna().tolist()]
        return [c for c in closes if c > 0]
    except Exception:
        return []


def market_open(market: str) -> bool:
    """Client-side market-hours pre-check (platform enforces regardless)."""
    from datetime import time as dtime

    if market == "crypto":
        return True
    try:
        from zoneinfo import ZoneInfo
    except Exception:
        return True
    now = datetime.now(ZoneInfo("America/New_York"))
    day, minutes = now.weekday(), now.hour * 60 + now.minute
    if market == "us-stock":
        return day < 5 and 570 <= minutes < 960
    if market == "forex":
        if day == 4 and minutes >= 17 * 60:
            return False
        if day == 5:
            return False
        if day == 6 and minutes < 17 * 60:
            return False
        return True
    return True


def run_cycle(token: str, dry: bool = False) -> None:
    global _last_llm_at
    now_iso = datetime.now(timezone.utc).isoformat()
    gw = _get_exec_gateway() if TRADING_MODE == "live" else None
    if TRADING_MODE == "paper":
        # PAPER MODE: portfolio from the paper store (virtual $1,000 start).
        # Positions come from paper_positions; cash = free virtual USDC.
        pg = _paper_gateway()
        pf = pg.store.ensure_portfolio(EXEC_BOT_ID)
        paper_positions = []
        for pos in pg.store.positions(EXEC_BOT_ID):
            paper_positions.append({"symbol": pos["symbol"],
                                    "side": pos["direction"],
                                    "quantity": pos["qty"] if pos["direction"] == "long"
                                                else -pos["qty"],
                                    "entry_price": pos["entry_price"],
                                    "leverage": pos["leverage"],
                                    "stop_loss": pos.get("stop_loss"),
                                    "take_profit": pos.get("take_profit")})
        portfolio = {"cash": pf["cash"], "positions": paper_positions,
                     "paper": True, "starting": pf["starting"]}
        print(f"[paper] cash=${pf['cash']:,.2f} positions={len(paper_positions)}")
    elif gw:
        portfolio = get_real_portfolio(gw, EXEC_BOT_ID)
        print(f"[exec] real mode: cash=${portfolio.get('cash', 0):,.2f} "
              f"positions={len(portfolio.get('positions', []))}")
    else:
        portfolio = get_portfolio(token)
    prices = {}
    price_txt = []
    context = {}
    closes_by_symbol: dict[str, list[float]] = {}
    # When the agent trades on a specific perp chain, use that venue's direct
    # pricing for crypto perps instead of routing through Hyperliquid.
    _chain = os.getenv("LIVE_AGENT_CHAIN", "sui").strip().lower()
    for sym, market in UNIVERSE:
        try:
            px = get_price(token, sym, market)
            # Override with Aftermath pricing when trading Sui perps: the
            # platform's price_fetcher uses Hyperliquid for all crypto, but
            # Sui trades should use Aftermath's own orderbook for accurate
            # entry/exit analysis on that venue.
            if _chain == "sui" and market == "crypto":
                af = fetch_aftermath_price(sym)
                if af is not None and af > 0:
                    px = af
            prices[sym] = px
            hist = get_history(sym, market, 30)
            closes_by_symbol[sym] = [float(c) for c in hist["Close"].tolist()]
            chg = hist["Close"].pct_change()
            c24 = float(chg.iloc[-1] * 100) if len(chg) else 0.0
            c7 = float((hist["Close"].iloc[-1] / hist["Close"].iloc[-8] - 1) * 100) if len(hist) > 8 else 0.0
            c30 = float((hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100) if len(hist) else 0.0
            hi30 = float(hist["Close"].max())
            lo30 = float(hist["Close"].min())
            context[sym] = (market, c24, c7, c30, hi30, lo30)
            price_txt.append(
                f"{sym} [{market}]: ${px:,.4f} (24h {c24:+.2f}%, 7d {c7:+.2f}%, 30d {c30:+.2f}%, "
                f"30d range ${lo30:,.2f}-${hi30:,.2f})"
            )
        except Exception as e:
            price_txt.append(f"{sym}: unavailable ({e})")
        # ALWAYS respect the platform rate limit (1/sec per agent), even when a
        # price fetch just failed with 429 - otherwise the error cascades and
        # every symbol in the cycle fails (the 2026-08-28 stall).
        time.sleep(1.2)
        m5 = fetch_5m_context(sym, market, 1)
        price_txt[-1] = f"{price_txt[-1]} | {m5}"
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
            if market == "us-stock":
                from stock_sentiment import enhanced_stock_sentiment

                senti = enhanced_stock_sentiment(sym)
            else:
                from sentiment import sentiment_block

                senti = sentiment_block(market, sym)
            if senti:
                price_txt[-1] = f"{price_txt[-1]} | {senti}"
        except Exception:
            pass

    # 5-minute closes for the scalp scenario engine so 5-min momentum (not
    # 30-day daily drift) drives long vs short direction selection.
    BARS_PER_YEAR_5M = 288.0 * 365.0
    scenario_closes: dict[str, list[float]] = {}
    bpy_by_symbol: dict[str, float] = {}
    for sym, market in UNIVERSE:
        try:
            c5 = fetch_5m_closes(sym, market)
            if len(c5) >= 20:
                scenario_closes[sym] = c5
                bpy_by_symbol[sym] = BARS_PER_YEAR_5M
            else:
                scenario_closes[sym] = closes_by_symbol.get(sym, [])
                bpy_by_symbol[sym] = 365.0
        except Exception:
            scenario_closes[sym] = closes_by_symbol.get(sym, [])
            bpy_by_symbol[sym] = 365.0

    positions = portfolio.get("positions", [])
    pos_txt = "; ".join(
        f"{p.get('symbol')} {p.get('side') or ('long' if p.get('quantity', 0) > 0 else 'short')} "
        f"qty={p.get('quantity')} entry={p.get('entry_price', 0):.2f} "
        f"current={p.get('current_price') or 'n/a'} stop={p.get('stop_loss')} take={p.get('take_profit')}"
        for p in positions
    ) or "none"
    eq = equity(portfolio, prices)
    used = daily_trade_count()

    # FUNDING-CARRY CONTEXT (option-one): surface live perp funding so the model
    # can weigh opening a carry short on highly-funded symbols. Only the symbol
    # with the largest |net carry| that is ALSO genuinely high (> carry_min_apy)
    # is surfaced, to avoid noise; below the floor we suppress it (a low carry
    # short in a bull just bleeds to price - the live-test lesson).
    carry_txt = ""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from funding_carry import scan_carry
        _carry = scan_carry(eq)
        if _carry:
            top = _carry[0]
            if abs(top["net_annualized_apy"]) >= CARRY_MIN_APY:
                carry_txt = (
                    f"Funding-carry: {top['symbol']} {top['collect_side']} "
                    f"net {abs(top['net_annualized_apy'])*100:.1f}% APY "
                    f"(gross {top['funding_apy']*100:+.1f}%, collect {top['collect_side']}). "
                    "If you open this as a SHORT, it collects funding but is UNHEDGED: in a "
                    "strong bull price drift can exceed the funding you earn. Size it small."
                )
    except Exception:
        carry_txt = ""

    prompt = (
        f"Live trading decision - {now_iso} UTC.\n"
        f"Universe: {', '.join(f'{s} [{m}]' for s, m in UNIVERSE)}\n"
        f"Prices + 5m trend + sentiment: {', '.join(price_txt)}\n"
        f"{('Funding-carry: ' + carry_txt + '\n') if carry_txt else ''}"
        f"Cash: ${portfolio.get('cash', 0):,.2f} | Total equity: ${eq:,.2f}\n"
        f"Open positions: {pos_txt}\n"
        f"Trades executed today: {used}/{MAX_DAILY_TRADES}\n"
        f"Per-symbol position cap: {MAX_POSITION_PCT}% of equity\n"
        f"Sizing guide: crypto = fractional coins (e.g. 0.01-0.2), US stocks = shares (e.g. 10-100), "
        f"forex = units (e.g. 1000-10000).\n"
        f"US stocks only trade 9:30-16:00 ET weekdays; forex closes Fri 17:00 ET - Sun 17:00 ET.\n"
        f"Decision horizon: next {INTERVAL} seconds. Return your JSON decision."
    )

    open_markets = {m for _, m in UNIVERSE if market_open(m)}
    if not open_markets:
        row = {"symbol": "", "action": "hold", "price": 0, "quantity": 0,
               "stop_pct": 0, "take_pct": 0, "fill_ok": None,
               "reasoning": "no market open in universe - skipped decision", "error": "market closed"}
        print(f"[hold] all markets closed - skipped decision")
        log_decision(row)
        return

    if STRATEGY == "momentum20":
        # AGENTIC SCENARIO DECISION: the quant engine builds a LONG/SHORT
        # scenario matrix for every symbol - each with a real probability of
        # success (barrier-crossing GBM), reward/risk ratio, and expected value.
        # The LLM (with skills loaded) reads the full matrix + market context,
        # does the math compilation, and picks the highest-conviction scenario.
        # Risk guards clamp the chosen trade AFTER the LLM decides.
        _last_scenario = None
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from quant_strategy import (
                scenario_matrix, pick_best_scenario, trail_check, time_exit_check,
                partial_profit_check, rsi as _rsi_fn, RSI_ENTRY_THRESHOLD,
                momentum_confirmed,
            )

            # update trailing peak tracker for open longs (per symbol)
            for p in positions:
                sym = p.get("symbol")
                if sym and p.get("quantity", 0) > 0:
                    px = prices.get(sym) or p.get("current_price") or p.get("entry_price") or 0
                    if px > 0:
                        _trailing_high[sym] = max(_trailing_high.get(sym, 0.0), px)
            try:
                TRAILING_HIGH_PATH.parent.mkdir(parents=True, exist_ok=True)
                TRAILING_HIGH_PATH.write_text(
                    json.dumps(_trailing_high), encoding="utf-8")
            except Exception:
                pass
            for p in positions:
                sym = p.get("symbol")
                if sym and p.get("quantity", 0) > 0 and _trailing_high.get(sym):
                    p["high_price"] = _trailing_high[sym]

            # PAPER MODE: manage open paper positions' stop/target/trail
            # exits + resting limit fills through the paper gateway before
            # any new decision.
            if TRADING_MODE == "paper":
                try:
                    events = _paper_gateway().manage_exits(EXEC_BOT_ID, prices)
                    for c in events:
                        kind = c.get("kind", "exit")
                        if kind == "limit_fill":
                            print(f"[paper] LIMIT FILLED {c.get('symbol')} "
                                  f"@ {c.get('fill_price', 0):.4f}")
                            notify_trade(c.get("symbol", "?"),
                                         "buy" if c.get("direction") == "long" else "short",
                                         c.get("qty", 0) or 0,
                                         c.get("fill_price", 0) or 0, 0, 0,
                                         c.get("leverage", 1) or 1,
                                         reasoning="[paper] resting LIMIT order filled "
                                                   "at the market price")
                        else:
                            print(f"[paper] exit {c.get('symbol')} :: {c.get('reason')} "
                                  f"pnl={c.get('pnl', 0):+.4f}")
                    if any(c.get("kind") != "limit_fill" for c in events):
                        # positions changed; refresh before deciding
                        pf = _paper_gateway().store.ensure_portfolio(EXEC_BOT_ID)
                        portfolio = {"cash": pf["cash"],
                                     "positions": [
                                         {"symbol": p["symbol"],
                                          "side": p["direction"],
                                          "quantity": p["qty"] if p["direction"] == "long" else -p["qty"],
                                          "entry_price": p["entry_price"],
                                          "leverage": p["leverage"],
                                          "stop_loss": p.get("stop_loss"),
                                          "take_profit": p.get("take_profit")}
                                         for p in _paper_gateway().store.positions(EXEC_BOT_ID)],
                                     "paper": True}
                except Exception as exc:
                    print(f"[paper] exit management failed: {exc}")

            # TIME-BASED EXITS take top priority: a trade that hasn't resolved
            # within its shelf life is dead capital. Green + old = bank it;
            # any position past max_hold_minutes = hard cut at market.
            time_exits = time_exit_check(positions, prices)
            if time_exits:
                pick = time_exits[0]
                print(f"[quant] time exit: {pick.symbol} :: {pick.reasoning}")
                decision = pick.to_dict()
                decision["_forced_exit"] = True
            else:
                # SCALE OUT: if a position has reached its target, bank half and
                # let the rest trail (proven retail technique - lock profit early)
                partial = partial_profit_check(positions, prices)
                if partial:
                    pick = partial[0]
                    print(f"[quant] scale out: {pick.symbol} :: {pick.reasoning}")
                    decision = pick.to_dict()
                    decision["_forced_exit"] = True
                else:
                    # trailing-stop exits lock profits / cut losers
                    trail_exits = trail_check(positions, prices)
                    if trail_exits:
                        pick = trail_exits[0]
                        print(f"[quant] trailing-stop exit: {pick.symbol} :: {pick.reasoning}")
                        decision = pick.to_dict()
                        decision["_forced_exit"] = True
                    else:
                        # build the full scenario matrix (long + short per symbol).
                        # Uses 5-minute closes so scalp direction follows 5-min
                        # momentum, not the 30-day daily drift.
                        matrix = scenario_matrix(scenario_closes, prices,
                                                 trader_type=TRADER_TYPE,
                                                 bars_per_year=bpy_by_symbol)
                        # MOMENTUM CONFIRMATION (the proven scalp edge): LONG is
                        # favored when EMA8 > EMA21 on the 5m series, SHORT when
                        # EMA8 < EMA21. This lifts the ~36% GBM coin-flip win rate
                        # by only entering trades already moving our way. We do NOT
                        # hard-filter here: removing one direction entirely would
                        # starve the LLM of the other side and defeat the
                        # best-long/best-short choice. Momentum is instead shown to
                        # the LLM as a soft signal (see matrix_txt below) so it can
                        # weigh it against P(win) and EV.
                        momentum_ok = {}
                        for s in matrix:
                            m = scenario_closes.get(s.symbol)
                            momentum_ok[(s.symbol, s.direction, s.horizon)] = (
                                momentum_confirmed(m, s.direction) if m else False
                            )
                        # RSI momentum filter (hyperopt: PF 1.51 -> 1.66): only
                        # consider symbols whose RSI confirms direction. Longs
                        # need RSI > threshold, shorts need RSI < 100-threshold.
                        # RSI is read on the SAME 5m series the matrix was built on.
                        # MOMENTUM CONFIRMATION IS A HARD ENTRY GATE (2026-09 win
                        # -rate pass): the EMA8>EMA21 scalp momentum confirmation
                        # converts the ~36% GBM coin-flip into the measured
                        # 45%+ win rate (test_winrate.py). Only scalp scenarios
                        # whose direction is already confirmed by 5m momentum pass;
                        # trend scenarios (intraday/swing) keep their own
                        # trend_confirmed gate below.
                        matrix = [
                            s for s in matrix
                            if not scenario_closes.get(s.symbol) or
                            (s.direction == "long" and _rsi_fn(scenario_closes[s.symbol]) >= RSI_ENTRY_THRESHOLD) or
                            (s.direction == "short" and _rsi_fn(scenario_closes[s.symbol]) <= 100 - RSI_ENTRY_THRESHOLD)
                        ]
                        matrix = [
                            s for s in matrix
                            if s.horizon != "scalp"
                            or (scenario_closes.get(s.symbol)
                                and momentum_confirmed(scenario_closes[s.symbol], s.direction))
                        ]
                        # ---- TREND-FOLLOWING MODEL (intraday / swing) ----
                        # Separate from the 5m scalp engine: built on each
                        # horizon's OWN timeframe (1h for intraday, 1d for swing)
                        # so drift/vol are meaningful there. Appended to the
                        # matrix AFTER the 5m RSI filter so scalp's path is
                        # completely untouched. The LLM sees these alongside
                        # scalp scenarios and picks the best-EV horizon.
                        try:
                            from quant_strategy import build_trend_scenarios, trend_confirmed
                            _trend_kept = 0
                            for tsym, tmarket in UNIVERSE:
                                tpx = prices.get(tsym, 0)
                                if tpx <= 0:
                                    continue
                                if TRADER_TYPE == "auto" or TRADER_TYPE == "intraday":
                                    # INTERDAY = ONE GOOD MOMENTUM TRADE, decided on
                                    # the 4H chart (~6 bars/day): EMA12/26 on 4h =
                                    # the multi-session trend, not 5-minute noise.
                                    # Hard entry gates: EMA trend confirmed AND 4h
                                    # RSI confirms direction (longs >= 40, shorts
                                    # <= 60) - only A-grade setups reach the LLM.
                                    c4h = fetch_interval_closes(tsym, tmarket, "4h", 250)
                                    if len(c4h) >= 30:
                                        for ts in build_trend_scenarios(tsym, c4h, tpx, "intraday",
                                                                        bars_per_year=6 * 365):
                                            confirmed = trend_confirmed(c4h, ts.direction)
                                            r4 = _rsi_fn(c4h)
                                            rsi_ok = (r4 >= RSI_ENTRY_THRESHOLD if ts.direction == "long"
                                                      else r4 <= 100 - RSI_ENTRY_THRESHOLD)
                                            momentum_ok[(ts.symbol, ts.direction, ts.horizon)] = confirmed
                                            if confirmed and rsi_ok:
                                                matrix.append(ts)
                                                _trend_kept += 1
                            if TRADER_TYPE == "intraday":
                                print(f"[quant] 4h momentum scan: {_trend_kept} A-grade "
                                      f"scenario(s) across {len(UNIVERSE)} symbols")
                                if TRADER_TYPE == "auto" or TRADER_TYPE == "swing":
                                    c1d = fetch_interval_closes(tsym, tmarket, "1d", 200)
                                    if len(c1d) >= 30:
                                        for ts in build_trend_scenarios(tsym, c1d, tpx, "swing",
                                                                        bars_per_year=365.0):
                                            momentum_ok[(ts.symbol, ts.direction, ts.horizon)] = (
                                                trend_confirmed(c1d, ts.direction)
                                            )
                                            matrix.append(ts)
                        except Exception as _texc:
                            print(f"[quant] trend model unavailable ({_texc})")
                        has_long = {p["symbol"]: p["quantity"] > 0 for p in positions}
                        has_short = {p["symbol"]: p["quantity"] < 0 for p in positions}
                        # PURE INTRADAY MODE: the 5m-sigma scalp scenarios are
                        # NOISE for this trader type - drop them entirely so the
                        # ONLY candidates are 4h-chart momentum plays. One good
                        # trade, decided on the higher timeframe.
                        if TRADER_TYPE == "intraday":
                            matrix = [s for s in matrix if s.horizon != "scalp"]
                        # DO NOT RE-ANALYZE a token that already has an OPEN
                        # ONE OPEN POSITION PER SYMBOL - NO EXCEPTIONS. While a
                        # position is held we wait for it to resolve instead of
                        # stacking entries or flipping direction on top of it.
                        # (The old WATCHED override silently disabled this rule:
                        # onboarding seeds the whole watchlist as WATCHED, so
                        # every symbol was exempt and the bot could stack
                        # positions / flip direction on the same asset.)
                        open_symbols = {p["symbol"] for p in positions
                                        if p.get("quantity") not in (0, None, "")}
                        if open_symbols:
                            matrix = [s for s in matrix
                                      if s.symbol not in open_symbols or s.symbol == PRIORITY]
                            print(f"[quant] holding {len(open_symbols)} open position(s) "
                                  f"({', '.join(sorted(open_symbols))}) - one position per "
                                  f"bot pick, waiting for it to resolve"
                                  + (f" | PRIORITY {PRIORITY} stays analyzable" if PRIORITY in open_symbols else ""))
                        # ONE TRADE PER TOKEN PER DAY: drop symbols already
                        # filled today so the agent moves on to the next token
                        # instead of flipping direction on the same one.
                        _traded_today = traded_symbols_today()
                        if TRADING_MODE == "paper":
                            try:
                                _traded_today |= _paper_gateway().store.filled_symbols_today(EXEC_BOT_ID)
                            except Exception:
                                pass
                        if _traded_today:
                            _skipped_today = sorted(s for s in _traded_today
                                                    if any(s == sc.symbol for sc in matrix))
                            matrix = [s for s in matrix if s.symbol not in _traded_today]
                            if _skipped_today:
                                print(f"[quant] already traded today: {', '.join(_skipped_today)} "
                                      f"- moving to next token")
                        # WATCHED = WAIT FOR GREAT (applies to the cooldown
                        # path too): if a watched symbol is the would-be pick,
                        # it only proceeds when its conviction matches or
                        # beats the best unwatched scenario in the matrix.
                        # Otherwise the watched asset stays cash and its one
                        # shot is preserved for a genuinely good setup.
                        if WATCHED:
                            best_unwatched = max(
                                (s.conviction for s in matrix
                                 if s.symbol not in WATCHED and s.ev > 0
                                 and s.symbol != PRIORITY),
                                default=0.0)
                            before = len(matrix)
                            matrix = [s for s in matrix
                                      if s.symbol == PRIORITY
                                      or s.symbol not in WATCHED
                                      or s.conviction >= best_unwatched
                                      or s.ev <= 0]
                            if len(matrix) < before:
                                print(f"[quant] watched asset below best unwatched "
                                      f"conviction ({best_unwatched:.4f}) - waiting "
                                      f"for a genuinely good setup"
                                      + (f" (PRIORITY {PRIORITY} exempt)" if PRIORITY else ""))
                    # PRIORITY WATCH leads the ranking unconditionally: the user
                    # explicitly demanded this trade — its scenarios come first.
                    actionable = sorted(matrix,
                                        key=lambda s: (s.symbol == PRIORITY,
                                                       s.symbol in WATCHED,
                                                       s.conviction),
                                        reverse=True)
                    # ALWAYS give the LLM the best LONG and the best SHORT so it
                    # can choose the more profitable direction instead of being
                    # starved into one side. CRITICAL: these two come from the
                    # FULL matrix (any EV), NOT the positive-EV subset. The GBM
                    # drift model makes EV positive on exactly ONE side almost
                    # always (the drift direction), so a positive-EV-only pick
                    # would starve the LLM of the opposite side. Presenting both
                    # lets the LLM weigh P(win)/EV/momentum and pick the better
                    # direction. The positive-EV + floor rules still gate the
                    # FILL slots below and the deterministic cooldown path.
                    best_long = max((s for s in matrix if s.direction == "long"),
                                    key=lambda s: s.conviction, default=None)
                    best_short = max((s for s in matrix if s.direction == "short"),
                                     key=lambda s: s.conviction, default=None)
                    top = []
                    # WATCHED FIRST: the user's watched tokens take priority -
                    # their highest-conviction scenarios lead the LLM's choice.
                    watched_act = [s for s in actionable if s.symbol in WATCHED]
                    for s in watched_act:
                        if len(top) >= 8:
                            break
                        if s not in top:
                            top.append(s)
                    for s in (best_long, best_short):
                        if len(top) >= 8:
                            break
                        if s is not None and s not in top:
                            top.append(s)
                    for s in actionable:
                        if len(top) >= 8:
                            break
                        if s not in top:
                            top.append(s)
                    print(f"[quant] scenario matrix: {len(matrix)} scenarios, "
                          f"{len(actionable)} ≥conv {CONVICTION_FLOOR:.2f}"
                          f" (best long={'yes' if best_long else 'no'}, "
                          f"best short={'yes' if best_short else 'no'})"
                          + (f", watched={[s for s in WATCHED]}" if WATCHED else ""))

                if not top:
                    decision = {"action": "hold", "symbol": "", "quantity": 0,
                                "stop_loss_pct": 0, "take_profit_pct": 0,
                                "reasoning": "scenario matrix: no positive-EV trade right now - cash"}
                    _last_scenario = None
                elif LIVE_AGENT_API_KEY and time.time() - _last_llm_at < LLM_COOLDOWN_SECONDS:
                    # AI-key cooldown: the model was asked recently, so pick the
                    # best scenario deterministically instead of burning another
                    # paid/rate-limited call. The math pick is the same engine the
                    # model is given; this just skips the model's vote for a while.
                    best = pick_best_scenario(matrix, has_long, has_short, CONVICTION_FLOOR, priority_symbol=PRIORITY)
                    if best is None:
                        decision = {"action": "hold", "symbol": "", "quantity": 0,
                                    "stop_loss_pct": 0, "take_profit_pct": 0,
                                    "reasoning": "AI-key cooldown - no strong scenario, cash"}
                        _last_scenario = None
                    else:
                        side = "buy" if best.direction == "long" else "short"
                        stop_pct = abs(best.entry - best.stop) / best.entry * 100
                        take_pct = abs(best.target - best.entry) / best.entry * 100
                        # Clamp to REACHABLE interday levels so the trade
                        # actually executes against real USDC and the target
                        # can hit within hours-to-days.
                        stop_pct, take_pct = clamp_risk_levels(stop_pct, take_pct)
                        qty, lev, why = balance_aware_size(
                            eq, portfolio.get('cash', eq), best.entry, stop_pct,
                            best.symbol, conviction=best.conviction, p_win=best.p_win)
                        decision = {"action": side, "symbol": best.symbol,
                                    "quantity": qty,
                                    "stop_loss_pct": round(stop_pct, 2),
                                    "take_profit_pct": round(take_pct, 2),
                                    "leverage": lev,
                                    "reasoning": f"[quant/cooldown] best scenario {best.direction} "
                                                 f"{best.symbol} EV={best.ev:+.2f}R | {why}"}
                        _last_scenario = best
                elif LIVE_AGENT_API_KEY:
                    # LLM compiles the matrix and picks the best trade
                    _last_llm_at = time.time()
                    skill_ctx = _load_skill_context()
                    matrix_txt = "\n".join(
                        s.to_prompt()
                        + (" | MOMENTUM: CONFIRMED" if momentum_ok.get((s.symbol, s.direction, s.horizon)) else " | MOMENTUM: against")
                        for s in top
                    )
                    system = (
                        "You are the decision layer of an automated trading agent. "
                        f"Your trader type: <b>{TRADER_TYPE.upper()}</b>. "
                        "You are GIVEN a scenario matrix computed with REAL "
                        "quantitative math: for each symbol there is a LONG and a "
                        "SHORT path, each with P(win) (the probability the take-profit "
                        "barrier is hit before the stop-loss, from a geometric Brownian "
                        "motion model), a reward/risk ratio R, and expected value EV "
                        "per unit risk. Your job: DO THE MATH and pick the single "
                        "best trade from the matrix - the one with the highest "
                        "conviction (P(win) * EV) that is also actionable given the "
                        "positions you already hold. This is not a vibe - use the "
                        "numbers.\n\n"
                        "BOTH DIRECTIONS ARE ALWAYS PRESENT: the matrix contains the "
                        "best LONG and the best SHORT. Do NOT default to one side. "
                        "Weigh them against each other by P(win) and EV - if the "
                        "short has the higher win rate and positive EV, pick the "
                        "short. Trading both directions is expected and correct.\n\n"
                        "MOMENTUM: each row ends with an EMA momentum flag. 'CONFIRMED' "
                        "means EMA8>EMA21 (for a long) or EMA8<EMA21 (for a short) - "
                        "the trade is already moving your way. 'against' means the "
                        "EMA stack disagrees. Favor CONFIRMED rows, but do not treat "
                        "it as an absolute veto - P(win) already bakes in drift.\n\n"
                        "EV: rows with negative EV are shown for COMPARISON so you "
                        "can see both sides. Do NOT pick a negative-EV trade - only "
                        "trade a scenario whose EV is positive and whose P(win) beats "
                        "the other direction.\n\n"
                        "THE STRATEGY SKILLS ARE LOADED BELOW. Follow them exactly; "
                        "do not invent rules that contradict them.\n\n"
                        f"{skill_ctx}\n\n"
                        "Reply with a single JSON object only:\n"
                        '{"action":"buy|short","symbol":"<SYMBOL>",'
                        '"direction":"long|short",'
                        '"quantity":<notional risk size in units of the symbol>,'
                        '"leverage":<20-40 integer based on your confidence>,'
                        '"reasoning":"<2-3 sentences: cite the P(win), EV, and why '
                        'this scenario beats the others>"}\n'
                        "LEVERAGE: Set it based on your confidence in the trade.\n"
                        "  - High confidence (P(win) >= 55% + momentum confirmed):\n"
                        "    40x for BTC, 25x for all other assets (max the venue allows)\n"
                        "  - Medium confidence (P(win) 45-55%): 20-25x\n"
                        "  - Low confidence (P(win) < 45%): 20x (minimum)\n"
                        "  You have all the numbers (P(win), EV, R, conviction, momentum).\n"
                        "  'Do the math' - pick the right leverage for the edge you see.\n"
                        "IMPORTANT: action 'buy' opens a LONG, action 'short' opens "
                        "a SHORT. Only ever pick an open-side action - exits are "
                        "handled by the engine, not you. "
                        "stop and take are already set per scenario. "
                        "quantity = dollars-at-risk / entry price, "
                        "where dollars-at-risk is ~1% of equity. The system will "
                        "clamp your size afterward - stay conservative."
                    )
                    user = (
                        f"Scenario decision - {now_iso} UTC.\n"
                        f"SCENARIO MATRIX (ranked by conviction):\n{matrix_txt}\n\n"
                        f"MARKET: {', '.join(price_txt)}\n"
                        f"FUNDING: {carry_txt if carry_txt else 'none above floor'}\n"
                        f"CASH: ${portfolio.get('cash', 0):,.2f} | EQUITY: ${eq:,.2f}\n"
                        f"OPEN POSITIONS: {pos_txt}\n"
                        f"TODAY: {used}/{MAX_DAILY_TRADES} trades\n"
                        f"Pick the best positive-EV scenario. A short is allowed if "
                        f"its P(win) is genuinely the best. Do not overtrade."
                    )
                    llm = _provider_completion(system, user)
                    llm_reasoning = str(llm.get("reasoning", ""))
                    # Notify the user about AI-key issues (rate limit, errors)
                    if "llm-http-429" in llm_reasoning:
                        notify_error(llm_reasoning, kind="rate_limit")
                        print("[agent] AI key rate-limited -> trading HALTED until refilled")
                    elif llm_reasoning.startswith("llm-") or llm_reasoning.startswith("parse-failed"):
                        notify_error(llm_reasoning, kind="llm")
                    llm_action = str(llm.get("action", "")).lower()
                    llm_dir = str(llm.get("direction", "")).lower()
                    llm_sym = str(llm.get("symbol", "")).upper()
                    # Derive the OPEN-side action from direction: the LLM may
                    # return action="buy|short" with direction="long|short".
                    # When direction is present, it takes precedence so a
                    # "short" direction always opens a short regardless of the
                    # action field value.
                    if llm_dir == "short":
                        llm_action = "short"
                    elif llm_dir == "long":
                        llm_action = "buy"
                    if llm_action in ("buy", "short") and llm_sym:
                        # risk-clamp: never exceed 1% risk; stop/target come from
                        # the chosen scenario's REAL volatility-based levels.
                        # Correct retail sizing: risk$ = 1% of equity, and the
                        # notional = risk$ / stop%, so qty = (eq*risk/stop)/price.
                        qty = float(llm.get("quantity", 0) or 0)
                        # find the scenario the LLM chose, for levels + tracking
                        _last_scenario = None
                        for _s in top:
                            if _s.symbol == llm_sym and _s.direction == llm_dir:
                                _last_scenario = _s
                                break
                        rejected = False
                        if qty > 0 and llm_sym in prices:
                            # ENFORCE positive-EV: negative-EV rows are shown only
                            # for comparison. Refuse to trade a scenario that does
                            # not clear the positive-EV + floor bar.
                            if _last_scenario is not None and \
                               (_last_scenario.ev <= 0 or _last_scenario.conviction < CONVICTION_FLOOR):
                                decision = {"action": "hold", "symbol": "", "quantity": 0,
                                            "stop_loss_pct": 0, "take_profit_pct": 0,
                                            "reasoning": f"[LLM guard] {llm_dir} {llm_sym} below "
                                                         f"positive-EV/floor bar - held"}
                                rejected = True
                                print(f"[agent] LLM pick rejected: {llm_dir} {llm_sym} "
                                      f"EV={_last_scenario.ev:+.3f} conv={_last_scenario.conviction:.4f} "
                                      f"< floor {CONVICTION_FLOOR}")
                            else:
                                stop_pct = (abs(_last_scenario.entry - _last_scenario.stop) / _last_scenario.entry * 100) \
                                    if _last_scenario is not None else 0.3
                                # PERP SIZING: amount scales with conviction
                                # (base 15% of balance, +15% per conviction
                                # doubling above the floor), hard-capped at 45%
                                # of the total balance. No 1%-of-equity risk
                                # sizing - conviction IS the risk control.
                                bal = max(float(portfolio.get('cash', eq) or 0),
                                          float(eq or 0), 0.0)
                                _floor = max(CONVICTION_FLOOR, 0.0008)
                                _conv = _last_scenario.conviction if _last_scenario else 0.0
                                if _conv > 0:
                                    _dbl = max(0.0, min(2.0, abs(_conv) / _floor))
                                    _expo = min(0.45, 0.15 * (1.0 + _dbl))
                                elif _last_scenario is not None and _last_scenario.p_win > 0:
                                    _expo = min(0.45, 0.15 + max(0.0, _last_scenario.p_win - 0.30) * 2.0)
                                else:
                                    _expo = 0.15
                                max_notional = min(bal * 0.45, bal * 0.95)
                                max_qty = (bal * _expo) / prices[llm_sym] if prices.get(llm_sym, 0) > 0 else 0.0
                                max_qty = min(max_qty, max_notional / max(prices[llm_sym], 1e-9))
                                # CONVICTION SIZE WINS: the sizing engine sets the
                                # amount (scaled by conviction, 45% cap). The LLM's
                                # freeform quantity is only an upper bound veto.
                                qty = max_qty
                        if not rejected:
                            if _last_scenario is not None:
                                stop_pct = abs(_last_scenario.entry - _last_scenario.stop) / _last_scenario.entry * 100
                                take_pct = abs(_last_scenario.target - _last_scenario.entry) / _last_scenario.entry * 100
                                stop_pct, take_pct = clamp_risk_levels(stop_pct, take_pct)
                            else:
                                stop_pct, take_pct = 3.0, 6.0  # reachable fallback
                            decision = {
                                "action": llm_action,
                                "symbol": llm_sym,
                                "quantity": qty if qty > 0 else 0,
                                "stop_loss_pct": round(stop_pct, 2),
                                "take_profit_pct": round(take_pct, 2),
                                "leverage": int(float(llm.get("leverage", 0) or 0) or LIVE_AGENT_LEVERAGE),
                                "reasoning": f"[LLM scenario pick] {llm_reasoning[:240]}",
                            }
                            print(f"[agent] LLM PICKED {llm_dir.upper() or llm_action} {llm_sym} "
                                  f"qty={qty:.4f} stop={stop_pct:.1f}% take={take_pct:.1f}% "
                                  f":: {llm_reasoning[:60]}")
                    else:
                        # LLM is the decision layer. If it failed (rate-limited,
                        # network, parse) or chose hold, the bot HALTS new entries
                        # - it does NOT silently fall back to trading on its own.
                        # Capital preservation: no new positions without the AI
                        # decision layer. Exits still run (they protect capital).
                        if llm_reasoning.startswith("llm-http-429"):
                            decision = {"action": "hold", "symbol": "", "quantity": 0,
                                        "stop_loss_pct": 0, "take_profit_pct": 0,
                                        "reasoning": "AI key rate-limited - trading HALTED until refilled"}
                        elif llm_reasoning.startswith("llm-") or llm_reasoning.startswith("parse-failed"):
                            decision = {"action": "hold", "symbol": "", "quantity": 0,
                                        "stop_loss_pct": 0, "take_profit_pct": 0,
                                        "reasoning": f"AI model error - holding ({llm_reasoning[:100]})"}
                        else:
                            print(f"[agent] LLM chose no trade ({llm_reasoning[:100]})")
                            decision = {"action": "hold", "symbol": "", "quantity": 0,
                                        "stop_loss_pct": 0, "take_profit_pct": 0,
                                        "reasoning": f"[LLM] {llm_reasoning[:200]}"}
                        _last_scenario = None
                else:
                    # no LLM key -> fall back to the math's best scenario
                    best = pick_best_scenario(matrix, has_long, has_short, CONVICTION_FLOOR, priority_symbol=PRIORITY)
                    if best is None:
                        decision = {"action": "hold", "symbol": "", "quantity": 0,
                                    "stop_loss_pct": 0, "take_profit_pct": 0,
                                    "reasoning": "best scenario has non-positive EV - cash"}
                        _last_scenario = None
                    else:
                        # OPEN-SIDE action: a SHORT scenario opens a real short.
                        side = "buy" if best.direction == "long" else "short"
                        stop_pct = abs(best.entry - best.stop) / best.entry * 100
                        take_pct = abs(best.target - best.entry) / best.entry * 100
                        stop_pct, take_pct = clamp_risk_levels(stop_pct, take_pct)
                        qty, lev, why = balance_aware_size(
                            eq, portfolio.get('cash', eq), best.entry, stop_pct,
                            best.symbol, conviction=best.conviction, p_win=best.p_win)
                        decision = {"action": side, "symbol": best.symbol,
                                    "quantity": qty,
                                    "stop_loss_pct": round(stop_pct, 2),
                                    "take_profit_pct": round(take_pct, 2),
                                    "leverage": lev,
                                    "reasoning": f"[quant] best scenario {best.direction} "
                                                 f"{best.symbol} EV={best.ev:+.2f}R | {why}"}
                        _last_scenario = best
        except Exception as exc:
            print(f"[quant] engine failed, holding: {exc}")
            decision = {"action": "hold", "symbol": "", "quantity": 0,
                        "stop_loss_pct": 0, "take_profit_pct": 0,
                        "reasoning": f"quant engine error: {exc}"}
    else:
        decision = ask_model(prompt)
    action = str(decision.get("action", "hold")).lower()
    symbol = str(decision.get("symbol", "")).upper()
    # DIRECTION: the LLM's long/short intent (not the order verb). Peek shows
    # this, so the user sees LONG/SHORT instead of buy/sell/short/cover verbs.
    direction = str(decision.get("direction", "")).lower()
    if not direction:
        direction = {"buy": "long", "short": "short"}.get(action, "")
    qty = float(decision.get("quantity", 0) or 0)
    stop_pct = float(decision.get("stop_loss_pct", 0) or 0)
    take_pct = float(decision.get("take_profit_pct", 0) or 0)
    reasoning = str(decision.get("reasoning", ""))[:300]
    market = dict(UNIVERSE).get(symbol, "crypto")
    # LEVERAGE POLICY — CONVICTION-DRIVEN, USER-CAPPED, TRANSPARENT: the
    # bot picks leverage by trade conviction (13x floor on 20x markets,
    # scaling toward the cap as conviction rises); the USER'S Settings
    # leverage is the hard cap it can never exceed; liq-safety clamps
    # apply last. The chosen value + reasoning are shown in every
    # notification and stored in the decision log.
    # Closes (sell/cover) always go 1x - leverage only applies to opens.
    _conv_for_lev = _last_scenario.conviction if _last_scenario is not None else 0.0
    lev_choice = conviction_leverage(symbol, market, _conv_for_lev,
                                     stop_pct=stop_pct or None,
                                     user_cap=LIVE_AGENT_LEVERAGE) \
        if action in ("buy", "short") else 1.0
    lev_explained = (f"{lev_choice:g}x (conviction {_conv_for_lev:.3f}, "
                     f"floor {CONVICTION_LEV_FLOOR:g}x, cap {min(LIVE_AGENT_LEVERAGE, AFTERMATH_MAX_LEVERAGE.get(symbol.upper(), 10)):g}x)"
                     if action in ("buy", "short") else "1x (close)")

    row = {"symbol": symbol, "action": action, "direction": direction,
           "price": prices.get(symbol, 0),
           "quantity": qty, "stop_pct": stop_pct, "take_pct": take_pct,
           "leverage": lev_choice if action in ("buy", "short") else 1.0,
           "lev_why": lev_explained,
           "fill_ok": None, "reasoning": reasoning, "error": ""}

    # Open-position map for the guard chain below (one position per symbol).
    open_symbols = {p["symbol"] for p in positions
                    if p.get("quantity") not in (0, None, "")}

    if action in ("buy", "sell", "short", "cover"):
        has_long = any(p["symbol"] == symbol and p["quantity"] > 0 for p in positions)
        has_short = any(p["symbol"] == symbol and p["quantity"] < 0 for p in positions)
        is_forced_exit = decision.get("_forced_exit", False)
        if not is_forced_exit and not market_open(market):
            row["action"] = "hold"; row["error"] = f"{market} market is closed now"
        elif symbol not in dict(UNIVERSE):
            row["action"] = "hold"; row["error"] = f"unsupported symbol {symbol}"
        elif qty <= 0:
            row["action"] = "hold"; row["error"] = "non-positive quantity"
        elif used >= MAX_DAILY_TRADES:
            row["action"] = "hold"; row["error"] = "daily trade limit reached"
        elif action in ("buy", "short") and symbol in (
                traded_symbols_today()
                | (_paper_gateway().store.filled_symbols_today(EXEC_BOT_ID)
                   if TRADING_MODE == "paper" else set())) and symbol != PRIORITY:
            # ONE TRADE PER TOKEN PER DAY: if the bot already opened + filled a
            # position on this symbol today (long OR short), it does NOT flip
            # direction on the same token - it moves on to the next watched
            # token instead. Paper fills count the same as live fills.
            # PRIORITY WATCH bypasses this: 'watch X now' is an explicit order.
            row["action"] = "hold"
            row["error"] = (f"already traded {symbol} today - one trade per "
                            f"token per day, moving to the next")
        elif action in ("buy", "short") and symbol != PRIORITY and symbol in open_symbols:
            # ONE OPEN POSITION PER SYMBOL: never stack a second position or
            # flip direction while the first is still open. The open trade is
            # left completely alone until its own exit logic (stop/target/
            # trail) resolves it - no interference, no additions, no flips.
            row["action"] = "hold"
            row["error"] = (f"position open on {symbol} - committed to this trade, "
                            f"waiting for it to win or stop out")
        elif action in ("buy", "short") and symbol != PRIORITY \
                and entry_attempts_today(symbol) >= MAX_ENTRY_ATTEMPTS:
            # ONE-SHOT RULE: a single entry decision per symbol per day. The
            # bot takes its best shot, then makes NO further decisions on the
            # asset - no re-entry, no direction flip, no retry spam - until
            # tomorrow. PRIORITY WATCH bypasses: an explicit user demand.
            row["action"] = "hold"
            row["error"] = (f"{symbol}'s one trade for today is already decided - "
                            f"no further decisions until tomorrow")
        elif action in ("buy", "short") and qty * prices.get(symbol, 1e9) > eq * MAX_POSITION_PCT / 100:
            row["action"] = "hold"; row["error"] = "position size cap exceeded"
        elif action in ("buy", "short") and stop_pct == 0 and FORCE_STOP_PCT > 0:
            stop_pct = FORCE_STOP_PCT  # mandatory stop-loss on new entries
        elif action in ("buy", "short") and positions and symbol != PRIORITY:
            # ONE POSITION RULE (interday): only ONE open position at a time -
            # the bot takes the single best trade and manages it to resolution.
            # No watched-symbol bypass: onboarding marks the whole watchlist
            # as watched, which silently disabled this rule before.
            # PRIORITY WATCH (watch X now) IS the bypass: the user explicitly
            # demanded this trade - the old position is closed first (slot
            # freed below) so the demand is honored instead of blocked.
            row["action"] = "hold"
            row["error"] = ("one-position rule: the current trade must resolve "
                            "before opening another")
        elif action in ("buy", "short") and positions and symbol == PRIORITY:
            # PRIORITY WATCH ADDS a position (user-driven multi-position):
            # 'watch X now' explicitly demands this trade ON TOP of whatever
            # else is open. The user's own trades stack; the bot's own picks
            # still follow the one-position rule. Nothing is closed here.
            print(f"[priority] {symbol} entry alongside {len(positions)} open "
                  f"position(s) - user-requested multi-position")
        elif action == "buy" and has_long:
            row["action"] = "hold"; row["error"] = "already long in symbol"
        elif action == "short" and has_short:
            row["action"] = "hold"; row["error"] = "already short in symbol"
        elif action == "sell" and not has_long:
            row["action"] = "hold"; row["error"] = f"no long position in {symbol}"
        elif action == "cover" and not has_short:
            row["action"] = "hold"; row["error"] = f"no short position in {symbol}"

    if row["action"] in ("buy", "sell", "short", "cover"):
        # PROFITABILITY GATE (SCALPER ONLY): new entries must clear regime + fee
        # floor. momentum20 has its OWN validated gate (20d > 2% long) - the 5m
        # scalper gate would wrongly block it. Closes are never blocked.
        if GATE_ENABLED and STRATEGY != "momentum20":
            stats = market_stats(symbol, market)
            allowed, reason = profitability_gate(row["action"], symbol, market,
                                                 prices, positions, stats)
            # SENTIMENT MULTIPLIER: numeric sentiment (-1..+1) scales size but
            # never creates or blocks a trade. Negative = tighter size.
            senti_score = 0.0
            senti_label = ""
            try:
                from sentiment import sentiment_score as _ss
                senti_score, senti_label = _ss(market, symbol)
            except Exception:
                pass
            if allowed and row["action"] in ("buy", "short") and senti_score < 0:
                mult = max(0.1, 1.0 + senti_score)  # -0.42 -> 0.58x size
                qty = qty * mult
                row["reasoning"] = f"{reasoning} [senti {senti_label}, size x{mult:.2f}]"
            # RISK-BASED SIZING: overwrite the LLM's freeform quantity with
            # risk-derivation (risk% / stop distance, Kelly-capped, vol-tar).
            if RISK_SIZE_ENABLED and allowed and row["action"] in ("buy", "short"):
                entry_px = prices.get(symbol, 0) or row["price"] or 0
                eqv = equity(portfolio, prices)
                rv = stats.get("realized_vol")
                new_qty, why = compute_risk_size(eqv, entry_px,
                                                 stop_pct or FORCE_STOP_PCT,
                                                 take_pct or 0.0, rv)
                if new_qty > 0:
                    qty = new_qty
                    row["quantity"] = new_qty
                    row["reasoning"] = f"{reasoning} [size {why}]"
            if not allowed:
                row["action"] = "hold"
                row["error"] = reason
                print(f"[gate] {symbol} {action} blocked: {reason}")
        if dry:
            print(f"[dry] would {action} {qty} {symbol} [{market}] "
                  f"(stop {stop_pct}%, take {take_pct}%, lev {lev_explained})")
            row["fill_ok"] = "dry"
        elif TRADING_MODE == "paper":
            # PAPER MODE: virtual fills at live prices with the REAL cost
            # model (venue fee + 0.5% platform fee + slippage). Same
            # discipline, same decisions - no real money moves. The paper
            # gateway is lazily built per bot (its own $1,000 portfolio).
            pg = _paper_gateway()
            if row["action"] in ("buy", "short"):
                lev = lev_choice if action in ("buy", "short") else 1.0
                # USER APPROVAL GATE: notify with Take/Reject buttons and wait.
                # Rejected = skipped; timeout = auto-execute (autonomy kept).
                _dk = f"{symbol}:{'long' if action == 'buy' else 'short'}:{int(time.time() * 1000)}"
                _entry_px = prices.get(symbol, 0) or row["price"] or 0
                _sl = _entry_px * (1 - stop_pct / 100) if action == "buy" \
                    else _entry_px * (1 + stop_pct / 100)
                _tp = _entry_px * (1 + take_pct / 100) if action == "buy" \
                    else _entry_px * (1 - take_pct / 100)
                try:
                    pg.store.put_decision(
                        EXEC_BOT_ID, _dk, symbol,
                        "long" if action == "buy" else "short", qty, _entry_px,
                        lev, _sl if stop_pct else None, _tp if take_pct else None,
                        reasoning)
                    _verdict = _request_paper_approval(
                        _dk, symbol, "long" if action == "buy" else "short",
                        qty, _entry_px, lev, stop_pct, take_pct, reasoning)
                except Exception as _exc:
                    print(f"[paper] approval gate error (executing): {_exc}")
                    _verdict = "timeout"
                if _verdict == "rejected":
                    row["action"] = "hold"
                    row["error"] = "rejected by user (paper approval)"
                    pg.store.set_decision_status(_dk, "rejected")
                    print(f"[paper] {symbol} REJECTED by user - skipped")
                    log_decision(row)
                    return row
                fill = pg.open(EXEC_BOT_ID, symbol,
                               "long" if action == "buy" else "short", qty,
                               prices.get(symbol, 0) or row["price"] or 0,
                               leverage=lev,
                               stop_loss=_sl if stop_pct else None,
                               take_profit=_tp if take_pct else None,
                               order_type="limit",
                               limit_price=row.get("limit_price") or None,
                               idempotency_key=row.get("idempotency_key", f"paper-{symbol}-{action}-{int(time.time()*1000)}"))
                if fill.get("ok"):
                    try:
                        pg.store.set_decision_status(_dk, "taken")
                    except Exception:
                        pass
            else:  # sell / cover
                fill = pg.close(EXEC_BOT_ID, symbol,
                                prices.get(symbol, 0) or row["price"] or 0,
                                idempotency_key=f"paper-{symbol}-{action}-{int(time.time()*1000)}")
            row["fill_ok"] = fill.get("ok")
            row["error"] = fill.get("error", "")
            row["price"] = fill.get("fill_price", row["price"])
            row["paper"] = True
            # PRIORITY WATCH fulfilled: clear the demand so normal rules resume.
            if fill.get("ok") and symbol == PRIORITY:
                try:
                    _clear_priority(EXEC_BOT_ID)
                    print(f"[priority] {symbol} trade taken - priority cleared, "
                          f"normal discipline resumes")
                except Exception as _exc:
                    print(f"[priority] clear failed: {_exc}")
            print(f"[trade][paper] {row['action']} {qty} {symbol} "
                  f"-> {'OK' if fill.get('ok') else fill.get('error')}")
        elif gw:
            # Pop-up the decided trade BEFORE the order goes out.
            notify_trade(symbol, action, qty, prices.get(symbol, 0) or row["price"] or 0,
                         stop_pct, take_pct, lev_choice,
                         reasoning=f"[LLM+quant] {reasoning} · leverage {lev_explained}")
            # REAL EXECUTION: route through the gateway (VenueRouter -> adapter).
            fill = route_real_order(gw, EXEC_BOT_ID, symbol, market, row["action"], qty,
                                    stop_pct or None, take_pct or None,
                                    prices.get(symbol, 0) or 0,
                                    lev_choice if action in ("buy", "short") else 1.0)
            row["fill_ok"] = fill.get("ok")
            row["error"] = fill.get("error", "")
            row["price"] = fill.get("price", row["price"])
            row["order_type"] = fill.get("order_type", "limit")
            row["limit_price"] = fill.get("limit_price") or row["price"]
            if not fill.get("ok"):
                notify_error(fill.get("error", ""))
            print(f"[trade][real] {row['action']} {qty} {symbol} [{market}] "
                  f"-> {'OK' if fill.get('ok') else fill.get('error')} "
                  f"order={fill.get('order_id', '')}")
            if fill.get("ok") and _last_scenario is not None:
                _log_prediction(decision, _last_scenario.p_win, _last_scenario.R,
                                _last_scenario.ev, _last_scenario.drift_annual,
                                _last_scenario.vol_annual,
                                entry=_last_scenario.entry,
                                stop=_last_scenario.stop,
                                target=_last_scenario.target)
        else:
            row["action"] = "hold"
            row["error"] = "live execution not configured - holding (switch to Paper mode on the dashboard to trade virtually)"
            print(f"[hold] live execution not configured - holding")
            log_decision(row)
            return
    else:
        print(f"[hold] {reasoning[:120]}")

    log_decision(row)


def _scope_universe_to_chain() -> None:
    """At startup, if the user chose a specific chain (LIVE_AGENT_CHAIN), the
    trading universe is REPLACED with that chain's crypto perp assets only
    (plus any 'watch <ASSET>' picks). Forex/stocks/spot are never analyzed
    when a perp chain is selected - the agent only works on that chain's
    tokens. Runs before the gateway so paper mode analyzes the right chain."""
    try:
        chain = os.getenv("LIVE_AGENT_CHAIN", "").strip().lower()
        if not chain:
            return
        # Watched assets always get priority (prepended).
        watched_crypto = [(s, "crypto") for s in WATCHED if s]
        # Only honor an explicit pinned crypto universe if the user set
        # LIVE_AGENT_SYMBOLS to crypto symbols AND no chain is chosen... but a
        # chain IS chosen here, so always scope to the chain's perps.
        perp_assets = {
            "sui": ["BTC", "ETH", "SOL", "SUI", "HYPE"],
            "solana": ["BTC", "ETH", "SOL", "SUI", "DOGE"],
            "hyperliquid": ["BTC", "ETH", "SOL", "SUI", "HYPE"],
        }.get(chain, ["BTC", "ETH"])
        # Replace the universe: chain perps + user watch picks. Nothing else.
        # When a perp chain is chosen, forex/stocks/spot are NOT analyzed.
        seen = set()
        new_universe = []
        for sym, mkt in watched_crypto + [(s, "crypto") for s in perp_assets]:
            sym = sym.upper()
            key = (sym, mkt)
            if key not in seen:
                seen.add(key)
                new_universe.append(key)
        UNIVERSE[:] = new_universe
        print(f"[agent] chain={chain}: scoped to {', '.join(s for s, _ in new_universe)}"
              + (f" (watch: {', '.join(WATCHED)})" if WATCHED else ""))
    except Exception as exc:
        print(f"[agent] chain scope skipped: {exc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single decision cycle")
    ap.add_argument("--dry", action="store_true", help="log decision without executing")
    args = ap.parse_args()

    _scope_universe_to_chain()
    token = _get_token()
    universe_txt = ", ".join(f"{s}[{m}]" for s, m in UNIVERSE)
    print(f"[agent] strategy={STRATEGY} model={MODEL} universe={universe_txt} interval={INTERVAL}s "
          f"max_trades/day={MAX_DAILY_TRADES} pos_cap={MAX_POSITION_PCT}%")
    run_cycle(token, dry=args.dry)
    if not args.once:
        # Fast exit-check thread (60s) - runs alongside the 300s decision cycle
        # so time exits, trailing stops, and partial profits fire within 1 min.
        def _exit_loop():
            while True:
                time.sleep(60)
                try:
                    gw = _get_exec_gateway()
                    pf = get_real_portfolio(gw, EXEC_BOT_ID) if gw else get_portfolio(token)
                    if not pf.get("positions"):
                        continue
                    positions = pf["positions"]
                    prices = {}
                    for sym, market in UNIVERSE:
                        try:
                            prices[sym] = get_price(token, sym, market)
                        except Exception:
                            pass
                    from quant_strategy import time_exit_check, trail_check, partial_profit_check
                    for check_fn, label in [(time_exit_check, "time exit"),
                                            (partial_profit_check, "scale out"),
                                            (trail_check, "trailing exit")]:
                        exits = check_fn(positions, prices)
                        if exits:
                            pick = exits[0]
                            d = pick.to_dict()
                            d["_forced_exit"] = True
                            side = "sell" if d["action"] in ("sell", "cover") else d["action"]
                            qty = d.get("quantity", 0) or 0
                            if qty <= 0:
                                qty = abs(next((p["quantity"] for p in positions if p["symbol"] == pick.symbol), 0))
                            # REAL MODE: route the exit through the execution
                            # gateway (same path as entries) so the Aftermath
                            # position is actually closed. The exit is a market
                            # order (no 2bps maker offset - it must fill).
                            if gw is not None:
                                fill = route_real_order(
                                    gw, EXEC_BOT_ID, pick.symbol,
                                    dict(UNIVERSE).get(pick.symbol, "crypto"),
                                    side, qty, None, None,
                                    prices.get(pick.symbol) or 0,
                                    clamp_leverage(pick.symbol, "crypto", LIVE_AGENT_LEVERAGE),
                                )
                            else:
                                # PAPER MODE REMOVED: no real gateway means no
                                # real position to close - never fabricate a
                                # paper exit.
                                fill = {"ok": False,
                                        "error": "real execution not configured - exit skipped (paper mode removed)"}
                            if fill.get("ok"):
                                print(f"[exit] {label}: {pick.symbol} - {pick.reasoning[:80]}")
                            else:
                                # ERROR DISCLOSURE: a forced exit that fails must
                                # be surfaced - the user needs to know their stop
                                # or time-exit didn't go through.
                                print(f"[exit] FAILED {label}: {pick.symbol} - {fill.get('error','')[:100]}")
                                notify_error(f"{label} for {pick.symbol} failed: {fill.get('error','')}", kind="error")
                            break
                except Exception as exc:
                    # FAULT TOLERANCE: never crash the exit thread. Transient
                    # errors (price fetch, RPC hiccup) are logged, NOT pushed
                    # to the user - they are internal noise. Only a genuinely
                    # failed exit (position stuck) is user-facing, and that is
                    # notified above where the fill is attempted.
                    print(f"[exit] loop error: {exc}")
                    import logging as _logging
                    _logging.getLogger("agent").warning("exit-check transient error: %s", exc)
        import threading as _threading
        _threading.Thread(target=_exit_loop, name="exit-check", daemon=True).start()
        while True:
            time.sleep(INTERVAL)
            try:
                run_cycle(token, dry=args.dry)
            except Exception as exc:
                print(f"[agent] cycle error: {exc}")


if __name__ == "__main__":
    main()