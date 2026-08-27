"""
Live LLM trading agent for AI-Trader.

Connects an opencode-go model to the running AI-Trader platform:
  1. Fetches live prices + portfolio state from the platform API
  2. Asks the LLM for a structured decision (JSON)
  3. Enforces client-side risk guards (daily trade limit, position size cap,
     mandatory stop-loss, market hours)
  4. Executes through POST /api/signals/realtime (paper trading, real prices)
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
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

AGENT_DIR = Path(__file__).resolve().parents[1]  # service/
LOG_PATH = Path(__file__).resolve().parents[2] / "research" / "exports" / "live_agent_log.csv"
TOKEN_FILE = Path(__file__).resolve().parents[2] / "service" / "agent" / ".agent_token"

MODEL = os.getenv("LIVE_AGENT_MODEL", "opencode-go/deepseek-v4-flash")
# universe as symbol:market pairs, e.g. "BTC:crypto,ETH:crypto,AAPL:us-stock,EURUSD:forex"
UNIVERSE = [
    (s.strip().split(":")[0], (s.strip().split(":")[1] if ":" in s else "crypto"))
    for s in os.getenv("LIVE_AGENT_SYMBOLS", "BTC,ETH").split(",")
    if s.strip()
]
INTERVAL = int(os.getenv("LIVE_AGENT_INTERVAL", "120"))
BASE_URL = os.getenv("AI_TRADER_URL", "http://127.0.0.1:8000")
MAX_DAILY_TRADES = int(os.getenv("LIVE_AGENT_MAX_DAILY_TRADES", "12"))
MAX_POSITION_PCT = float(os.getenv("LIVE_AGENT_MAX_POSITION_PCT", "30"))
FORCE_STOP_PCT = float(os.getenv("LIVE_AGENT_FORCE_STOP_PCT", "5"))
# 1 = active scalper mode: hold a position most of the time (long/short), trade often.
# 0 = conservative mode: cash-preferred, only trade on clear setups.
ACTIVE_MODE = os.getenv("LIVE_AGENT_ACTIVE_MODE", "1").strip() in {"1", "true", "yes", "on"}
# Per-user LLM credentials (set by the Telegram bot network). When LIVE_AGENT_API_KEY
# is set, decisions call the provider API directly instead of the opencode CLI.
LIVE_AGENT_API_KEY = os.getenv("LIVE_AGENT_API_KEY", "")
LIVE_AGENT_PROVIDER = os.getenv("LIVE_AGENT_PROVIDER", "openai")
LIVE_AGENT_BASE_URL = os.getenv("LIVE_AGENT_BASE_URL", "")
LIVE_AGENT_LEVERAGE = float(os.getenv("LIVE_AGENT_LEVERAGE", "1"))


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


def get_history(symbol: str, market: str, days: int = 30) -> pd.DataFrame:
    """Daily history via yfinance for trend context (only used for prompts)."""
    import yfinance as yf

    if market == "crypto":
        ticker = f"{symbol}-USD"
    elif market == "forex":
        ticker = f"{symbol}=X"
    else:
        ticker = symbol
    df = yf.download(ticker, period=f"{days}d", interval="1d",
                     progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Close"]].dropna()


def execute_trade(token: str, symbol: str, market: str, action: str, quantity: float,
                  stop_loss_pct=None, take_profit_pct=None, leverage=None) -> dict:
    payload = {
        "market": market, "symbol": symbol, "action": action,
        "quantity": quantity, "price": 0, "executed_at": "now",
    }
    if leverage:
        payload["leverage"] = leverage
    # stop/take are OPEN-side params only: the platform rejects them on closes
    # (routes_signals.py: "can only be set when opening (buy/short)"). Do not
    # forward them on sell/cover or the close is rejected and the position
    # stays open (this was the 2026-08-27 00:41/01:14 ETH-sell rejections).
    if stop_loss_pct is not None and action in ("buy", "short"):
        payload["stop_loss_pct"] = stop_loss_pct
    if take_profit_pct is not None and action in ("buy", "short"):
        payload["take_profit_pct"] = take_profit_pct
    r = requests.post(f"{BASE_URL}/api/signals/realtime", headers=_headers(token),
                      json=payload, timeout=60)
    if r.status_code != 200:
        return {"ok": False, "error": r.json().get("detail", r.text[:200])}
    return {"ok": True, **r.json()}


# ---------------------------------------------------------------- profitability gate

TRADE_FEE_RATE = 0.001          # platform fee per leg (fees.py TRADE_FEE_RATE)
GATE_ENABLED = os.getenv("LIVE_AGENT_PROFIT_GATE", "1").strip() in {"1", "true", "yes", "on"}
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


def market_stats(symbol: str, market: str) -> dict:
    """Regime + trend read from the agent's 5m window (candidates for the gate).

    - trend_ratio: |1h net change|/100 as a decimal bias (sign = direction)
    - regime: documented classifier (bull | bear | sideways) from 4h lookback:
        forward-implied 20-bar move > +0.2% bullish, < -0.2% bearish else sideways
      (vol classifier omitted here — keep the gate deterministic and cheap).
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
        return {"regime": regime, "trend_ratio": trend, "closes": closes,
                "realized_vol": rv if rv else None}
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
        return False, f"regime={regime}: sideways - no entries (fee burn)"

    tr = cached_data.get("trend_ratio")
    if tr is not None:
        expected_move = abs(tr) * 100
        cost_floor = (TRADE_FEE_RATE * 4) * 100  # 0.4% round trip incl. slippage
        if 0 < expected_move < cost_floor * 0.75:
            return False, f"move {expected_move:.2f}% < fee floor ~{cost_floor:.2f}% (churn filter)"
        # trend alignment: long wants tr > 0, short wants tr < 0
        aligned = (tr > 0 if wanted_long else tr < 0)
        if not aligned and abs(tr) > 1e-6:
            return False, f"1h trend {tr*100:+.2f}% opposes requested {('long' if wanted_long else 'short')} (trend alignment)"
    return True, ""

def ask_model(prompt: str) -> dict:
    """Ask the model for a JSON decision: provider API (user key) or opencode CLI."""
    if ACTIVE_MODE:
        system = (
            "You are an ACTIVE trader on a paper-trading platform (real prices, simulated money). "
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
            "You are a disciplined crypto futures paper-trading agent. "
            "You ALWAYS reply with a single valid JSON object, no markdown, no extra text:\n"
            '{"action":"buy|sell|hold","symbol":"BTC|ETH","quantity":<number>,"stop_loss_pct":<number|0>,'
            '"take_profit_pct":<number|0>,"reasoning":"<1-2 sentences>"}\n'
            "Rules: action=hold means no trade. quantity 0 for hold. "
            "When buying, always set stop_loss_pct between 3 and 15. "
            "Do not overtrade. Prefer cash in uncertainty. Never chase after big green candles."
        )
    if LIVE_AGENT_API_KEY:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tg_bot"))
            from provider import chat_completion

            out = chat_completion(LIVE_AGENT_PROVIDER, LIVE_AGENT_API_KEY, system, prompt,
                                  base_url=LIVE_AGENT_BASE_URL or None, model=MODEL)
            start, end = out.find("{"), out.rfind("}")
            if start == -1 or end == -1:
                return {"action": "hold", "quantity": 0, "reasoning": f"parse-failed: {out[:120]}"}
            return json.loads(out[start : end + 1])
        except Exception as exc:
            return {"action": "hold", "quantity": 0, "reasoning": f"llm-error: {exc}"}
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
            action = parts[2] if len(parts) > 2 else ""
            fill_ok = parts[7] if len(parts) > 7 else ""
            if action in ("buy", "sell", "short", "cover") and fill_ok.strip() == "True":
                count += 1
    return count


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


# ---------------------------------------------------------------- log

def log_decision(row: dict):
    """Append one decision row to the CSV log. Never raises: a cycle that fails
    to execute a DB fill must still leave a durable log record."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fresh = not LOG_PATH.exists()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            if fresh:
                f.write("ts,symbol,action,price,quantity,stop_pct,take_pct,fill_ok,reasoning,error\n")
            ts = datetime.now(timezone.utc).isoformat()
            f.write(f"{ts},{row.get('symbol')},{row.get('action')},{row.get('price')},"
                    f"{row.get('quantity')},{row.get('stop_pct')},{row.get('take_pct')},"
                    f"{row.get('fill_ok')},\"{str(row.get('reasoning','')).replace('\"','\"\"')}\","
                    f"{str(row.get('error','')).replace(',',';')}\n")
    except Exception as exc:
        print(f"[agent log] failed to append decision row: {exc}", file=sys.stderr)


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
    now_iso = datetime.now(timezone.utc).isoformat()
    portfolio = get_portfolio(token)
    prices = {}
    price_txt = []
    context = {}
    for sym, market in UNIVERSE:
        try:
            px = get_price(token, sym, market)
            prices[sym] = px
            hist = get_history(sym, market, 30)
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
            time.sleep(1.2)  # respect platform rate limit (1/sec per agent)
        except Exception as e:
            price_txt.append(f"{sym}: unavailable ({e})")
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

    positions = portfolio.get("positions", [])
    pos_txt = "; ".join(
        f"{p['symbol']} {p['side']} qty={p['quantity']} entry={p['entry_price']:.2f} "
        f"current={p.get('current_price') or 'n/a'} stop={p.get('stop_loss')} take={p.get('take_profit')}"
        for p in positions
    ) or "none"
    eq = equity(portfolio, prices)
    used = daily_trade_count()

    prompt = (
        f"Live paper-trading decision — {now_iso} UTC.\n"
        f"Universe: {', '.join(f'{s} [{m}]' for s, m in UNIVERSE)}\n"
        f"Prices + 5m trend + sentiment: {', '.join(price_txt)}\n"
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
               "reasoning": "no market open in universe - skipped LLM call", "error": "market closed"}
        print(f"[hold] all markets closed - skipped LLM call")
        log_decision(row)
        return

    decision = ask_model(prompt)
    action = str(decision.get("action", "hold")).lower()
    symbol = str(decision.get("symbol", "")).upper()
    qty = float(decision.get("quantity", 0) or 0)
    stop_pct = float(decision.get("stop_loss_pct", 0) or 0)
    take_pct = float(decision.get("take_profit_pct", 0) or 0)
    reasoning = str(decision.get("reasoning", ""))[:300]

    market = dict(UNIVERSE).get(symbol, "crypto")
    row = {"symbol": symbol, "action": action, "price": prices.get(symbol, 0),
           "quantity": qty, "stop_pct": stop_pct, "take_pct": take_pct,
           "fill_ok": None, "reasoning": reasoning, "error": ""}

    if action in ("buy", "sell", "short", "cover"):
        has_long = any(p["symbol"] == symbol and p["quantity"] > 0 for p in positions)
        has_short = any(p["symbol"] == symbol and p["quantity"] < 0 for p in positions)
        if not market_open(market):
            row["action"] = "hold"; row["error"] = f"{market} market is closed now"
        elif symbol not in dict(UNIVERSE):
            row["action"] = "hold"; row["error"] = f"unsupported symbol {symbol}"
        elif qty <= 0:
            row["action"] = "hold"; row["error"] = "non-positive quantity"
        elif used >= MAX_DAILY_TRADES:
            row["action"] = "hold"; row["error"] = "daily trade limit reached"
        elif action in ("buy", "short") and qty * prices.get(symbol, 1e9) > eq * MAX_POSITION_PCT / 100:
            row["action"] = "hold"; row["error"] = "position size cap exceeded"
        elif action in ("buy", "short") and stop_pct == 0 and FORCE_STOP_PCT > 0:
            stop_pct = FORCE_STOP_PCT  # mandatory stop-loss on new entries
        elif action == "buy" and has_long:
            row["action"] = "hold"; row["error"] = "already long in symbol"
        elif action == "short" and has_short:
            row["action"] = "hold"; row["error"] = "already short in symbol"
        elif action == "sell" and not has_long:
            row["action"] = "hold"; row["error"] = f"no long position in {symbol}"
        elif action == "cover" and not has_short:
            row["action"] = "hold"; row["error"] = f"no short position in {symbol}"

    if row["action"] in ("buy", "sell", "short", "cover"):
        # PROFITABILITY GATE: new entries must clear regime + fee floor. Closes/
        # exits are never blocked (stop-loss discipline wins over churn filter).
        if GATE_ENABLED:
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
            print(f"[dry] would {action} {qty} {symbol} [{market}] (stop {stop_pct}%, take {take_pct}%)")
            row["fill_ok"] = "dry"
        else:
            fill = execute_trade(token, symbol, market, row["action"], qty, stop_pct or None, take_pct or None,
                     leverage=LIVE_AGENT_LEVERAGE if market == "crypto" and LIVE_AGENT_LEVERAGE > 1 else None)
            row["fill_ok"] = fill["ok"]
            row["error"] = fill.get("error", "")
            row["price"] = fill.get("price", row["price"])
            print(f"[trade] {row['action']} {qty} {symbol} [{market}] @ {fill.get('price', 'n/a')} "
                  f"-> {'OK' if fill['ok'] else fill['error']}")
            # D2 fix: LOG IMMEDIATELY after the DB fill is acknowledged so a
            # later exception can never make an executed trade invisible in the
            # decision log (this was the 2026-08-26 22:42 EURUSD gap).
            log_decision(row)
            return
    else:
        print(f"[hold] {reasoning[:120]}")

    log_decision(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single decision cycle")
    ap.add_argument("--dry", action="store_true", help="log decision without executing")
    args = ap.parse_args()

    token = _get_token()
    universe_txt = ", ".join(f"{s}[{m}]" for s, m in UNIVERSE)
    print(f"[agent] model={MODEL} universe={universe_txt} interval={INTERVAL}s "
          f"max_trades/day={MAX_DAILY_TRADES} pos_cap={MAX_POSITION_PCT}%")
    run_cycle(token, dry=args.dry)
    if not args.once:
        while True:
            time.sleep(INTERVAL)
            try:
                run_cycle(token, dry=args.dry)
            except Exception as exc:
                print(f"[agent] cycle error: {exc}")


if __name__ == "__main__":
    main()