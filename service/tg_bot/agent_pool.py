"""Agent pool: one live_agent.py subprocess per active bot, with the user's
provider credentials passed via env (never argv)."""

import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone

from tg_config import RUNNER_SCRIPT, RISK_PRESETS
from store import utcnow


def _symbols_to_universe(symbols: dict, leverage: float,
                         watchlist: str | None = None) -> str:
    """Analysis universe for the agent. Crypto mapping must cover the FULL
    tradable set (BTC/ETH/SOL/SUI/HYPE) — mapping only BTC/ETH meant a newly
    watched asset (SOL, SUI, HYPE...) was never analyzed and never traded.
    Watchlist symbols are prepended so they're always considered first.
    Deduped by base symbol so "BTC" (watchlist) and "BTC:crypto" (default)
    never both appear — duplicates double the per-cycle candle fetches."""
    parts: list[str] = []
    seen: set[str] = set()

    def _add(entry: str) -> None:
        base = entry.split(":")[0].strip().upper()
        if base and base not in seen:
            seen.add(base)
            parts.append(entry)

    if watchlist:
        for s in (watchlist or "").split(","):
            s = s.strip().upper()
            if s:
                # bare watchlist symbols default to the crypto perp market
                _add(s if ":" in s else f"{s}:crypto")
    if symbols.get("perps") or symbols.get("spot"):
        for c in ("BTC:crypto", "ETH:crypto", "SOL:crypto", "SUI:crypto", "HYPE:crypto"):
            _add(c)
    if symbols.get("us-stock"):
        for s in ("AAPL:us-stock", "NVDA:us-stock", "SPY:us-stock"):
            _add(s)
    if symbols.get("forex"):
        for s in ("EURUSD:forex", "USDJPY:forex", "GBPUSD:forex"):
            _add(s)
    return ",".join(parts) if parts else "BTC:crypto"


class AgentPool:
    def __init__(self, registry):
        self.registry = registry
        self._procs: dict[int, subprocess.Popen] = {}
        self._restart_counts: dict[int, list[float]] = {}
        self._lock = threading.Lock()

        # MIGRATION: stop() used to reset this as an int (0); the healthcheck
        # iterates it as a list of timestamps. An int here crashed every
        # healthcheck with "'int' object is not iterable", so dead agents were
        # never respawned. Coerce any legacy int to an empty list on boot.
        for _bid, _v in list(self._restart_counts.items()):
            if not isinstance(_v, list):
                self._restart_counts[_bid] = []

    def start(self, bot_id: int) -> bool:
        """Spawn a runner for the bot with its key/provider/risk config.

        KEYLESS MODE: no AI API key is fine — the agent falls back to the
        deterministic quant engine (the LLM is an upgrade, not a requirement).
        Users connect a key later for AI-powered decisions."""
        bot = self.registry.get_bot(bot_id)
        if not bot:
            return False
        key = self.registry.get_active_key(bot["tg_id"])
        token = self.registry.bot_token(bot_id)
        if not token:
            return False

        with self._lock:
            if bot_id in self._procs and self._procs[bot_id].poll() is None:
                return True

        caps = RISK_PRESETS.get(bot["risk_profile"], RISK_PRESETS["balanced"])
        try:
            markets = json.loads(bot["symbols"]) if isinstance(bot["symbols"], str) else dict(bot["symbols"] or {})
        except Exception:
            markets = {"perps": 1, "spot": 0, "us-stock": 0, "forex": 0}
        env = os.environ.copy()
        env.update({
            "LIVE_AGENT_SYMBOLS": _symbols_to_universe(
                markets, float(bot.get("leverage") or 1.0),
                bot.get("watchlist") or ""),
            "LIVE_AGENT_INTERVAL": str(bot["interval_sec"]),
            "LIVE_AGENT_ACTIVE_MODE": str(caps["active_mode"]),
            "LIVE_AGENT_MAX_DAILY_TRADES": str(caps["max_daily_trades"]),
            "LIVE_AGENT_MAX_POSITION_PCT": str(caps["max_position_pct"]),
            "LIVE_AGENT_FORCE_STOP_PCT": str(caps["force_stop_pct"]),
            "LIVE_AGENT_LEVERAGE": str(bot.get("leverage") or 1),
            "LIVE_AGENT_API_KEY": key["api_key"] if key else "",
            "LIVE_AGENT_PROVIDER": key["provider"] if key else "",
            "LIVE_AGENT_BASE_URL": key.get("base_url") or "" if key else "",
            "LIVE_AGENT_MODEL": key.get("model") or "gpt-4o-mini" if key else "",
            "LIVE_AGENT_TOKEN": bot["platform_token"],
            "LIVE_AGENT_NAME": bot["agent_name"],
            "LIVE_AGENT_BOT_ID": str(bot["id"]),
            "LIVE_AGENT_EXECUTION": os.environ.get("LIVE_AGENT_EXECUTION", "0"),
            "LIVE_AGENT_STRATEGY": os.environ.get("LIVE_AGENT_STRATEGY", "momentum20"),
            "LIVE_AGENT_TRADER_TYPE": bot.get("trader_type") or "scalp",
            "LIVE_AGENT_NETWORK": bot.get("network") or "mainnet",
            "LIVE_AGENT_CHAIN": bot.get("chain") or "sui",
            "LIVE_AGENT_WATCHLIST": bot.get("watchlist") or "",
            # PRIORITY WATCH: set by 'watch <ASSET> now' — the agent must
            # analyze THIS symbol next cycle and take its next valid setup,
            # overriding cooldown/one-shot/direction locks for it.
            "LIVE_AGENT_PRIORITY": (bot.get("priority_watch") or "").strip().upper(),
            # trading mode: every new bot defaults to paper ($1,000 virtual);
            # live only after the user flips the dashboard toggle.
            "LIVE_AGENT_TRADING_MODE": bot.get("trading_mode") or "paper",
            # for pushing human-friendly error notifications straight to the user
            "TG_BOT_TOKEN": self.registry.bot_token(bot_id) or "",
            "TG_CHAT_ID": str(bot["tg_id"]),
        })
        try:
            proc = subprocess.Popen(
                [sys_executable(), "-u", str(RUNNER_SCRIPT)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return False
        with self._lock:
            self._procs[bot_id] = proc
        self.registry.update_bot(bot_id, is_running=1, pid=proc.pid, last_error=None)
        return True

    def stop(self, bot_id: int):
        with self._lock:
            proc = self._procs.pop(bot_id, None)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.registry.update_bot(bot_id, is_running=0, pid=None)
        self._restart_counts[bot_id] = []

    def healthcheck(self, max_restarts_per_hour: int = 6):
        """Respawn dead agents. Trading must CONTINUE: a crash burst never
        permanently disables a bot — when the per-hour cap is hit we simply
        wait for the window to clear and try again (no is_running=0 marking)."""
        now = time.time()
        for bot in self.registry.all_bots():
            bot_id = bot["id"]
            if not bot["is_running"] or bot.get("paused"):
                continue
            proc = self._procs.get(bot_id)
            if proc is not None and proc.poll() is None:
                continue
            hour_window = [t for t in self._restart_counts.get(bot_id, []) if now - t < 3600]
            if len(hour_window) >= max_restarts_per_hour:
                continue
            if self.start(bot_id):
                self._restart_counts[bot_id] = hour_window + [now]

    def start_all_active(self):
        for bot in self.registry.all_bots():
            if bot["is_running"]:
                self.start(bot["id"])


def sys_executable() -> str:
    import sys

    return sys.executable