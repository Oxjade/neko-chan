"""Web app: the public track-record page and the operator console.

Replaces Telegram as the interface. Paper trading only: the console can
create, start and pause paper bots; it cannot switch a bot to live trading.

Environment:
  WEB_OPERATOR_PASSWORD  required to sign in to the console (no default)
  TG_VAULT_MASTER_KEY    Fernet key; required to create bots or store an AI key
  WEB_OPERATOR_ID        owner id for bots created here (default 1)
  WEB_MANAGE_AGENTS      "0" leaves agent processes to another supervisor

Run ONE supervisor of agent processes. If the Telegram master bot is also
running it has its own agent pool; set WEB_MANAGE_AGENTS=0 in that case.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import web_core

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
TG_BOT_DIR = REPO_ROOT / "service" / "tg_bot"

SESSION_COOKIE = "neko_session"
SESSION_TTL = 12 * 3600
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILS = 5

TRADABLE = ("BTC", "ETH", "SOL", "SUI", "HYPE")
TRADER_TYPES = ("scalp", "intraday", "auto")
RISK_PROFILES = ("conservative", "balanced", "aggressive")
INTERVALS = (60, 120, 300, 600)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")

_sessions: dict[str, float] = {}
_login_fails: dict[str, list[float]] = {}
_lock = threading.Lock()
_services: dict = {}


# ------------------------------------------------------------------ settings

def _operator_password() -> str:
    return os.getenv("WEB_OPERATOR_PASSWORD", "")


def _operator_id() -> int:
    try:
        return int(os.getenv("WEB_OPERATOR_ID", "1"))
    except ValueError:
        return 1


def _manages_agents() -> bool:
    return os.getenv("WEB_MANAGE_AGENTS", "1").strip() not in {"0", "false", "no"}


def _setup_missing() -> list[str]:
    missing = []
    if not _operator_password():
        missing.append("WEB_OPERATOR_PASSWORD")
    if not os.getenv("TG_VAULT_MASTER_KEY", ""):
        missing.append("TG_VAULT_MASTER_KEY")
    return missing


# ------------------------------------------------------------------ sessions

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _require_operator(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE, "")
    with _lock:
        expires = _sessions.get(token)
        if expires and expires > time.time():
            return
        _sessions.pop(token, None)
    raise HTTPException(status_code=401, detail="Sign in to use the console.")


def _require_json(request: Request) -> None:
    # A cross-site form cannot send application/json without a preflight, so
    # this plus the SameSite=Strict cookie closes cross-site request forgery.
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(status_code=415, detail="Send JSON.")


# ---------------------------------------------------------- web-owned tables

def _write_conn() -> sqlite3.Connection:
    path = web_core.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS web_bot_flags ("
        " bot_id INTEGER PRIMARY KEY, is_public INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE IF NOT EXISTS web_waitlist ("
        " email TEXT PRIMARY KEY, created_at TEXT NOT NULL);"
    )
    return conn


def _public_bot_ids() -> list[int]:
    conn = _write_conn()
    try:
        rows = conn.execute("SELECT bot_id FROM web_bot_flags WHERE is_public = 1").fetchall()
    finally:
        conn.close()
    paper = {b["id"] for b in web_core.list_bots() if (b.get("trading_mode") or "paper") == "paper"}
    return sorted(r["bot_id"] for r in rows if r["bot_id"] in paper)


# -------------------------------------------- registry / agent pool (lazy)

def _svc() -> dict:
    """Registry, paper store and agent pool, built on first use.

    They need the vault key, so the public page works without it and the
    console reports what is missing instead of crashing at import."""
    with _lock:
        if _services:
            return _services
        if not os.getenv("TG_VAULT_MASTER_KEY", ""):
            raise HTTPException(
                status_code=503,
                detail="TG_VAULT_MASTER_KEY is not set, so bots and keys cannot be stored.")
        if str(TG_BOT_DIR) not in sys.path:
            sys.path.append(str(TG_BOT_DIR))
        from agent_pool import AgentPool
        from key_vault import KeyVault
        from paper_store import PaperStore
        from store import Registry

        path = web_core.registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        registry = Registry(str(path), KeyVault())
        _services.update(
            registry=registry,
            paper=PaperStore(str(path)),
            pool=AgentPool(registry),
        )
        if _manages_agents():
            threading.Thread(target=_healthcheck_loop, daemon=True,
                             name="web-agent-healthcheck").start()
        return _services


def _healthcheck_loop() -> None:
    while True:
        time.sleep(60)
        try:
            _services["pool"].healthcheck()
        except Exception:
            pass


def _stop_stale_agent(bot: dict) -> None:
    """Stop an agent left over from a previous server process.

    Agents run in their own session and outlive a restart; starting a second
    one for the same bot would double every trade."""
    pid = bot.get("pid")
    if not pid:
        return
    try:
        cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ")
    except (OSError, ValueError):
        return
    if b"live_agent.py" not in cmdline:
        return
    try:
        os.killpg(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _bot_or_404(bot_id: int) -> dict:
    bot = web_core.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="No bot with that id.")
    return bot


def _bot_summary(bot: dict, public_ids: set[int]) -> dict:
    mode = bot.get("trading_mode") or "paper"
    if bot.get("paused"):
        state = "paused"
    elif bot.get("is_running"):
        state = "running"
    else:
        state = "stopped"
    return {
        "id": bot["id"],
        "name": bot["bot_name"],
        "mode": mode,
        "state": state,
        "trader_type": bot.get("trader_type") or "scalp",
        "risk_profile": bot.get("risk_profile") or "balanced",
        "interval_sec": bot.get("interval_sec"),
        "watchlist": [s for s in (bot.get("watchlist") or "").split(",") if s],
        "is_public": bot["id"] in public_ids,
        "last_error": bot.get("last_error"),
        "created_at": bot.get("created_at"),
    }


# -------------------------------------------------------------- request bodies

class LoginBody(BaseModel):
    password: str = Field(max_length=512)


class NewBotBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    watchlist: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    trader_type: str = "scalp"
    risk_profile: str = "balanced"
    interval_sec: int = 120


class PublicBody(BaseModel):
    is_public: bool


class AIKeyBody(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    api_key: str = Field(min_length=8, max_length=400)
    model: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=300)


class WaitlistBody(BaseModel):
    email: str = Field(max_length=254)


# --------------------------------------------------------------------- routes

def register_web_routes(app: FastAPI) -> None:
    # ---- public -----------------------------------------------------------

    @app.get("/api/web/public/track-record")
    async def public_track_record():
        ids = _public_bot_ids()
        report = web_core.combined_report(ids)
        report["bots"] = len(ids)
        report["mode"] = "paper"
        report["replay"] = web_core.replay_evidence()
        for trade in report["trades"]:
            # Sizes stay private: the public page shows outcomes, not the book.
            for key in ("qty", "notional", "gross", "fees", "net"):
                trade.pop(key, None)
        return report

    @app.post("/api/web/public/waitlist")
    async def join_waitlist(body: WaitlistBody, request: Request):
        _require_json(request)
        email = body.email.strip().lower()
        if not EMAIL_RE.match(email):
            raise HTTPException(status_code=422, detail="Enter a valid email address.")
        conn = _write_conn()
        try:
            count = conn.execute("SELECT COUNT(*) c FROM web_waitlist").fetchone()["c"]
            if count >= 50000:
                raise HTTPException(status_code=503, detail="The list is full for now.")
            conn.execute("INSERT OR IGNORE INTO web_waitlist (email, created_at) VALUES (?, ?)",
                         (email, web_core.utcnow()))
            conn.commit()
        finally:
            conn.close()
        return {"joined": True}

    # ---- session ----------------------------------------------------------

    @app.get("/api/web/session")
    async def session_state(request: Request):
        try:
            _require_operator(request)
            signed_in = True
        except HTTPException:
            signed_in = False
        return {"signed_in": signed_in, "setup_missing": _setup_missing()}

    @app.post("/api/web/login")
    async def login(body: LoginBody, request: Request):
        _require_json(request)
        expected = _operator_password()
        if not expected:
            raise HTTPException(
                status_code=503,
                detail="The console is off: set WEB_OPERATOR_PASSWORD on the server.")
        ip = _client_ip(request)
        now = time.time()
        with _lock:
            fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
            _login_fails[ip] = fails
            if len(fails) >= LOGIN_MAX_FAILS:
                raise HTTPException(status_code=429,
                                    detail="Too many attempts. Try again in 15 minutes.")
        if not hmac.compare_digest(body.password.encode(), expected.encode()):
            with _lock:
                _login_fails.setdefault(ip, []).append(now)
            raise HTTPException(status_code=401, detail="That password is not correct.")
        token = secrets.token_urlsafe(32)
        with _lock:
            for old, exp in list(_sessions.items()):
                if exp <= now:
                    _sessions.pop(old, None)
            _sessions[token] = now + SESSION_TTL
            _login_fails.pop(ip, None)
        response = JSONResponse({"signed_in": True})
        response.set_cookie(
            SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="strict",
            secure=request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https",
            path="/")
        return response

    @app.post("/api/web/logout")
    async def logout(request: Request):
        with _lock:
            _sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        response = JSONResponse({"signed_in": False})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # ---- console ----------------------------------------------------------

    @app.get("/api/web/console/bots")
    async def console_bots(request: Request):
        _require_operator(request)
        public_ids = set(_public_bot_ids())
        key = None
        if os.getenv("TG_VAULT_MASTER_KEY", ""):
            active = _svc()["registry"].get_active_key(_operator_id())
            if active:
                key = {"provider": active["provider"], "model": active.get("model")}
        return {
            "bots": [_bot_summary(b, public_ids) for b in web_core.list_bots()],
            "ai_key": key,
            "manages_agents": _manages_agents(),
            "setup_missing": _setup_missing(),
        }

    @app.post("/api/web/console/bots")
    async def create_bot(body: NewBotBody, request: Request):
        _require_operator(request)
        _require_json(request)
        watch = [s.strip().upper() for s in body.watchlist]
        if not watch or any(s not in TRADABLE for s in watch):
            raise HTTPException(status_code=422,
                                detail=f"Choose markets from {', '.join(TRADABLE)}.")
        if body.trader_type not in TRADER_TYPES:
            raise HTTPException(status_code=422, detail="Unknown trading style.")
        if body.risk_profile not in RISK_PROFILES:
            raise HTTPException(status_code=422, detail="Unknown risk profile.")
        if body.interval_sec not in INTERVALS:
            raise HTTPException(status_code=422, detail="Unknown decision interval.")
        svc = _svc()
        registry = svc["registry"]
        owner = _operator_id()
        registry.upsert_user(owner, "web-operator")
        name = body.name.strip()
        bot = registry.create_bot(
            owner, name, None, None,
            agent_name=f"web-{secrets.token_hex(4)}", platform_token="",
            symbols={"perps": 1, "spot": 0, "us-stock": 0, "forex": 0},
            leverage=1.0, interval_sec=body.interval_sec, risk_profile=body.risk_profile)
        registry.update_bot(
            bot["id"], trading_mode="paper", trader_type=body.trader_type,
            watchlist=",".join(dict.fromkeys(watch)), chain="sui", network="mainnet",
            onboarding_complete=1, paused=1, is_running=0)
        svc["paper"].ensure_portfolio(bot["id"])
        return _bot_summary(web_core.get_bot(bot["id"]), set())

    @app.get("/api/web/console/bots/{bot_id}")
    async def console_bot(bot_id: int, request: Request):
        _require_operator(request)
        bot = _bot_or_404(bot_id)
        summary = _bot_summary(bot, set(_public_bot_ids()))
        if summary["mode"] != "paper":
            return {"bot": summary, "report": None, "decisions": []}
        return {
            "bot": summary,
            "report": web_core.bot_report(bot_id),
            "decisions": web_core.recent_decisions(bot_id, 40),
        }

    @app.post("/api/web/console/bots/{bot_id}/start")
    async def start_bot(bot_id: int, request: Request):
        _require_operator(request)
        _require_json(request)
        bot = _bot_or_404(bot_id)
        if (bot.get("trading_mode") or "paper") != "paper":
            raise HTTPException(status_code=409,
                                detail="This bot trades live. The web console only runs paper bots.")
        svc = _svc()
        svc["registry"].update_bot(bot_id, paused=0, is_running=1)
        if _manages_agents():
            _stop_stale_agent(bot)
            if not svc["pool"].start(bot_id):
                svc["registry"].update_bot(bot_id, is_running=0, paused=1,
                                           last_error="agent process could not start")
                raise HTTPException(status_code=500, detail="The agent process could not start.")
        return _bot_summary(web_core.get_bot(bot_id), set(_public_bot_ids()))

    @app.post("/api/web/console/bots/{bot_id}/pause")
    async def pause_bot(bot_id: int, request: Request):
        _require_operator(request)
        _require_json(request)
        bot = _bot_or_404(bot_id)
        svc = _svc()
        if _manages_agents():
            svc["pool"].stop(bot_id)
            _stop_stale_agent(bot)
        svc["registry"].update_bot(bot_id, paused=1, is_running=0)
        return _bot_summary(web_core.get_bot(bot_id), set(_public_bot_ids()))

    @app.post("/api/web/console/bots/{bot_id}/public")
    async def set_public(bot_id: int, body: PublicBody, request: Request):
        _require_operator(request)
        _require_json(request)
        bot = _bot_or_404(bot_id)
        if body.is_public and (bot.get("trading_mode") or "paper") != "paper":
            raise HTTPException(status_code=409, detail="Only paper bots can be shown publicly.")
        conn = _write_conn()
        try:
            conn.execute(
                "INSERT INTO web_bot_flags (bot_id, is_public) VALUES (?, ?) "
                "ON CONFLICT(bot_id) DO UPDATE SET is_public = excluded.is_public",
                (bot_id, 1 if body.is_public else 0))
            conn.commit()
        finally:
            conn.close()
        return _bot_summary(bot, set(_public_bot_ids()))

    @app.post("/api/web/console/ai-key")
    async def save_ai_key(body: AIKeyBody, request: Request):
        _require_operator(request)
        _require_json(request)
        svc = _svc()
        from provider import ProviderError, validate_key
        provider = body.provider.strip().lower()
        try:
            model = validate_key(provider, body.api_key.strip(), body.base_url, body.model)
        except ProviderError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        owner = _operator_id()
        svc["registry"].upsert_user(owner, "web-operator")
        try:
            svc["registry"].store_key(owner, provider, body.api_key.strip(), body.base_url, model)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"provider": provider, "model": model}

    @app.get("/api/web/console/waitlist")
    async def console_waitlist(request: Request):
        _require_operator(request)
        conn = _write_conn()
        try:
            rows = conn.execute(
                "SELECT email, created_at FROM web_waitlist ORDER BY created_at DESC LIMIT 500"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) c FROM web_waitlist").fetchone()["c"]
        finally:
            conn.close()
        return {"total": total, "entries": [dict(r) for r in rows]}

    # ---- pages ------------------------------------------------------------

    if not WEB_DIR.exists():
        return

    app.mount("/web", StaticFiles(directory=str(WEB_DIR / "assets")), name="web-assets")

    @app.get("/", include_in_schema=False)
    async def public_page():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/console", include_in_schema=False)
    async def console_page():
        return FileResponse(WEB_DIR / "console.html", headers={"Cache-Control": "no-store"})
