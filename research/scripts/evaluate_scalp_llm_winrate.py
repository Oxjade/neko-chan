"""Win-rate eval for the scalp perp agent — LLM decides between best-LONG
and best-SHORT, mirroring the live agent's real decision path.

The quant builds scenario matrix (long + short per symbol). The live_agent
builds `top` = [best_long, best_short] + fill by conviction, then calls the
LLM to pick the single best trade. This eval does the same:
  - builds matrix, applies momentum + RSI + conviction floor
  - builds top = best_long + best_short + fill (mirroring live_agent)
  - calls the real LLM via _provider_completion when both directions exist
  - deterministic conviction pick otherwise (mirrors cooldown path)
  - walks forward to measure win/loss
"""

import os
import sys
import time
import json
import math

sys.path.insert(0, "/home/carnage/tradebotpro/service/agent")

# Load LIVE_AGENT_* env from the running agent process BEFORE importing
# live_agent, so its module-level LIVE_AGENT_API_KEY is populated.
agent_pid = None
for line in os.popen("pgrep -f live_agent.py").read().strip().splitlines():
    if line.strip():
        agent_pid = int(line.strip())
        break
if agent_pid:
    for line in open(f"/proc/{agent_pid}/environ").read().split("\0"):
        if line.startswith("LIVE_AGENT_"):
            k, _, v = line.partition("=")
            os.environ[k] = v

from quant_strategy import build_scenarios, rsi as rsi_fn
from quant_strategy import RSI_ENTRY_THRESHOLD, momentum_confirmed
from live_agent import balance_aware_size, _provider_completion

SYMBOLS = ["BTC", "ETH", "SOL", "SUI", "HYPE", "SEI", "NEAR", "ATOM"]
CONVICTION_FLOOR = 0.015
FEE_RT = 0.0009
BARS_PER_YEAR_5M = 288 * 365
LOOKBACK = 60
MAX_HOLD_BARS = 48
LLM_COOLDOWN = 900
MAX_LLM_CALLS = 12  # cap per simulate() to avoid rate limits / long runtime


def fetch_5m(symbol, hours=12):
    now = int(time.time() * 1000)
    r = __import__("requests").post("https://api.hyperliquid.xyz/info", json={
        "type": "candleSnapshot", "req": {"coin": symbol, "interval": "5m",
        "startTime": now - hours * 3600 * 1000, "endTime": now}}, timeout=15)
    out = []
    for c in r.json():
        out.append({"ts": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                    "l": float(c["l"]), "c": float(c["c"])})
    return out


def build_top(actionable, matrix_all):
    """Build the same `top` list as live_agent: best_long + best_short (from the
    FULL matrix, any EV - so both directions always reach the LLM) + fill slots
    (positive-EV, floor-passing) by conviction."""
    best_long = max((s for s in matrix_all if s.direction == "long"),
                    key=lambda s: s.conviction, default=None)
    best_short = max((s for s in matrix_all if s.direction == "short"),
                     key=lambda s: s.conviction, default=None)
    top = []
    for s in (best_long, best_short):
        if s is not None and s not in top:
            top.append(s)
    for s in actionable:
        if len(top) >= 8:
            break
        if s not in top:
            top.append(s)
    return top


def simulate(balance, use_llm=True):
    trades = []
    last_llm_at = 0.0
    llm_calls = 0

    for sym in SYMBOLS:
        bars = fetch_5m(sym, 12)
        if len(bars) < LOOKBACK + 30:
            continue
        i = LOOKBACK
        while i < len(bars) - 30:
            closes = [b["c"] for b in bars[:i]]
            px = bars[i]["c"]
            if px <= 0:
                i += 1
                continue
            sc = [s for s in build_scenarios(sym, closes, px,
                                             bars_per_year=BARS_PER_YEAR_5M)
                  if s.horizon == "scalp"]
            # momentum as a SOFT signal (mirrors live_agent): NOT a hard filter,
            # so both directions survive to the LLM. P(win) already bakes drift in.
            momentum_ok = {s.direction: momentum_confirmed(closes, s.direction) for s in sc}
            # RSI direction filter
            rsi = rsi_fn(closes)
            sc = [s for s in sc if
                  (s.direction == "long" and rsi >= RSI_ENTRY_THRESHOLD) or
                  (s.direction == "short" and rsi <= 100 - RSI_ENTRY_THRESHOLD)]
            # conviction floor
            actionable = [s for s in sc if s.ev > 0 and s.conviction >= CONVICTION_FLOOR]
            ev_pos = [s for s in sc if s.ev > 0]
            if not ev_pos:
                i += 1
                continue
            top = build_top(actionable, sc)
            has_both = any(s.direction == "long" for s in top) and any(s.direction == "short" for s in top)

            # Decide: real LLM (when both directions present) or deterministic.
            # Backtest mode: no wall-clock cooldown - measure the LLM's choice on
            # every cycle that presents both directions.
            now = time.time()
            if use_llm and has_both and llm_calls < MAX_LLM_CALLS:
                # Build the same system prompt as live_agent
                matrix_txt = "\n".join(
                    s.to_prompt()
                    + (" | MOMENTUM: CONFIRMED" if momentum_ok.get(s.direction) else " | MOMENTUM: against")
                    for s in top
                )
                system = (
                    "You are the decision layer of an automated trading agent. "
                    "Your trader type: <b>SCALP</b>. "
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
                    "Reply with a single JSON object only:\n"
                    '{"action":"buy|short","symbol":"<SYMBOL>",'
                    '"direction":"long|short",'
                    '"quantity":<notional risk size in units of the symbol>,'
                    '"reasoning":"<2-3 sentences: cite the P(win), EV, and why '
                    'this scenario beats the others>"}\n'
                    "IMPORTANT: action 'buy' opens a LONG, action 'short' opens "
                    "a SHORT. Only ever pick an open-side action. "
                    "stop and take are already set per scenario. "
                    "quantity = dollars-at-risk / entry price, "
                    "where dollars-at-risk is ~1% of equity. The system will "
                    "clamp your size afterward - stay conservative."
                )
                user = (
                    f"Scenario decision.\n"
                    f"SCENARIO MATRIX (ranked by conviction):\n{matrix_txt}\n\n"
                    f"Pick the best positive-EV scenario. A short is allowed if "
                    f"its P(win) is genuinely the best. Do not overtrade."
                )
                llm = _provider_completion(system, user)
                llm_calls += 1
                print(f"[eval] LLM call {llm_calls}: {str(llm.get('direction',''))} {str(llm.get('symbol',''))} | {str(llm.get('reasoning',''))[:80]}", flush=True)
                llm_reasoning = str(llm.get("reasoning", ""))
                if llm_reasoning.startswith("llm-") or llm_reasoning.startswith("parse-failed"):
                    # LLM failed — fall back to deterministic conviction pick
                    best = max(actionable, key=lambda s: s.conviction) if actionable else max(ev_pos, key=lambda s: s.conviction)
                    best_src = "det"
                else:
                    llm_dir = str(llm.get("direction", "")).lower()
                    llm_sym = str(llm.get("symbol", "")).upper()
                    # Find the best scenario the LLM intended in the FULL matrix
                    candidates = [s for s in sc if s.direction == llm_dir and s.symbol == llm_sym]
                    if candidates:
                        best = max(candidates, key=lambda s: s.conviction)
                        # Guard: skip if below floor (mirrors live_agent rejection)
                        if best.ev <= 0 or best.conviction < CONVICTION_FLOOR:
                            i += 1
                            continue
                        best_src = "llm"
                    else:
                        # LLM picked a mismatch — fall back to deterministic
                        best = max(actionable, key=lambda s: s.conviction) if actionable else max(ev_pos, key=lambda s: s.conviction)
                        best_src = "det"
                last_llm_at = now
                llm_calls += 1
            else:
                best = max(actionable, key=lambda s: s.conviction) if actionable else max(ev_pos, key=lambda s: s.conviction)
                best_src = "det"

            # Size with the live leverage logic
            stop_pct = abs(best.entry - best.stop) / best.entry * 100
            qty, lev, _ = balance_aware_size(balance, balance, px, stop_pct, sym)
            if qty <= 0:
                i += 1
                continue
            notional = qty * px
            # Walk forward
            entry = px
            stop, target = best.stop, best.target
            win = None
            exit_reason = ""
            for j in range(i + 1, min(i + MAX_HOLD_BARS, len(bars))):
                hb, lb = bars[j]["h"], bars[j]["l"]
                if best.direction == "long":
                    if hb >= target:
                        win = True; exit_reason = "target"; break
                    if lb <= stop:
                        win = False; exit_reason = "stop"; break
                else:
                    if lb <= target:
                        win = True; exit_reason = "target"; break
                    if hb >= stop:
                        win = False; exit_reason = "stop"; break
            if win is None:
                final = bars[min(i + MAX_HOLD_BARS, len(bars) - 1)]["c"]
                win = (final > entry) if best.direction == "long" else (final < entry)
                exit_reason = "time"
            R = best.R
            gross = R if win else -1.0
            net = gross - FEE_RT
            trades.append({"sym": sym, "dir": best.direction, "win": win,
                           "exit": exit_reason, "R": R, "net": net,
                           "p_win": best.p_win, "conv": best.conviction,
                           "lev": lev, "notional": notional,
                           "notional_pct": notional / balance * 100,
                           "src": best_src})
            i += 1
    return trades, llm_calls


def report(balance):
    trades, llm_calls = simulate(balance, use_llm=True)
    if not trades:
        print(f"balance ${balance}: no trades")
        return
    n = len(trades)
    wins = sum(1 for t in trades if t["win"])
    wr = wins / n * 100
    gross = sum(t["R"] for t in trades if t["win"])
    losses = sum(1 for t in trades if not t["win"])
    pf = gross / max(losses, 1)
    exp = sum(t["net"] for t in trades) / n
    llm_trades = [t for t in trades if t["src"] == "llm"]
    det_trades = [t for t in trades if t["src"] == "det"]
    print(f"\n=== BALANCE ${balance:,} ({llm_calls} LLM calls, {len(llm_trades)} LLM trades, {len(det_trades)} det trades) ===")
    print(f"Trades: {n} | Win rate: {wr:.1f}% | PF: {pf:.2f} | "
          f"Expectancy: {exp:+.2f}R/trade")
    print(f"Avg leverage: {sum(t['lev'] for t in trades)/n:.1f}x | "
          f"Avg notional: {sum(t['notional_pct'] for t in trades)/n:.0f}% of balance")
    for d in ("long", "short"):
        dt = [t for t in trades if t["dir"] == d]
        if not dt:
            continue
        dw = sum(1 for t in dt if t["win"])
        print(f"  {d:5s}: {len(dt):3d} trades, {dw/len(dt)*100:.0f}% win, "
              f"{sum(t['net'] for t in dt)/len(dt):+.2f}R exp")
    if llm_trades:
        lw = sum(1 for t in llm_trades if t["win"])
        print(f"  LLM  : {len(llm_trades):3d} trades, {lw/len(llm_trades)*100:.0f}% win, "
              f"{sum(t['net'] for t in llm_trades)/len(llm_trades):+.2f}R exp")
    if det_trades:
        dw = sum(1 for t in det_trades if t["win"])
        print(f"  DET  : {len(det_trades):3d} trades, {dw/len(det_trades)*100:.0f}% win, "
              f"{sum(t['net'] for t in det_trades)/len(det_trades):+.2f}R exp")
    buckets = {}
    for t in trades:
        b = int(t["lev"] // 10 * 10)
        buckets.setdefault(b, []).append(t)
    print("  leverage buckets:")
    for b in sorted(buckets):
        bt = buckets[b]
        bw = sum(1 for t in bt if t["win"])
        print(f"    {b}-{b+9}x: {len(bt)} trades, {bw/len(bt)*100:.0f}% win, "
              f"{sum(t['net'] for t in bt)/len(bt):+.2f}R exp")


if __name__ == "__main__":
    for bal in (1000, 5000, 10000):
        report(bal)