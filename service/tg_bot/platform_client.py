"""AI-Trader platform REST wrapper (agents, positions, leaderboard, trades, prices)."""

import requests

from tg_config import PLATFORM_URL, PLATFORM_REGISTER_PASSWORD


class PlatformError(Exception):
    pass


class PlatformClient:
    def __init__(self, base_url: str = PLATFORM_URL, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self, token: str | None = None) -> dict:
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    # ---------------- agents ----------------

    def register_agent(self, name: str) -> dict:
        r = requests.post(
            f"{self.base}/api/claw/agents/selfRegister",
            headers=self._headers(),
            json={"name": name, "password": PLATFORM_REGISTER_PASSWORD},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise PlatformError(f"agent registration failed: {r.text[:160]}")
        return r.json()  # {token, agent_id, name, ...}

    # ---------------- state ----------------

    def positions(self, token: str) -> dict:
        r = requests.get(f"{self.base}/api/positions", headers=self._headers(token), timeout=self.timeout)
        if r.status_code != 200:
            raise PlatformError(f"positions failed: {r.text[:160]}")
        return r.json()

    def leaderboard(self, token: str, limit: int = 200) -> dict:
        r = requests.get(f"{self.base}/api/profit/history", headers=self._headers(token),
                         params={"limit": limit}, timeout=self.timeout)
        if r.status_code != 200:
            raise PlatformError(f"leaderboard failed: {r.text[:160]}")
        return r.json()

    def price(self, token: str, market: str, symbol: str) -> float:
        r = requests.get(
            f"{self.base}/api/price",
            headers=self._headers(token),
            params={"market": market, "symbol": symbol},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise PlatformError(f"price {symbol} failed: {r.text[:120]}")
        return float(r.json()["price"])

    def trade(self, token: str, market: str, symbol: str, action: str, quantity: float,
              stop_loss_pct: float | None = None, take_profit_pct: float | None = None,
              leverage: float | None = None) -> dict:
        payload = {
            "market": market, "symbol": symbol, "action": action,
            "quantity": quantity, "price": 0, "executed_at": "now",
        }
        if stop_loss_pct:
            payload["stop_loss_pct"] = stop_loss_pct
        if take_profit_pct:
            payload["take_profit_pct"] = take_profit_pct
        if leverage:
            payload["leverage"] = leverage
        r = requests.post(
            f"{self.base}/api/signals/realtime",
            headers=self._headers(token),
            json=payload,
            timeout=60,
        )
        if r.status_code != 200:
            return {"ok": False, "error": r.json().get("detail", r.text[:160])}
        return {"ok": True, **r.json()}

    # ---------------- helpers ----------------

    def agent_row(self, token: str, name: str) -> dict | None:
        """Leaderboard entry for one agent name."""
        try:
            lb = self.leaderboard(token)
        except PlatformError:
            return None
        for a in lb.get("top_agents", []):
            if a.get("name") == name:
                return a
        return None

    def signals(self, agent_id: int, limit: int = 15) -> list[dict]:
        """Recent operation signals for an agent."""
        r = requests.get(f"{self.base}/api/signals/{agent_id}", params={"limit": limit}, timeout=self.timeout)
        if r.status_code != 200:
            raise PlatformError(f"signals failed: {r.text[:160]}")
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("signals", []) if isinstance(data, dict) else []