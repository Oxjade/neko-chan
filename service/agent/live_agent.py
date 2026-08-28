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
# STRATEGY selects the decision engine:
#   "momentum20" (DEFAULT): deterministic, validated 20d momentum + funding-carry
#     overlay from quant_strategy.py. Long only when 20d return > 2%, stop 8% /
#     take 24%, risk 1%/trade, half-Kelly, vol-targeted. Cash is the default. This
#     is the strategy the skills actually validate (backtest_risk_controlled.py).
#   "scalper": the old LLM 5m active scalper. NOT validated - the audit
#     (agent_evaluation_report.md) measured it at base-rate accuracy, negative
#     after fees. Kept only for A/B.
STRATEGY = os.getenv("LIVE_AGENT_STRATEGY", "momentum20").strip().lower()
# Peak-price tracker for trailing stops (in-memory per agent process).
_trailing_high: dict[str, float] = {}
# Per-user LLM credentials (set by the Telegram bot network). When LIVE_AGENT_API_KEY
# is set, decisions call the provider API directly instead of the opencode CLI.
LIVE_AGENT_API_KEY = os.getenv("LIVE_AGENT_API_KEY", "")
LIVE_AGENT_PROVIDER = os.getenv("LIVE_AGENT_PROVIDER", "openai")
LIVE_AGENT_BASE_URL = os.getenv("LIVE_AGENT_BASE_URL", "")
LIVE_AGENT_LEVERAGE = float(os.getenv("LIVE_AGENT_LEVERAGE", "1"))
# Real execution through the chain adapters (execution gateway). Requires
# REAL_TRADING_ENABLED=1 AND per-chain keys in the gateway env. Default off:
# the agent stays paper-only on the platform. When on, orders route through
# the VenueRouter -> chain adapter -> real venue, risk-guarded + ledgered.
EXEC_ENABLED = os.getenv("LIVE_AGENT_EXECUTION", "0").strip() in {"1", "true", "yes", "on"}
EXEC_BOT_ID = int(os.getenv("LIVE_AGENT_BOT_ID", "1") or "1")
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
            return False, f"move {expected_move:.2f}% < fee floor ~{cost_floor:.2f}% (churn filter)"
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


def _provider_completion(system: str, user: str) -> dict:
    """Direct OpenAI-compatible (OpenRouter/Anthropic/etc.) JSON decision call.

    OpenRouter uses the OpenAI /chat/completions shape. The active model may be a
    reasoning model that spends tokens on `reasoning` before emitting the final
    JSON in `content`. A small max_tokens budget truncates the thinking and
    returns content=None, so we pass a generous budget + low temperature, and
    fall back to a safe hold if content is empty or unparseable.
    """
    import requests as _requests

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
        body["max_tokens"] = int(os.getenv("LIVE_AGENT_MAX_TOKENS", "0"))
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
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1:
            return {"action": "hold", "quantity": 0,
                    "reasoning": f"parse-failed: {content[:120]}"}
        return json.loads(content[start : end + 1])
    except Exception as exc:  # noqa: BLE001
        return {"action": "hold", "quantity": 0, "reasoning": f"llm-error: {exc}"}


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


# ---------------------------------------------------------------- prediction tracking

PRED_LOG = Path(__file__).resolve().parents[2] / "research" / "exports" / "predictions.csv"


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
            from gateway import ExecGateway
            gw = ExecGateway.build()
            if gw.ready:
                gw.provision_all_wallets(EXEC_BOT_ID)
                print(f"[exec] gateway ready: chains={list(gw.adapters.keys())}")
                _exec_gateway = gw
            else:
                print("[exec] LIVE_AGENT_EXECUTION=1 but gateway not ready (keys not configured) - staying paper")
        except Exception as exc:
            print(f"[exec] gateway init failed (staying paper): {exc}")
            _exec_gateway = None
    return _exec_gateway


def _resolve_real_venue(symbol: str, market: str, gw) -> tuple[str, str] | None:
    """(chain, venue) for a symbol/market in real mode, or None if unsupported."""
    adapters = gw.adapters
    if market == "crypto":
        if "hyperliquid" in adapters:
            return "hyperliquid", "hl-perp"
        if "solana" in adapters:
            return "solana", "jup-perp"
        return None
    if market == "us-stock":
        if "solana" in adapters:
            return "solana", "xstocks-spot"
        return None
    return None  # forex is COMING-SOON on all chains


def get_real_portfolio(gw, bot_id: int) -> dict:
    """Positions + cash from the execution ledger (synced from on-chain)."""
    positions, cash = [], 0.0
    for chain in gw.adapters:
        try:
            gw.sync(bot_id, chain)
        except Exception:
            pass
        wallet = gw.ledger.wallet_by_bot_chain(bot_id, chain)
        if not wallet:
            continue
        state = gw.ledger.load_chain_state(wallet["id"]) or {}
        balances = state.get("balances") or {}
        cash += float(balances.get("USDC") or balances.get("USD") or 0.0)
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
    """Route one agent decision through the execution gateway (real venue)."""
    resolved = _resolve_real_venue(symbol, market, gw)
    if not resolved:
        return {"ok": False, "error": f"no real venue for {symbol} [{market}]"}
    chain, venue = resolved
    side = "buy" if action in ("buy", "cover") else "sell"
    intent_kw = dict(
        chain=chain, venue=venue, symbol=symbol, side=side, qty=qty,
        order_type="market",
        # closes (sell/cover) are always 1x with no stop/target re-armed
        leverage=leverage if action in ("buy", "short") else 1.0,
        idempotency_key=(
            f"agent:{os.getenv('LIVE_AGENT_NAME', 'agent')}:{symbol}:{action}:{int(time.time() * 1000)}"
        ),
    )
    if action in ("buy", "short"):
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
    return gw.route_and_sync(bot_id, intent, ref_price)


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
        return "Unknown error — no trade was placed."
    lowered = raw.lower()
    for needle, friendly in (
        ("Short position entry price is missing",
         "Couldn't open the short — the platform needs an entry price for shorts. "
         "The bot will keep trying with a valid price."),
        ("stop_loss_pct/take_profit_pct can only be set when opening (buy/short)",
         "Closing trades can't carry a stop/target — the close was sent safely without one."),
        ("market is currently closed", "That market is closed right now — the bot will retry when it reopens."),
        ("US market is closed", "US stocks only trade Mon–Fri 9:30–16:00 ET."),
        ("daily trade limit reached", "Today's trade limit is hit. Your bot resumes tomorrow."),
        ("position size cap exceeded", "The position was too large for your risk settings — the bot stayed flat."),
        ("rate limit", "The trading venue is rate-limiting. The bot will retry shortly."),
        ("timeout", "The trading venue timed out. The bot will retry."),
    ):
        if needle.lower() in lowered:
            return friendly
    if len(raw) > max_len:
        return f"⚠️ {raw[:max_len]}…"
    return f"⚠️ {raw}"


def notify_error(message: str) -> None:
    """Push one human-friendly error to the user's bot chat (best-effort)."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        import requests as _r
        _r.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": humanize_error(message)},
            timeout=15,
        )
    except Exception:
        pass


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
    gw = _get_exec_gateway()
    if gw:
        portfolio = get_real_portfolio(gw, EXEC_BOT_ID)
        print(f"[exec] real mode: cash=${portfolio.get('cash', 0):,.2f} "
              f"positions={len(portfolio.get('positions', []))}")
    else:
        portfolio = get_portfolio(token)
    prices = {}
    price_txt = []
    context = {}
    closes_by_symbol: dict[str, list[float]] = {}
    for sym, market in UNIVERSE:
        try:
            px = get_price(token, sym, market)
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

    positions = portfolio.get("positions", [])
    pos_txt = "; ".join(
        f"{p['symbol']} {p['side']} qty={p['quantity']} entry={p['entry_price']:.2f} "
        f"current={p.get('current_price') or 'n/a'} stop={p.get('stop_loss')} take={p.get('take_profit')}"
        for p in positions
    ) or "none"
    eq = equity(portfolio, prices)
    used = daily_trade_count()

    # FUNDING-CARRY CONTEXT (option-one): surface live perp funding so the model
    # can weigh opening a carry short on highly-funded symbols. Only the symbol
    # with the largest |net carry| that is ALSO genuinely high (> carry_min_apy)
    # is surfaced, to avoid noise; below the floor we suppress it (a low carry
    # short in a bull just bleeds to price — the live-test lesson).
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
        f"Live trading decision — {now_iso} UTC.\n"
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
        # scenario matrix for every symbol — each with a real probability of
        # success (barrier-crossing GBM), reward/risk ratio, and expected value.
        # The LLM (with skills loaded) reads the full matrix + market context,
        # does the math compilation, and picks the highest-conviction scenario.
        # Risk guards clamp the chosen trade AFTER the LLM decides.
        _last_scenario = None
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from quant_strategy import (
                scenario_matrix, pick_best_scenario, trail_check, time_exit_check,
                partial_profit_check,
            )

            # update trailing peak tracker for open longs (per symbol)
            for p in positions:
                sym = p.get("symbol")
                if sym and p.get("quantity", 0) > 0:
                    px = prices.get(sym) or p.get("current_price") or p.get("entry_price") or 0
                    if px > 0:
                        _trailing_high[sym] = max(_trailing_high.get(sym, 0.0), px)
            for p in positions:
                sym = p.get("symbol")
                if sym and p.get("quantity", 0) > 0 and _trailing_high.get(sym):
                    p["high_price"] = _trailing_high[sym]

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
                        # build the full scenario matrix (long + short per symbol)
                        matrix = scenario_matrix(closes_by_symbol, prices)
                        has_long = {p["symbol"]: p["quantity"] > 0 for p in positions}
                        has_short = {p["symbol"]: p["quantity"] < 0 for p in positions}
                    # top candidates the LLM will choose among (ranked by conviction)
                    actionable = sorted([s for s in matrix if s.ev > 0],
                                        key=lambda s: s.conviction, reverse=True)
                    top = actionable[:8]
                    print(f"[quant] scenario matrix: {len(matrix)} scenarios, "
                          f"{len(actionable)} positive-EV")

                if not top:
                    decision = {"action": "hold", "symbol": "", "quantity": 0,
                                "stop_loss_pct": 0, "take_profit_pct": 0,
                                "reasoning": "scenario matrix: no positive-EV trade right now - cash"}
                    _last_scenario = None
                elif LIVE_AGENT_API_KEY:
                    # LLM compiles the matrix and picks the best trade
                    skill_ctx = _load_skill_context()
                    matrix_txt = "\n".join(s.to_prompt() for s in top)
                    system = (
                        "You are the decision layer of an automated trading agent. "
                        "You are GIVEN a scenario matrix computed with REAL "
                        "quantitative math: for each symbol there is a LONG and a "
                        "SHORT path, each with P(win) (the probability the take-profit "
                        "barrier is hit before the stop-loss, from a geometric Brownian "
                        "motion model), a reward/risk ratio R, and expected value EV "
                        "per unit risk. Your job: DO THE MATH and pick the single "
                        "best trade from the matrix — the one with the highest "
                        "conviction (P(win) * EV) that is also actionable given the "
                        "positions you already hold. This is not a vibe — use the "
                        "numbers.\n\n"
                        "THE STRATEGY SKILLS ARE LOADED BELOW. Follow them exactly; "
                        "do not invent rules that contradict them.\n\n"
                        f"{skill_ctx}\n\n"
                        "Reply with a single JSON object only:\n"
                        '{"action":"buy|sell","symbol":"<SYMBOL>",'
                        '"direction":"long|short",'
                        '"quantity":<notional risk size in units of the symbol>,'
                        '"reasoning":"<2-3 sentences: cite the P(win), EV, and why '
                        'this scenario beats the others>"}\n'
                        "IMPORTANT: stop and take are already set per scenario. "
                        "quantity = dollars-at-risk / entry price, "
                        "where dollars-at-risk is ~1% of equity. The system will "
                        "clamp your size afterward — stay conservative."
                    )
                    user = (
                        f"Scenario decision — {now_iso} UTC.\n"
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
                    llm_action = str(llm.get("action", "")).lower()
                    llm_dir = str(llm.get("direction", "")).lower()
                    llm_sym = str(llm.get("symbol", "")).upper()
                    if llm_action in ("buy", "sell") and llm_sym:
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
                        if qty > 0 and llm_sym in prices:
                            stop_pct = (abs(_last_scenario.entry - _last_scenario.stop) / _last_scenario.entry * 100) \
                                if _last_scenario is not None else 0.3
                            risk_notional = eq * 1.0 / 100.0 / (stop_pct / 100.0)
                            # cap at 30% of equity (position limit)
                            cap_notional = eq * 0.30
                            max_qty = min(risk_notional, cap_notional) / prices[llm_sym]
                            qty = min(qty, max_qty)
                        if _last_scenario is not None:
                            stop_pct = abs(_last_scenario.entry - _last_scenario.stop) / _last_scenario.entry * 100
                            take_pct = abs(_last_scenario.target - _last_scenario.entry) / _last_scenario.entry * 100
                        else:
                            stop_pct, take_pct = 8.0, 24.0  # fallback if no match
                        decision = {
                            "action": llm_action,
                            "symbol": llm_sym,
                            "quantity": qty if qty > 0 else 0,
                            "stop_loss_pct": round(stop_pct, 2),
                            "take_profit_pct": round(take_pct, 2),
                            "reasoning": f"[LLM scenario pick] {llm.get('reasoning','')[:240]}",
                        }
                        print(f"[agent] LLM PICKED {llm_dir.upper() or llm_action} {llm_sym} "
                              f"qty={qty:.4f} stop={stop_pct:.1f}% take={take_pct:.1f}% "
                              f":: {llm.get('reasoning','')[:60]}")
                    else:
                        print(f"[agent] LLM chose no trade ({llm.get('reasoning','')[:100]})")
                        decision = {"action": "hold", "symbol": "", "quantity": 0,
                                    "stop_loss_pct": 0, "take_profit_pct": 0,
                                    "reasoning": f"[LLM] {llm.get('reasoning','')[:200]}"}
                        _last_scenario = None
                else:
                    # no LLM key -> fall back to the math's best scenario
                    best = pick_best_scenario(matrix, has_long, has_short)
                    if best is None:
                        decision = {"action": "hold", "symbol": "", "quantity": 0,
                                    "stop_loss_pct": 0, "take_profit_pct": 0,
                                    "reasoning": "best scenario has non-positive EV - cash"}
                        _last_scenario = None
                    else:
                        side = "buy" if best.direction == "long" else "sell"
                        stop_pct = abs(best.entry - best.stop) / best.entry * 100
                        take_pct = abs(best.target - best.entry) / best.entry * 100
                        # correct retail sizing: risk$ = 1% equity / stop%, capped at 30% notional
                        risk_notional = eq * 1.0 / 100.0 / (stop_pct / 100.0)
                        cap_notional = eq * 0.30
                        max_qty = min(risk_notional, cap_notional) / best.entry
                        decision = {"action": side, "symbol": best.symbol,
                                    "quantity": max_qty,
                                    "stop_loss_pct": round(stop_pct, 2),
                                    "take_profit_pct": round(take_pct, 2),
                                    "reasoning": f"[quant] best scenario {best.direction} "
                                                 f"{best.symbol} EV={best.ev:+.2f}R"}
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
        is_forced_exit = decision.get("_forced_exit", False)
        if not is_forced_exit and not market_open(market):
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
            print(f"[dry] would {action} {qty} {symbol} [{market}] (stop {stop_pct}%, take {take_pct}%)")
            row["fill_ok"] = "dry"
        elif gw:
            # REAL EXECUTION: route through the gateway (VenueRouter -> adapter).
            fill = route_real_order(gw, EXEC_BOT_ID, symbol, market, row["action"], qty,
                                    stop_pct or None, take_pct or None,
                                    prices.get(symbol, 0) or 0,
                                    LIVE_AGENT_LEVERAGE if market == "crypto" else 1.0)
            row["fill_ok"] = fill.get("ok")
            row["error"] = fill.get("error", "")
            row["price"] = fill.get("price", row["price"])
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
            fill = execute_trade(token, symbol, market, row["action"], qty, stop_pct or None, take_pct or None,
                     leverage=LIVE_AGENT_LEVERAGE if market == "crypto" and LIVE_AGENT_LEVERAGE > 1 else None)
            row["fill_ok"] = fill["ok"]
            row["error"] = fill.get("error", "")
            row["price"] = fill.get("price", row["price"])
            if not fill.get("ok"):
                notify_error(fill.get("error", ""))
            print(f"[trade] {row['action']} {qty} {symbol} [{market}] @ {fill.get('price', 'n/a')} "
                  f"-> {'OK' if fill['ok'] else fill['error']}")
            if fill.get("ok") and _last_scenario is not None:
                _log_prediction(decision, _last_scenario.p_win, _last_scenario.R,
                                _last_scenario.ev, _last_scenario.drift_annual,
                                _last_scenario.vol_annual,
                                entry=_last_scenario.entry,
                                stop=_last_scenario.stop,
                                target=_last_scenario.target)
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
                            fill = execute_trade(token, pick.symbol,
                                                 dict(UNIVERSE).get(pick.symbol, "crypto"),
                                                 side, qty)
                            if fill.get("ok"):
                                print(f"[exit] {label}: {pick.symbol} - {pick.reasoning[:80]}")
                            break
                except Exception as exc:
                    pass
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