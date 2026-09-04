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


def _symbols_to_universe(symbols: dict, leverage: float) -> str:
    parts = []
    if symbols.get("perps"):
        parts += ["BTC:crypto", "ETH:crypto"]
    if symbols.get("spot"):
        parts += ["BTC:crypto", "ETH:crypto"]
    if symbols.get("us-stock"):
        parts += ["AAPL:us-stock", "NVDA:us-stock", "SPY:us-stock"]
    if symbols.get("forex"):
        parts += ["EURUSD:forex", "USDJPY:forex", "GBPUSD:forex"]
    return ",".join(parts) if parts else "BTC:crypto"


class AgentPool:
    def __init__(self, registry):
        self.registry = registry
        self._procs: dict[int, subprocess.Popen] = {}
        self._restart_counts: dict[int, int] = {}
        self._lock = threading.Lock()

    def start(self, bot_id: int) -> bool:
        """Spawn a runner for the bot with its key/provider/risk config."""
        bot = self.registry.get_bot(bot_id)
        if not bot:
            return False
        key = self.registry.get_active_key(bot["tg_id"])
        if not key:
            return False
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
            "LIVE_AGENT_SYMBOLS": _symbols_to_universe(markets, float(bot.get("leverage") or 1.0)),
            "LIVE_AGENT_INTERVAL": str(bot["interval_sec"]),
            "LIVE_AGENT_ACTIVE_MODE": str(caps["active_mode"]),
            "LIVE_AGENT_MAX_DAILY_TRADES": str(caps["max_daily_trades"]),
            "LIVE_AGENT_MAX_POSITION_PCT": str(caps["max_position_pct"]),
            "LIVE_AGENT_FORCE_STOP_PCT": str(caps["force_stop_pct"]),
            "LIVE_AGENT_LEVERAGE": str(bot.get("leverage") or 1),
            "LIVE_AGENT_API_KEY": key["api_key"],
            "LIVE_AGENT_PROVIDER": key["provider"],
            "LIVE_AGENT_BASE_URL": key.get("base_url") or "",
            "LIVE_AGENT_MODEL": key.get("model") or "gpt-4o-mini",
            "LIVE_AGENT_TOKEN": bot["platform_token"],
            "LIVE_AGENT_NAME": bot["agent_name"],
            "LIVE_AGENT_BOT_ID": str(bot["id"]),
            "LIVE_AGENT_EXECUTION": os.environ.get("LIVE_AGENT_EXECUTION", "0"),
            "LIVE_AGENT_STRATEGY": os.environ.get("LIVE_AGENT_STRATEGY", "momentum20"),
            "LIVE_AGENT_TRADER_TYPE": bot.get("trader_type") or "scalp",
            "LIVE_AGENT_NETWORK": bot.get("network") or "mainnet",
            "LIVE_AGENT_CHAIN": bot.get("chain") or "sui",
            "LIVE_AGENT_WATCHLIST": bot.get("watchlist") or "",
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
        self._restart_counts[bot_id] = 0

    def healthcheck(self, max_restarts_per_hour: int = 3):
        """Restart crashed runners up to the limit; flag beyond that."""
        now = time.time()
        for bot_id, proc in list(self._procs.items()):
            if proc.poll() is None:
                continue
            hour_window = [t for t in self._restart_counts.get(bot_id, []) if now - t < 3600]
            if len(hour_window) >= max_restarts_per_hour:
                self.registry.update_bot(bot_id, is_running=0,
                                         last_error="crashed too often (paused)")
                self._procs.pop(bot_id, None)
                continue
            self.start(bot_id)
            self._restart_counts[bot_id] = hour_window + [time.time()]

    def start_all_active(self):
        for bot in self.registry.all_bots():
            if bot["is_running"]:
                self.start(bot["id"])


def sys_executable() -> str:
    import sys

    return sys.executable