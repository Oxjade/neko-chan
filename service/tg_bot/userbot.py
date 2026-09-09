"""User bot serving: one python-telegram-bot Application per registered bot.

First-run onboarding (welcome -> AI API key -> dashboard), then a detailed,
polished dashboard: P&L, positions, live markets with sentiment, trades feed,
settings, inbox. All data comes from the AI-Trader platform in real time.
"""

import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import telegram
from telegram import Update
from telegram.ext import (Application, ContextTypes, CommandHandler,
                          CallbackQueryHandler, ConversationHandler,
                          MessageHandler, TypeHandler, filters)
from telegram.request import HTTPXRequest

from messages import USERBOT, WIZARD, NOTIF, ONBOARD, mask_key, humanize_error
from store import utcnow
from provider import validate_key, ProviderError

# Aftermath public perp API — the SAME pricing source the trading agent and
# paper fills use, so dashboard marks/PnL match real fills. Overridable via
# env like the agent (AFTERMATH_API_BASE / AFTERMATH_TESTNET_API_BASE).
AFTERMATH_API = os.getenv("AFTERMATH_API_BASE", "https://aftermath.finance/api").rstrip("/")
AFTERMATH_TESTNET_API = os.getenv("AFTERMATH_TESTNET_API_BASE",
                                  "https://testnet.aftermath.finance/api").rstrip("/")


def _master_token_fallback() -> str:
    """Master bot token from tg_config when TG_BOT_TOKEN isn't in the env."""
    try:
        from tg_config import require_master_token
        return require_master_token()
    except Exception:  # noqa: BLE001
        return ""

BACK = "↩️ Back"
HOME = "🏠 Home"
CANCEL = "❌ Cancel"

# key onboarding states
K_PROVIDER, K_KEY = range(2)
# send-funds states
S_ADDR, S_AMOUNT = range(2)


def _chain_label(chain: str) -> str:
    return {"sui": "Sui (Aftermath)", "solana": "Solana (Jupiter)", "hyperliquid": "Hyperliquid"}.get(
        chain, chain.title())


def _money(v: float, sign: bool = True) -> str:
    return f"${v:+,.2f}" if sign else f"${v:,.2f}"


def _esc(v) -> str:
    """Escape untrusted strings for Telegram parse_mode=HTML (bot names,
    symbols, reasoning - anything that can contain < > & breaks rendering)."""
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mask_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


async def _safe_edit(q, text, parse_mode="HTML", reply_markup=None):
    """edit_text that ignores the harmless 'Message is not modified' error so
    repeat taps (Refresh / Back / Check Deposits) never throw BadRequest."""
    try:
        await q.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        raise


def _delayed_photo_delete(bot_token: str, chat_id: int, message_id: int,
                          ttl: int = 300):
    """Delete a photo message after ttl seconds (background thread)."""
    import requests as _r
    time.sleep(ttl)
    try:
        _r.post(f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id}, timeout=10)
    except Exception:
        pass


def _schedule_msg_delete(bot_token: str, chat_id: int, message_id: int,
                          ttl: int = 300) -> None:
    """Delete a text message after ttl seconds (background thread). Reuses the
    same delete path as photo messages - private keys self-destruct so they
    don't persist in Telegram history."""
    threading.Thread(
        target=lambda: (_ for _ in ()).throw(TypeError("noop")) if False else _delayed_photo_delete(
            bot_token, chat_id, message_id, ttl),
        daemon=True).start()


def render_production_dashboard(bot: dict, account: dict, chain: str,
                                equity: dict | None = None) -> str:
    """Simple production dashboard: real chain balance + address + positions.

    `equity` is an optional sui_equity.sui_equity() snapshot used to show a
    real Equity (wallet USDC + Aftermath collateral + unrealized PnL) and the
    on-chain breakdown, instead of a hard-coded baseline."""
    line = "─" * 26
    bal = account.get("balances") or {}
    usdc = float(bal.get("USDC", 0))
    native = float(bal.get("native", 0))
    positions = account.get("positions") or []
    realized = float(bal.get("realized_pnl", 0))
    addr = str(account.get("wallet_address") or "") or ""
    equity_lines = ""
    if equity:
        e_usdc = float(equity.get("usdc", 0))
        e_coll = float(equity.get("collateral", 0))
        e_unreal = float(equity.get("unrealized_pnl", 0))
        e_sui = float(equity.get("sui", 0))
        e_sui_val = float(equity.get("sui_value", 0))
        e_eq = float(equity.get("equity", 0))
        bits = [f"  Wallet USDC <code>{_money(e_usdc, sign=False)}</code>",
                f"  Aftermath   <code>{_money(e_coll, sign=False)}</code>"]
        # Aftermath incentives: liquid rewards waiting to be claimed
        rewards = equity.get("rewards") or {}
        if rewards.get("claimable_sui"):
            bits.append(f"  🎁 Rewards   <code>{rewards['claimable_sui']:.4f} SUI</code> (claimable)")
        if abs(e_unreal) >= 0.005:
            bits.append(f"  Unrealized  <code>{_money(e_unreal)}</code>")
        if e_sui:
            bits.append(f"  SUI         <code>{e_sui:,.4f} (${e_sui_val:,.2f})</code>")
        equity_lines = ("\n<b>💎 EQUITY</b>\n" + "\n".join(bits)
                        + f"\n  <b>Total <code>{_money(e_eq, sign=False)}</code></b>")
    pos_lines = []
    if not positions:
        pos_lines.append("  no open positions")
    for p in positions[:3]:
        sym = str(p.get("symbol") or p.get("coin") or "?")
        side = str(p.get("side") or (p.get("szi", 0) > 0 and "long") or "short")
        qty = abs(float(p.get("qty") or p.get("szi") or p.get("quantity") or 0))
        pnl = float(p.get("pnl") or p.get("unrealized_pnl") or 0)
        entry = float(p.get("entry") or p.get("entry_px") or p.get("entry_price") or 0)
        cur = float(p.get("markPrice") or p.get("mark_price") or p.get("current_price") or entry)
        stop = p.get("stop") or p.get("stop_loss") or ""
        tgt = p.get("target") or p.get("take_profit") or ""
        meta = ""
        if entry and cur:
            meta += f" entry {entry:,.4f} → {cur:,.4f}"
        if stop:
            meta += f" · stop {stop}"
        if tgt:
            meta += f" · target {tgt}"
        pos_lines.append(f"  {_esc(sym)}  {side.upper()} {qty:g}  {_money(pnl)}{meta}")
    return (
        f"<b>🐾 {_esc(bot['bot_name'])}</b>\n"
        f"<code>{line}</code>\n"
        f"{_chain_label(chain)}\n\n"
        f"<b>💰 BALANCE</b>\n"
        f"  USDC <code>{_money(usdc, sign=False)}</code>\n"
        f"{f'  SUI <code>{native:,.4f}</code>' if native else ''}\n"
        f"{f'  realized {_money(realized)}' if realized else ''}\n"
        f"{equity_lines}\n"
        f"{('<b>🔗 ADDRESS</b>\n  <code>' + _esc(addr) + '</code>') if addr else ''}\n"
        f"<b>📡 POSITIONS ({len(positions)})</b>\n" + "\n".join(pos_lines) + "\n"
        f"<code>{line}</code>"
    )


def _ago(iso: str | None) -> str:
    if not iso:
        return "n/a"
    try:
        dt = datetime.fromisoformat(iso)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        return f"{secs // 60}m ago"
    except Exception:
        return "n/a"


def _sparkline(equity: list[float], width: int = 12) -> str:
    """ASCII equity sparkline like ▁▃▅▆▇█."""
    if not equity:
        return "(no history yet)"
    lo, hi = min(equity), max(equity)
    if hi - lo < 1e-9:
        return "▁" * min(width, len(equity))
    bars = "▁▂▃▄▅▆▇█"
    step = max(1, len(equity) // width)
    pts = equity[::step][:width]
    return "".join(bars[min(len(bars) - 1, int((p - lo) / (hi - lo) * (len(bars) - 1)))] for p in pts)


def render_dashboard_text(bot: dict, portfolio: dict, lb_row: dict | None = None) -> str:
    """Pure: bot + portfolio + leaderboard row -> full-screen telemetry panel (HTML)."""
    cash = portfolio.get("cash", 0)
    open_pos = portfolio.get("positions", [])
    pnl = 0.0
    for p in open_pos:
        cur = p.get("current_price") or p["entry_price"]
        q = p["quantity"]
        pnl += (cur - p["entry_price"]) * q if q >= 0 else (p["entry_price"] - cur) * abs(q)
    profit = cash + pnl - 100000
    ret = lb_row.get("total_profit_percent", profit / 1000) if lb_row else profit / 1000
    dd = lb_row.get("max_drawdown", 0) if lb_row else 0.0
    tcount = lb_row.get("trade_count", 0) if lb_row else 0
    rank = lb_row.get("rank", "–") if lb_row else "–"

    # today / 7d from profit history points (leaderboard history: {profit, recorded_at})
    hist = (lb_row or {}).get("history") or []
    def _profit_of(h):
        return float(h.get("profit", 0)) if isinstance(h, dict) else 0.0
    today = _profit_of(hist[-1]) - _profit_of(hist[-2]) if len(hist) >= 2 else None
    week = _profit_of(hist[-1]) - _profit_of(hist[0]) if len(hist) >= 2 else None

    line = "─" * 30
    def money(v, sign=True):
        return f"${v:+,.2f}" if sign else f"${v:,.2f}"

    pos_lines = []
    if not open_pos:
        pos_lines.append("  (no open positions - waiting for a setup)")
    for p in open_pos[:3]:
        cur = p.get("current_price") or p["entry_price"]
        q = p["quantity"]
        pp = (cur - p["entry_price"]) * q if q >= 0 else (p["entry_price"] - cur) * abs(q)
        lev = p.get("leverage") or 1.0
        icon = "⚡" if lev and lev > 1 else "📈"
        pos_lines.append(
            f"  {icon} <b>{p['symbol']}</b> {p['side'].upper()} {q}  "
            f"<code>{p['entry_price']:.2f} → {cur:.2f}</code>  {money(pp)}"
        )
        if lev and lev > 1:
            pos_lines.append(f"     {lev:g}x · stop {p.get('stop_loss')} · target {p.get('take_profit')}")
        else:
            pos_lines.append(f"     stop {p.get('stop_loss')} · target {p.get('take_profit')}")

    status = ("⏸️ TRADING PAUSED" if bot.get("paused") else ("🟢 RUNNING" if bot.get("is_running") else "⛔ OFFLINE"))
    ago = _ago(bot.get("last_heartbeat"))
    trade_total = lb_row.get("trade_count", tcount) if lb_row else tcount
    fees_paid = lb_row.get("fees_paid", 0.0) if lb_row else 0.0
    win_rate = lb_row.get("win_rate", 0) if lb_row else 0

    return (
        f"<b>🤖 {bot['bot_name']} - LIVE TELEMETRY</b>\n"
        f"<code>{line}</code>\n"
        f"{status} · heartbeat {ago} · rank #{rank}\n\n"
        f"<b>💰 EQUITY &amp; P&amp;L</b>\n"
        f"  Equity        <code>{money(profit + 100000, sign=False)}</code>\n"
        f"  Total P&amp;L    <code>{money(profit)}</code>  ({ret:+.2f}%)\n"
        f"  Today         <code>{money(today) if today is not None else '-'}</code>\n"
        f"  7d            <code>{money(week) if week is not None else '-'}</code>\n"
        f"  Max drawdown  <code>{dd * 100:.2f}%</code>\n"
        f"  Cash free     <code>{money(cash, sign=False)}</code>\n\n"
        f"<b>📈 PERFORMANCE</b>\n"
        f"  Closed trades <code>{trade_total}</code>\n"
        f"  Win rate      <code>{win_rate}%</code>\n"
        f"  Fees paid     <code>{money(fees_paid)}</code>\n\n"
        f"<b>📡 POSITIONS ({len(open_pos)})</b>\n" + "\n".join(pos_lines) + "\n\n"
        f"<b>🔮 NEXT DECISION</b> in ~{max(0, int(bot.get('interval_sec', 120)))}s "
        f"· interval {bot.get('interval_sec', 120)}s\n"
        f"<code>{line}</code>"
    )


def render_positions_text(portfolio: dict) -> str:
    pos = portfolio.get("positions", [])
    lines = [USERBOT["positions_header"].format(n=len(pos))]
    if not pos:
        lines.append("No open positions. Your bot will trade when it sees a setup.")
    for p in pos[:5]:
        cur = p.get("current_price") or p["entry_price"]
        qty = p["quantity"]
        pnl = (cur - p["entry_price"]) * qty if qty >= 0 else (p["entry_price"] - cur) * abs(qty)
        lev = p.get("leverage") or 1.0
        lines.append(f"{'⚡' if lev and lev > 1 else '📈'} {p['symbol']}  {p['side'].upper()}  {qty}  "
                     f"entry {p['entry_price']:.2f}  now {cur:.2f}  {pnl:+,.2f}")
        if lev and lev > 1:
            lines.append(f"    {lev:g}x · stop {p.get('stop_loss')} · take {p.get('take_profit')}")
        else:
            lines.append(f"    stop {p.get('stop_loss')} · take {p.get('take_profit')}")
    if len(pos) > 5:
        lines.append(f"…and {len(pos) - 5} more")
    return "\n".join(lines)


class UserBotController:
    """Builds and tracks one Application per user bot."""

    def __init__(self, registry, platform, vault=None, agent_pool=None, gateway=None):
        self._dead_token_notified: dict[int, float] = {}  # bot_id -> ts
        self.registry = registry
        self.platform = platform
        self.vault = vault
        self.agent_pool = agent_pool
        self.gateway = gateway  # ExecGateway (on-chain execution) or None
        self._apps: dict[int, Application] = {}
        self._lock = threading.Lock()
        import tg_config as _cfg
        self._master_token = _cfg.MASTER_BOT_TOKEN or ""
        self._paper_store_instance = None  # shared PaperStore (one per process)

    def _paper_store(self):
        """SHARED PaperStore for the whole bot process. Creating a new
        PaperStore per dashboard click ran DDL (executescript) and grabbed a
        write lock on registry.db each time — colliding with the agent's
        writes and destabilizing the agents. One instance = one connection."""
        if self._paper_store_instance is None:
            from paper_store import PaperStore
            self._paper_store_instance = PaperStore(self.registry.path)
        return self._paper_store_instance

    def _paper_gateway(self):
        """PaperGateway over the shared PaperStore (dashboard-side closes)."""
        if getattr(self, "_paper_gw", None) is None:
            from paper_gateway import PaperGateway
            self._paper_gw = PaperGateway(self._paper_store())
        return self._paper_gw

    def _paper_mark_prices(self, symbols: list[str], network: str = "mainnet") -> dict:
        """Current prices for paper positions from Aftermath's orderbook —
        the same venue the agent trades and paper fills on, so dashboard
        marks and PnL match real fills (CoinGecko can't price XAUT/WTI/
        stocks and drifts from the venue anyway). Falls back to the
        testnet API on testnet bots. Returns {} on failure."""
        if not symbols:
            return {}
        base_api = AFTERMATH_TESTNET_API if (network or "mainnet") == "testnet" else AFTERMATH_API
        prices: dict[str, float] = {}
        try:
            import requests as _req
            r = _req.get(f"{base_api}/ccxt/markets", timeout=10)
            if r.status_code != 200:
                return prices
            markets = r.json() if isinstance(r.json(), list) else []
            wanted = {s.upper() for s in symbols}
            ch_by_sym: dict[str, str] = {}
            for m in markets:
                b = str(m.get("base") or "").upper()
                if b in wanted and m.get("swap") and b not in ch_by_sym:
                    ch_by_sym[b] = m.get("id")
            for sym, ch_id in ch_by_sym.items():
                try:
                    rb = _req.post(f"{base_api}/ccxt/orderbook",
                                   json={"chId": ch_id}, timeout=10)
                    if rb.status_code != 200:
                        continue
                    data = rb.json()
                    bids = data.get("bids") or []
                    asks = data.get("asks") or []
                    best_bid = max((float(x[0]) for x in bids if x and len(x) > 1), default=None)
                    best_ask = min((float(x[0]) for x in asks if x and len(x) > 1), default=None)
                    if best_bid is not None and best_ask is not None:
                        prices[sym] = (best_bid + best_ask) / 2.0
                    elif best_bid is not None:
                        prices[sym] = best_bid
                    elif best_ask is not None:
                        prices[sym] = best_ask
                except Exception:
                    continue
        except Exception:
            return {}
        return prices

    # ---------------- on-chain execution helpers ----------------

    def _exec_ready(self) -> bool:
        return bool(self.gateway and getattr(self.gateway, "ready", False))

    @staticmethod
    def _exec_path():
        import os as _os
        p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "execution")
        if p not in sys.path:
            sys.path.insert(0, p)
        return p

    def _exec_chain_state(self, bot_id: int) -> tuple[dict, dict]:
        """(wallets, chain_state) for the bot from the execution ledger."""
        if not self._exec_ready():
            return {}, {}
        self._exec_path()
        try:
            self.gateway.provision_all_wallets(bot_id)
        except Exception:
            pass
        wallets, chain_state = {}, {}
        for chain in self.gateway.adapters:
            try:
                wallet = self.gateway.ledger.wallet_by_bot_chain(bot_id, chain)
                if not wallet:
                    continue
                wallets[chain] = wallet
                try:
                    self.gateway.sync(bot_id, chain)
                except Exception:
                    pass
                state = self.gateway.ledger.load_chain_state(wallet["id"]) or {}
                chain_state[chain] = state
            except Exception:
                continue
        return wallets, chain_state

    def _real_close_position(self, bot_id: int, chain: str, symbol: str) -> dict:
        """Close a position on the real execution gateway (Aftermath), not paper.

        Reads the live position, builds a market reduce-only OrderIntent for the
        opposite side, and routes it through the gateway (which enforces risk +
        idempotency + fee sweep). Returns {ok, error}."""
        try:
            self._exec_path()
            self.gateway.provision_wallet(bot_id, chain)
            self.gateway.sync(bot_id, chain)
            wallet = self.gateway.ledger.wallet_by_bot_chain(bot_id, chain)
            if not wallet:
                return {"ok": False, "error": "no wallet for this bot"}
            state = self.gateway.ledger.load_chain_state(wallet["id"]) or {}
            positions = state.get("positions") or []
            pos = next((p for p in positions
                        if (p.get("symbol") or p.get("coin") or "").upper() == symbol.upper()), None)
            if not pos:
                return {"ok": False, "error": f"no open {symbol} position on-chain"}
            qty = abs(float(pos.get("qty") or pos.get("szi") or pos.get("quantity") or 0))
            side = str(pos.get("side") or ("long" if qty > 0 else "short"))
            if qty <= 0:
                return {"ok": False, "error": f"zero {symbol} quantity"}
            # reduce-only market order on the opposite side
            from order_model import OrderIntent
            intent = OrderIntent(
                chain=chain, venue="aftermath-perp", symbol=symbol,
                side="sell" if side == "long" else "buy",
                qty=qty, order_type="market", leverage=1.0,
                idempotency_key=f"manual-close:{bot_id}:{symbol}:{int(time.time() * 1000)}",
            )
            res = self.gateway.route_and_sync(bot_id, intent, float(pos.get("entryPrice") or pos.get("entry") or 0) or 1.0)
            return {"ok": bool(res.get("ok")), "error": res.get("error") or "", "qty": qty}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    def _exec_ledger(self):
        """Open the canonical execution ledger (same path the gateway uses),
        so user wallets persist and are readable even before chain keys are
        configured. Returns an ExecLedger instance."""
        self._exec_path()
        from ledger import ExecLedger
        path = os.environ.get("EXEC_LEDGER_PATH", "exec_ledger.db")
        return ExecLedger(path)

    def _user_wallet(self, bot_id: int, chain: str) -> dict | None:
        """The user's own generated wallet (from onboarding) for a chain,
        regardless of gateway readiness. Falls back to the registry-stored
        address so the screen always shows the user's wallet."""
        try:
            ledger = self._exec_ledger()
            wallet = ledger.wallet_by_bot_chain(bot_id, chain)
            if hasattr(ledger, "close"):
                try:
                    ledger.close()
                except Exception:
                    pass
            if wallet:
                # Keep the registry's wallet_addr in sync so the fallback path
                # always works (this was the 2026-08-29 "wallet doesn't persist"
                # bug: the ledger had the wallet but the registry field was empty,
                # and any ledger-read failure then showed 'no wallet').
                try:
                    b = self.registry.get_bot(bot_id)
                    if b and not (b.get("wallet_addr") or ""):
                        self.registry.update_bot(bot_id, wallet_addr=wallet.get("address") or "")
                except Exception:
                    pass
                return wallet
        except Exception:
            pass
        try:
            b = self.registry.get_bot(bot_id)
            addr = (b or {}).get("wallet_addr") or ""
            if addr:
                return {"id": None, "bot_id": bot_id, "chain": chain,
                        "address": addr, "key_enc": None, "key_hash": None,
                        "pubkey": addr, "status": "created"}
        except Exception:
            pass
        return None

    def _generate_user_wallet(self, bot_id: int, chain: str) -> dict | None:
        """Generate + store a fresh per-chain wallet for the user. Returns the
        wallet row (with decrypted key in 'private_key') or None on failure.
        Reuses an existing wallet for (bot_id, chain) if one is already stored
        in the exec ledger - never silently overwrites a funded address."""
        try:
            self._exec_path()
            from exec_vault import ExecVault, generate_key_material
            from ledger import ExecLedger
            ledger = ExecLedger(os.environ.get("EXEC_LEDGER_PATH", "exec_ledger.db"))
            existing = ledger.wallet_by_bot_chain(bot_id, chain)
            if existing and existing.get("key_enc"):
                vault = ExecVault()
                try:
                    key = vault.decrypt(existing["key_enc"])
                    addr = existing["address"]
                except Exception:
                    key, addr = None, existing.get("address")
                if key or addr:
                    try:
                        ledger.close()
                    except Exception:
                        pass
                    if key:
                        return {"address": addr, "private_key": key}
                    return {"address": addr, "private_key": "", "existing": True}
            vault = ExecVault()
            addr, key_hex = generate_key_material(chain)
            enc = vault.encrypt(key_hex)
            ledger.upsert_wallet(bot_id, chain, addr, addr, enc, ExecVault.key_hash(key_hex))
            try:
                ledger.close()
            except Exception:
                pass
            # Persist the address on the registry bots row so Receive / wallet
            # screens can always show the user's wallet - even if the exec
            # ledger is ever reset or the path differs between processes.
            try:
                self.registry.update_bot(bot_id, wallet_addr=addr)
            except Exception:
                pass
            return {"address": addr, "private_key": key_hex}
        except Exception as exc:
            import logging
            logging.getLogger("tg_bot").warning("wallet gen failed: %s", exc)
            return None

    # Native Circle USDC on Sui mainnet (canonical coin type, 2026).
    # The old wUSDC (0x5d4b3025...::coin::COIN) is deprecated.
    SUI_USDC_MAINNET = "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
    # Testnet USDC = the Aftermath testnet settleId (what the perp account
    # settles in; the generic faucet USDC 0xa1ec7fc0... is a DIFFERENT token
    # and cannot be deposited or traded).
    SUI_USDC_TESTNET = "0xcdd397f2cffb7f5d439f56fc01afe5585c5f06e3bcd2ee3a21753c566de313d9::usdc::USDC"

    def _sui_transfer(self, bot_id: int, chain: str, dest: str, amount: float) -> dict:
        """Execute a real on-chain USDC transfer from the bot's wallet.

        Builds a SUIAdapter from the stored wallet key, constructs a
        SplitCoins + TransferObjects PTB, dry-runs, signs, and broadcasts.
        Returns {ok, digest, tx_hash, error}.
        """
        self._exec_path()
        try:
            from ledger import ExecLedger
            from exec_vault import ExecVault
            from sui_adapter import SUIAdapter

            b = self.registry.get_bot(bot_id) or {}
            network = (b.get("network") or "mainnet").strip().lower()
            testnet = network != "mainnet"

            ledger = ExecLedger(os.environ.get("EXEC_LEDGER_PATH", "exec_ledger.db"))
            wallet = ledger.wallet_by_bot_chain(bot_id, chain)
            if not wallet or not wallet.get("key_enc"):
                try:
                    ledger.close()
                except Exception:
                    pass
                return {"ok": False, "error": "no wallet key stored — generate one first"}
            vault = ExecVault()
            key_hex = vault.decrypt(wallet["key_enc"])
            if network == "mainnet":
                usdc_coin = self.SUI_USDC_MAINNET
                rpc_url = "https://fullnode.mainnet.sui.io:443"
            else:
                usdc_coin = self.SUI_USDC_TESTNET
                rpc_url = "https://fullnode.testnet.sui.io:443"
            adapter = SUIAdapter(ledger, key_hex, testnet=testnet,
                                 rpc_url=rpc_url,
                                 usdc_coin_type=usdc_coin)
            try:
                result = adapter.transfer_asset(dest, amount, "USDC")
                return result
            finally:
                try:
                    ledger.close()
                except Exception:
                    pass
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    def _rpc_balances(self, bot_id: int, chain: str, address: str) -> dict:
        """Read on-chain balances via public RPC using ONLY the wallet address
        (no private key needed). Keeps Receiver / dashboard accurate even when
        the execution gateway has no operator keys configured yet.

        Sui: GraphQL balance for native SUI + USDC on the bot's network.
        Solana: getBalance (SOL) + token account (USDC). Hyperliquid: n/a via
        public RPC without keys -> returns {}.
        """
        if not address:
            return {}
        try:
            b = self.registry.get_bot(bot_id) or {}
            network = (b.get("network") or "mainnet").strip().lower()
            import requests
            if chain == "sui":
                # network -> GraphQL endpoint + USDC coin type (mainnet uses
                # canonical Circle USDC; testnet uses the Aftermath settleId).
                if network == "mainnet":
                    gql = "https://graphql.mainnet.sui.io/graphql"
                    usdc_type = self.SUI_USDC_MAINNET
                else:
                    gql = "https://graphql.testnet.sui.io/graphql"
                    usdc_type = self.SUI_USDC_TESTNET
                out = {"USDC": 0.0, "native": 0.0}
                for key, coin in (("native", "0x2::sui::SUI"), ("USDC", usdc_type)):
                    try:
                        q = ('{ address(address: "' + address + '") { balance(coinType: "'
                             + coin + '") { totalBalance } } }')
                        r = requests.post(gql, json={"query": q}, timeout=8)
                        dec = 9 if key == "native" else 6
                        out[key] = float(int((r.json().get("data") or {})
                                             .get("address", {}).get("balance", {}).get("totalBalance", 0) or 0)) / (10 ** dec)
                    except Exception:
                        pass
                return out
            if chain == "solana":
                rpc = ("https://api.testnet.solana.com" if network != "mainnet"
                       else "https://api.mainnet-beta.solana.com")
                out = {"USDC": 0.0, "native": 0.0}
                try:
                    r = requests.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                        "params": [address]}, timeout=8)
                    out["native"] = float(r.json().get("result", {}).get("value", 0) or 0) / 1e9
                except Exception:
                    pass
                return out
        except Exception:
            return {}
        return {}

    def _exec_account(self, bot_id: int, chain: str) -> dict:
        """Real on-chain account state for a bot+chain (RPC, not mock).
        Falls back to the user's generated wallet (address) even when the
        gateway has no chain keys, so Receive/dashboard always shows the
        wallet with live balances."""
        wallet = self._user_wallet(bot_id, chain)
        if not self._exec_ready():
            if wallet:
                return {"balances": self._rpc_balances(
                            bot_id, chain, wallet.get("address") or ""),
                        "positions": [],
                        "wallet_address": wallet.get("address") or ""}
            return {"balances": {}, "positions": []}
        self._exec_path()
        try:
            self.gateway.provision_wallet(bot_id, chain)
        except Exception:
            pass
        if wallet is None:
            try:
                wallet = self.gateway.ledger.wallet_by_bot_chain(bot_id, chain)
            except Exception:
                pass
        if not wallet:
            return {"balances": {}, "positions": []}
        try:
            self.gateway.sync(bot_id, chain)
        except Exception:
            pass
        try:
            state = self.gateway.ledger.load_chain_state(wallet["id"]) or {}
        except Exception:
            state = {}
        return {
            "balances": state.get("balances") or {},
            "positions": state.get("positions") or [],
            "wallet_address": wallet.get("address") or "",
        }

    def _equity_snapshot(self, bot: dict | None = None) -> dict:
        """Real Sui/Aftermath equity snapshot (wallet USDC + collateral +
        unrealized PnL). Used by the dashboard to show honest Equity."""
        try:
            from sui_equity import sui_equity
            bot = bot or {}
            return sui_equity(bot)
        except Exception:
            return {}

    def _render_wallet(self, bot_id: int, bot_name: str, paused: int) -> str:
        self._exec_path()
        from wallet_ui import render_wallet_panel

        if not self._exec_ready():
            return USERBOT["wallet_disabled"].format(name=bot_name)
        wallets, chain_state = self._exec_chain_state(bot_id)
        if not wallets:
            return USERBOT["wallet_not_connected"].format(name=bot_name)
        return render_wallet_panel(
            [{"id": bot_id, "bot_name": bot_name, "paused": paused}],
            wallets, chain_state)

    def _exec_risk_lines(self) -> list[str]:
        self._exec_path()
        from risk_guard import BotRiskProfile

        profile = BotRiskProfile()
        lines = [
            f"• Max notional per order  <code>${profile.max_notional_usd:,.0f}</code>",
            f"• Max exposure           <code>{profile.max_exposure_pct:.0f}%</code> of balance",
            f"• Max leverage           <code>{profile.max_leverage:.0f}x</code>",
            f"• Stop-loss              <code>{'required' if profile.require_stop else 'optional'}</code> "
            f"({profile.min_stop_pct:.0f}–{profile.max_stop_pct:.0f}%)",
            f"• Daily loss halt        <code>-{profile.daily_loss_halt_pct:.0f}%</code>",
            f"• Max open positions     <code>{profile.max_open_positions}</code>",
        ]
        return lines

    # ---------------- lifecycle ----------------

    def _install_message_ttl(self, bot_token: str) -> type:
        """Build the TTLRequest class: auto-delete for every bot-sent message
        + user input after 3 minutes. Only the MAIN DASHBOARD survives
        (keyboard carries sb:dash + sb:kill).

        Implementation: PTB's ExtBot AND HTTPXRequest are FROZEN (setattr
        blocked - the 2026-09-06 crash loop), so the TTL lives in a
        TTLRequest SUBCLASS injected at build time via
        Application.builder().request(...). do_request inspects successful
        sendMessage / editMessageText / sendPhoto responses (message_id +
        reply_markup come straight from the response JSON) and schedules
        deletion with a raw-request daemon thread (cannot recurse)."""
        ttl = int(os.getenv("TG_MSG_TTL_SECONDS", "180"))

        def _keep(payload: dict) -> bool:
            """The MAIN DASHBOARD is the only survivor: the only panel whose
            keyboard carries both the Home and the Kill-Switch buttons."""
            try:
                rm = payload.get("reply_markup") or {}
                rows = rm.get("inline_keyboard") or []
                cbs = [b.get("callback_data", "") for row in rows for b in row]
                return "sb:dash" in cbs and "sb:kill" in cbs
            except Exception:
                return False

        def _schedule_delete(chat_id: int, message_id: int):
            def _del():
                time.sleep(ttl)
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                        json={"chat_id": chat_id, "message_id": message_id},
                        timeout=10)
                except Exception:
                    pass
            threading.Thread(target=_del, daemon=True).start()

        def _inspect_response(method: str, payload: dict, result) -> None:
            if method not in ("sendMessage", "editMessageText", "sendPhoto"):
                return
            try:
                res = (result or {}).get("result") or {}
                chat_id = (res.get("chat") or {}).get("id")
                message_id = res.get("message_id")
                if not chat_id or not message_id:
                    return
                if _keep(payload):
                    return
                _schedule_delete(chat_id, message_id)
            except Exception:
                pass

        def _payload_from(args, kwargs) -> dict:
            p = kwargs.get("parameters") or (args[1] if len(args) > 1 else None)
            if isinstance(p, str):
                import json as _json
                try:
                    return _json.loads(p)
                except Exception:
                    return {}
            return p if isinstance(p, dict) else {}

        class TTLRequest(HTTPXRequest):
            async def do_request(self, *a, **k):
                res = await super().do_request(*a, **k)
                try:
                    url = str(k.get("url") or (a[0] if a else ""))
                    method = url.rstrip("/").split("/")[-1]
                    if isinstance(res, dict) and res.get("ok"):
                        _inspect_response(method, _payload_from(a, k), res)
                except Exception:
                    pass
                return res

        return TTLRequest

    def _register_user_input_ttl(self, app, bot_token: str) -> None:
        """User-input half of the TTL: the user's own messages (keys, amounts,
        addresses, commands) vanish after the same 3 minutes. Button taps are
        excluded: the tapped message belongs to the bot (dashboard/panels)."""
        ttl = int(os.getenv("TG_MSG_TTL_SECONDS", "180"))

        def _schedule_delete(chat_id: int, message_id: int):
            def _del():
                time.sleep(ttl)
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                        json={"chat_id": chat_id, "message_id": message_id},
                        timeout=10)
                except Exception:
                    pass
            threading.Thread(target=_del, daemon=True).start()

        async def _user_input_ttl(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if update.callback_query:
                return
            msg = update.effective_message
            if msg and msg.from_user and not msg.from_user.is_bot:
                _schedule_delete(msg.chat_id, msg.message_id)

        app.add_handler(TypeHandler(Update, _user_input_ttl), group=-1)

    def start_bot(self, bot_id: int) -> bool:
        import logging

        log = logging.getLogger("tg_bot")
        with self._lock:
            if bot_id in self._apps:
                return True
            bot = self.registry.get_bot(bot_id)
            if not bot:
                return False
            token = self.registry.bot_token(bot_id)
            if not token:
                return False
            # SESSION UNIQUENESS: one poller per Telegram token. Two bot rows
            # sharing a token = Telegram 409 conflicts, dropped updates, and
            # cross-user session bleed (bots 2/4 both held @Nkofbot).
            other = self.registry.bot_token_owner(token, exclude_bot_id=bot_id)
            if other is not None:
                log.warning("bot %s shares its Telegram token with bot %s - refusing to start a second poller",
                            bot_id, other)
                self.registry.update_bot(
                    bot_id, is_running=0,
                    last_error=f"token already polled by bot {other} - relink this bot with its own token")
                return False
            # PRE-FLIGHT: validate the token before spawning the poll thread.
            # A dead token (regenerated in @BotFather) polls 'Unauthorized'
            # forever - the user can NEVER reach the bot to change anything
            # (their API key included). Fail fast, mark the bot, and tell the
            # user through the MASTER bot (which is always alive) how to fix it.
            # ONLY a confirmed 401 marks the token dead: a network hiccup here
            # must not disable a healthy bot.
            try:
                r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
                body = r.json() if r.status_code == 200 else {}
                if r.status_code in (401, 403) or (
                        isinstance(body, dict) and body.get("error_code") in (401, 403)):
                    self.registry.update_bot(
                        bot_id, is_running=0,
                        last_error="bot token invalid - relink required (recreate the token "
                                   "in @BotFather, then send /relink to @Nekochanadminbot)")
                    log.warning("bot %s (@%s) token rejected by Telegram - not starting, user notified",
                                bot_id, bot.get("bot_username"))
                    self._notify_dead_token(bot, bot_id)
                    return False
                # any other status (429/5xx/timeout): let the poll retry loop handle it
            except Exception:
                pass  # network error - don't mislabel a healthy token as dead
            # 3-MINUTE MESSAGE TTL: every message the bot SENDS (notifications,
            # confirmations, prompts) and every USER INPUT (pasted keys,
            # amounts) self-destructs after TG_MSG_TTL_SECONDS. Only the MAIN
            # DASHBOARD persists. TTLRequest is injected at build time -
            # PTB's ExtBot and HTTPXRequest are frozen and cannot be
            # monkey-patched (that crash-looped the service on 2026-09-06).
            TTLRequest = self._install_message_ttl(token)
            app = (Application.builder()
                   .token(token)
                   .request(TTLRequest())
                   .build())
            self._register_user_input_ttl(app, token)
            self._register_handlers(app, bot)
            self._apps[bot_id] = app
            self.registry.update_bot(bot_id, is_running=1, last_heartbeat=utcnow())

        def _poll():
            import asyncio
            import logging

            log = logging.getLogger("tg_bot")
            attempts = 0
            while attempts < 10:
                # run_polling creates AND CLOSES its own event loop; calling it
                # again in the same thread crashes with 'Event loop is closed'
                # instead of re-testing the token. Fresh loop per attempt.
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    app.run_polling(drop_pending_updates=True, stop_signals=())
                    loop.close()
                    return  # clean stop
                except Exception as exc:  # noqa: BLE001
                    attempts += 1
                    msg = str(exc)
                    log.error("user bot %s polling failed (%s/10): %s", bot_id, attempts, msg)
                    from telegram.error import InvalidToken as _InvalidToken
                    if isinstance(exc, _InvalidToken) or "Unauthorized" in msg:
                        # dead token will never recover by retrying - stop now
                        # and route the user to the master bot for a relink.
                        self.registry.update_bot(
                            bot_id, is_running=0,
                            last_error="bot token invalid - relink required (recreate the "
                                       "token in @BotFather, then send /relink to @Nekochanadminbot)")
                        self._notify_dead_token(bot, bot_id)
                        with self._lock:
                            self._apps.pop(bot_id, None)
                        return
                    if attempts >= 10:
                        break
                    import time as _t

                    _t.sleep(10 * attempts)  # backoff: 10s, 20s, ...
                finally:
                    if not loop.is_closed():
                        loop.close()
                    asyncio.set_event_loop(None)
            self.registry.update_bot(bot_id, is_running=0, last_error="polling failed after 10 attempts")
            with self._lock:
                self._apps.pop(bot_id, None)

        threading.Thread(target=_poll, name=f"userbot-{bot_id}", daemon=True).start()
        return True

    def _notify_dead_token(self, bot: dict, bot_id: int) -> None:
        """Tell the user (via the always-alive master bot) that their bot's
        token died and exactly how to relink it. Deduped: only once per hour
        per bot so polling restarts don't spam."""
        import time as _t

        now = _t.time()
        last = self._dead_token_notified.get(bot_id, 0.0)
        if now - last < 3600:
            return
        self._dead_token_notified[bot_id] = now
        try:
            import os as _os

            master_token = _os.environ.get("TG_BOT_TOKEN") or _master_token_fallback()
            if not master_token:
                return
            username = bot.get("bot_username") or f"bot {bot_id}"
            text = (
                f"⚠️ <b>@{username} is unreachable</b>\n\n"
                f"Its Telegram token was rejected (it was probably regenerated or "
                f"deleted in @BotFather). Until it is relinked the bot cannot receive "
                f"ANY commands - including AI-key changes.\n\n"
                f"<b>How to fix (2 min):</b>\n"
                f"1. Open @BotFather → /mybots → select @{username} → API Token → "
                f"Revoke (or copy the current token)\n"
                f"2. Send <code>/relink</code> here and paste the new token\n\n"
                f"Your wallet, positions and AI key stay untouched."
            )
            requests.post(
                f"https://api.telegram.org/bot{master_token}/sendMessage",
                json={"chat_id": bot.get("tg_id"), "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("dead-token notification failed for bot %s: %s", bot_id, exc)

    def stop_bot(self, bot_id: int):
        with self._lock:
            app = self._apps.pop(bot_id, None)
        if app:
            app.stop()
            self.registry.update_bot(bot_id, is_running=0)

    def start_all(self):
        for bot in self.registry.all_bots():
            if not bot.get("paused"):
                self.start_bot(bot["id"])

    # ---------------- handlers ----------------

    def _register_handlers(self, app: Application, bot: dict):
        bot_id = bot["id"]
        platform_token = bot["platform_token"]
        tg_id = bot["tg_id"]


        async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """First-ever /start: welcome + key onboarding gate + setup wizard."""
            key = self.registry.get_active_key(tg_id)
            b = self.registry.get_bot(bot_id)
            if key:
                if not (b or {}).get("onboarding_complete"):
                    # Key set but setup unfinished -> resume the onboarding wizard.
                    await onboarding_intro(update, context)
                    return
                await dash(update, context)
                return
            text = (
                f"🐾 Welcome to {bot['bot_name']} - your AI trading cat.\n\n"
                "I watch live markets (BTC, ETH, US stocks, Forex) and trade on the "
                "platform with real prices. Every trade gets pushed here.\n\n"
                "To start trading I need one thing: your AI API key - it "
                "powers my decisions and you pay for your own model calls.\n\n"
                "⚠️ Trading involves real risk. (I'm a cat, not an advisor.)"
            )
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("🔑 Set AI Key", callback_data="key:start")],
                [telegram.InlineKeyboardButton("❓ How to get a key", callback_data="key:help")],
            ])
            if update.message:
                await update.message.reply_text(text, reply_markup=kb)
            else:
                await update.callback_query.message.edit_text(text, reply_markup=kb)

        async def key_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            await q.message.edit_text(
                "🔑 Where to get an AI API key:\n\n"
                "• OpenAI: platform.openai.com → API keys → sk-…\n"
                "• OpenRouter: openrouter.ai → keys → sk-or-…\n"
                "• Custom: any OpenAI-compatible endpoint (URL + key)\n\n"
                "The key only pays for YOUR bot's decisions. Tap Set AI Key when ready.",
                reply_markup=telegram.InlineKeyboardMarkup([
                    [telegram.InlineKeyboardButton("🔑 Set AI Key", callback_data="key:start")],
                ]),
            )

        async def key_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            # Gate: disclaimer must be accepted before key setup.
            user = self.registry.get_user(tg_id)
            if user and user.get("accepted_disclaimer"):
                await _show_key_provider(q)
                return K_PROVIDER
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("✅ I understand, continue", callback_data="key:disclaimer_accept")],
                [telegram.InlineKeyboardButton("✖️ I don't accept", callback_data="key:decline")],
                [telegram.InlineKeyboardButton(CANCEL, callback_data="key:cancel")],
            ])
            await q.message.edit_text(WIZARD["disclaimer"], reply_markup=kb)
            return K_PROVIDER  # re-use the provider state; we handle the accept below

        async def key_disclaimer_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            self.registry.accept_disclaimer(tg_id)
            await _show_key_provider(q)
            return K_PROVIDER

        async def _show_key_provider(q):
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("OpenAI", callback_data="keyp:openai"),
                 telegram.InlineKeyboardButton("OpenRouter", callback_data="keyp:openrouter")],
                [telegram.InlineKeyboardButton("DeepSeek", callback_data="keyp:deepseek"),
                 telegram.InlineKeyboardButton("Claude", callback_data="keyp:claude")],
                [telegram.InlineKeyboardButton("opencode-go", callback_data="keyp:opencode-go"),
                 telegram.InlineKeyboardButton("Custom URL", callback_data="keyp:custom")],
                [telegram.InlineKeyboardButton(CANCEL, callback_data="key:cancel")],
            ])
            await q.message.edit_text(
                "🔑 Which AI provider powers your bot?\n\n"
                "Your key pays for your own model calls. We test it before saving.",
                reply_markup=kb,
            )

        async def key_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            provider = q.data.split(":", 1)[1]
            context.bot_data["key_provider"] = provider
            if provider == "custom":
                await q.message.edit_text(
                    "Send your provider URL (OpenAI-compatible), e.g.\n"
                    "https://your-provider.com/v1\n\nThen the model, then the key.",
                    reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(CANCEL, callback_data="key:cancel")]]),
                )
            else:
                labels = {"openai": "sk-…", "openrouter": "sk-or-…",
                          "deepseek": "sk-…", "claude": "sk-ant-…",
                          "opencode-go": "gateway key"}
                await q.message.edit_text(
                    f"Paste your key ({labels.get(provider, 'API key')}):\n\n"
                    "It will be stored encrypted and shown masked (sk-•••4821).",
                    reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(CANCEL, callback_data="key:cancel")]]),
                )
            return K_KEY

        async def key_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
            provider = context.bot_data.get("key_provider", "openai")
            raw = (update.message.text or "").strip()
            if provider == "custom" and "key_provider_url" not in context.bot_data:
                context.bot_data["key_provider_url"] = raw
                await update.message.reply_text("Got it. Now the model (e.g. gpt-4o-mini):")
                return K_KEY
            if provider == "custom" and "key_provider_model" not in context.bot_data:
                context.bot_data["key_provider_model"] = raw
                await update.message.reply_text("Now paste your API key:")
                return K_KEY
            api_key = raw
            if len(api_key) < 8:
                await update.message.reply_text("❌ That doesn't look like a key. Paste the full key.")
                return K_KEY
            base = context.bot_data.get("key_provider_url")
            model = context.bot_data.get("key_provider_model")
            await update.message.reply_text("⏳ Testing your key…")
            try:
                model = validate_key(provider, api_key, base, model)
            except ProviderError as exc:
                msg = {"invalid": "❌ Provider rejected this key. Double-check it.",
                       "rate_limited": "⏳ Provider needs credits or is rate-limited - wait and retry.",
                       "network": f"⏳ Can't reach provider ({exc})."}.get(exc.kind, "❌ Key rejected.")
                await update.message.reply_text(msg)
                return K_KEY
            try:
                self.registry.store_key(tg_id, provider, api_key, base, model)
            except ValueError as exc:
                await update.message.reply_text(f"❌ {exc}")
                return K_KEY
            self.registry.cancel_bot_deletion(bot_id)  # key set -> keep the bot
            context.bot_data.pop("key_provider", None)
            context.bot_data.pop("key_provider_url", None)
            context.bot_data.pop("key_provider_model", None)
            if self.agent_pool:
                try:
                    self.agent_pool.start(bot_id)
                except Exception:
                    pass
            # Foreign/custom provider rule notification: tell the user exactly
            # how their key is used and the operating rule for non-preset keys.
            rule_notice = ""
            if provider in ("custom", "deepseek", "claude"):
                rule_notice = (
                    f"\n\n🧠 <b>HOW YOUR KEY IS USED</b>\n"
                    f"Provider: <b>{provider}</b> · Model: <code>{model}</code>\n"
                    f"• Your key is sent ONLY to {base or 'your provider'} - never "
                    f"to us or any other service\n"
                    f"• It powers your bot's trading decisions, stored encrypted "
                    f"and masked\n"
                    f"• You can revoke it anytime in Settings → Change AI Key\n"
                    f"• If your provider uses a non-standard API shape, the bot "
                    f"falls back to OpenAI-compatible calls"
                )
            await update.message.reply_text(
                f"✅ Key works ({provider}) · model <code>{model}</code>\n\n"
                "Let's set up how Neko-Chan trades for you." + rule_notice,
                parse_mode="HTML",
            )
            await onboarding_intro(update, context)
            return ConversationHandler.END

        async def key_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            declined = q.data == "key:decline"
            # Unconfigured bot (no AI key yet) -> schedule cleanup so we don't
            # hold idle bots. The user gets a clear deadline + how to keep it.
            try:
                active_key = self.registry.get_active_key(tg_id)
            except Exception:
                active_key = None
            deadline = ""
            if not active_key:
                from datetime import timedelta
                from store import utcnow
                try:
                    self.registry.schedule_bot_deletion(
                        bot_id, (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat())
                    deadline = (
                        "\n\n🕒 Your bot is unconfigured - it will be removed from the "
                        "network in 3 hours unless you add your AI key.\n"
                        "To keep it, just tap \"Set AI Key\" and complete setup."
                    )
                except Exception:
                    deadline = ""
            if declined:
                text = ("✅ Understood - nothing was saved.\n\n"
                        "Your bot stays OFF. No AI key was stored, no agent was "
                        "started, and nothing was charged." + deadline)
            else:
                text = ("Key setup canceled. Your bot stays paused until you add one." + deadline)
            await q.message.edit_text(
                text,
                reply_markup=telegram.InlineKeyboardMarkup(
                    [[telegram.InlineKeyboardButton("🔑 Set AI Key", callback_data="key:start")],
                     [telegram.InlineKeyboardButton("🏠 Home", callback_data="sb:dash")]]))
            return ConversationHandler.END

        key_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(key_start, pattern=r"^key:start$")],
            states={
                K_PROVIDER: [
                    CallbackQueryHandler(key_provider, pattern=r"^keyp:"),
                    CallbackQueryHandler(key_disclaimer_accept, pattern=r"^key:disclaimer_accept$"),
                ],
                K_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, key_input)],
            },
            fallbacks=[CallbackQueryHandler(key_cancel, pattern=r"^key:(cancel|decline)$")],
            name="userbot_key_setup",
            allow_reentry=True,
        )

        # ------------------------------------------------------------------
        # Onboarding: how Neko trades -> trader type -> chain -> wallet backup
        # ------------------------------------------------------------------
        async def onboarding_intro(update: Update, context: ContextTypes.DEFAULT_TYPE):
            b = self.registry.get_bot(bot_id)
            interval = b.get("interval_sec", 120) if b else 120
            text = ONBOARD["intro"].format(interval=interval)
            text += ("\n\n📣 <b>First thing:</b> follow our channel "
                     "https://t.me/Nekobotnews - it helps you use Neko-Chan "
                     "properly (updates, tips, announcements).")
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("📣 Follow @Nekobotnews", url="https://t.me/Nekobotnews")],
                [telegram.InlineKeyboardButton("Continue →", callback_data="ob:trader")],
            ])
            msg = update.message or update.callback_query.message
            if update.callback_query:
                await update.callback_query.answer()
                await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
            else:
                await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

        async def onboarding_trader(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            texts = {
                "scalp": ONBOARD["trader_scalp"],
                "intraday": ONBOARD["trader_intraday"],
                "swing": ONBOARD["trader_swing"],
                "auto": ONBOARD["trader_auto"],
            }
            # Scalp principles: tight stops, quick 1-2% targets, but leverage is
            # kept moderate (3x) so a sudden wick doesn't wipe the position.
            leverage_for = {"scalp": 3.0, "intraday": 2.0, "swing": 2.0, "auto": 2.0}
            ttype = q.data.split(":", 2)[2] if q.data.count(":") >= 2 else ""
            if ttype in texts:
                self.registry.update_bot(bot_id, trader_type=ttype,
                                         leverage=leverage_for.get(ttype, 2.0))
                kb = telegram.InlineKeyboardMarkup([
                    [telegram.InlineKeyboardButton("Continue →", callback_data="ob:chain")],
                    [telegram.InlineKeyboardButton(BACK, callback_data="ob:trader")],
                ])
                await q.message.edit_text(texts[ttype], parse_mode="HTML", reply_markup=kb)
                return
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("⚡ Scalp", callback_data="ob:trader:scalp")],
                [telegram.InlineKeyboardButton("⏱ Intraday", callback_data="ob:trader:intraday")],
                [telegram.InlineKeyboardButton("📈 Swing", callback_data="ob:trader:swing")],
                [telegram.InlineKeyboardButton("🤖 Auto", callback_data="ob:trader:auto")],
            ])
            await q.message.edit_text(ONBOARD["trader"], parse_mode="HTML", reply_markup=kb)

        async def onboarding_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            texts = {
                "sui": ONBOARD["chain_sui"],
                "solana": ONBOARD["chain_solana"],
                "hyperliquid": ONBOARD["chain_hyperliquid"],
            }
            chain = q.data.split(":", 2)[2] if q.data.count(":") >= 2 else ""
            if chain in texts:
                if chain == "sui":
                    kb = telegram.InlineKeyboardMarkup([
                        [telegram.InlineKeyboardButton("✅ Trade on this chain", callback_data="ob:chain_confirm:sui")],
                        [telegram.InlineKeyboardButton(BACK, callback_data="ob:chain")],
                    ])
                else:
                    # BLOCKED until released: non-Sui chains show the release
                    # status and cannot be confirmed to trade.
                    kb = telegram.InlineKeyboardMarkup([
                        [telegram.InlineKeyboardButton("⛓ Sui is live now", callback_data="ob:chain:sui")],
                        [telegram.InlineKeyboardButton(BACK, callback_data="ob:chain")],
                    ])
                await q.message.edit_text(texts[chain], parse_mode="HTML", reply_markup=kb)
                return
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("⛓ Sui (live)", callback_data="ob:chain:sui")],
                [telegram.InlineKeyboardButton("⛓ Solana", callback_data="ob:chain:solana")],
                [telegram.InlineKeyboardButton("⛓ Hyperliquid", callback_data="ob:chain:hyperliquid")],
                [telegram.InlineKeyboardButton(BACK, callback_data="ob:trader")],
            ])
            await q.message.edit_text(ONBOARD["chain"], parse_mode="HTML", reply_markup=kb)

        async def onboarding_chain_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            chain = q.data.split(":", 2)[2]
            self.registry.update_bot(bot_id, chain=chain)
            # DEFAULT WATCHLIST for new users: if the user hasn't picked any
            # assets yet, seed the watchlist with the chain's default perps so
            # Peek shows them and the agent analyzes them out of the box.
            b_now = self.registry.get_bot(bot_id)
            if not _parse_watchlist((b_now or {}).get("watchlist")):
                _def_watch = {
                    "sui": ["BTC", "ETH", "SOL", "SUI", "HYPE"],
                    "solana": ["BTC", "ETH", "SOL", "SUI", "DOGE"],
                    "hyperliquid": ["BTC", "ETH", "SOL", "SUI", "HYPE"],
                }.get(chain, ["BTC", "ETH"])
                self.registry.update_bot(bot_id,
                                         watchlist=",".join(sorted(_def_watch)))
            # Generate the real per-chain wallet (address + private key).
            # Persist EVEN without a gateway so the user's wallet survives
            # restarts and is always recoverable via the Registry.
            key_hex = None
            addr = None
            try:
                self._exec_path()
                from exec_vault import ExecVault, generate_key_material
                vault = ExecVault()
                from ledger import ExecLedger
                _path = os.environ.get("EXEC_LEDGER_PATH", "exec_ledger.db")
                _ledger = ExecLedger(_path)
                existing = _ledger.wallet_by_bot_chain(bot_id, chain)
                if existing and existing.get("key_enc"):
                    addr = existing["address"]
                    key_hex = vault.decrypt(existing["key_enc"]) if existing.get("key_enc") else key_hex
                else:
                    addr, key_hex = generate_key_material(chain)
                    enc = vault.encrypt(key_hex)
                    _ledger.upsert_wallet(bot_id, chain, addr, addr, enc,
                                          ExecVault.key_hash(key_hex))
                try:
                    _ledger.close()
                except Exception:
                    pass
                # Keep the registry bots row in sync so the user's address
                # survives even if the exec ledger path ever differs.
                self.registry.update_bot(bot_id, wallet_addr=addr)
            except Exception as exc:
                import logging
                logging.getLogger("tg_bot").warning("wallet gen failed: %s", exc)
            if not (addr and key_hex):
                await q.message.edit_text("⚠️ Couldn't generate a wallet for this chain yet. Try again.",
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            context.bot_data["pending_key"] = key_hex
            text = ONBOARD["wallet_created"].format(chain=_chain_label(chain), address=addr, private_key=key_hex)
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("🗝️ I've saved my key", callback_data="ob:key_saved")],
            ])
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            # SECURITY: private key shown once; auto-delete the message in 5 min
            _schedule_msg_delete(self._master_token or "", q.message.chat_id, q.message.message_id)

        async def onboarding_key_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            context.bot_data.pop("pending_key", None)
            self.registry.update_bot(bot_id, onboarding_complete=1)
            await q.message.edit_text(ONBOARD["wallet_saved"], parse_mode="HTML")
            await dash(update, context)

        async def dash(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if update.callback_query:
                await update.callback_query.answer()
            self.registry.update_bot(bot_id, last_heartbeat=utcnow())
            b = self.registry.get_bot(bot_id)
            chain = b.get("chain") or "sui"
            mode = (b.get("trading_mode") or "paper").lower()
            account = {"balances": {}, "positions": []}
            if mode == "paper":
                # PAPER MODE: build dashboard from the shared virtual portfolio.
                # Prices come from Aftermath (the venue the agent trades) so
                # marks/PnL match real fills.
                try:
                    _ps = self._paper_store()
                    _syms = [p["symbol"] for p in _ps.positions(bot_id)]
                    _paper_prices = await asyncio.get_running_loop().run_in_executor(
                        None, self._paper_mark_prices, _syms, (b.get('network') or 'mainnet'))
                    account = _ps.dashboard_account(bot_id, _paper_prices or None)
                except Exception:
                    account = {"balances": {}, "positions": []}
            else:
                try:
                    account = self._exec_account(bot_id, chain)
                except Exception:
                    account = {"balances": {}, "positions": []}
            # WALLET ADDRESS ALWAYS VISIBLE (paper or live): read straight
            # from the registry so the dashboard never hides the address.
            if not account.get("wallet_address"):
                try:
                    _addr = (b or {}).get("wallet_addr") or ""
                    if _addr:
                        account["wallet_address"] = _addr
                except Exception:
                    pass
            # Paper mode: no on-chain equity block — the balances dict IS the paper equity.
            mode = (b.get("trading_mode") or "paper").lower()
            _eq = None if mode == "paper" else self._equity_snapshot(b)
            text = render_production_dashboard(b, account, chain, equity=_eq)
            # Pause now means "pause trading (LLM)" — bot stays online, so use `paused` flag
            is_trading = not b.get("paused") and b.get("is_running")
            start_label = "⏸️ Pause Trading" if is_trading else "▶️ Start Trading"
            start_cb = "sb:pause" if is_trading else "sb:resume"
            mode_label = "🧪 Switch to PAPER" if mode == "live" else "🔴 Switch to LIVE"
            mode_cb = "sb:mode_paper" if mode == "live" else "sb:mode_live"
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton(start_label, callback_data=start_cb),
                 telegram.InlineKeyboardButton("👀 Peek", callback_data="sb:peek")],
                [telegram.InlineKeyboardButton(mode_label, callback_data=mode_cb)],
                [telegram.InlineKeyboardButton("📤 Send", callback_data="sb:send"),
                 telegram.InlineKeyboardButton("📥 Receive", callback_data="sb:receive")],
                [telegram.InlineKeyboardButton("📊 P&L", callback_data="sb:pnl"),
                 telegram.InlineKeyboardButton("💰 Active Positions", callback_data="sb:pos")],
                [telegram.InlineKeyboardButton("📬 Notifications", callback_data="sb:inbox"),
                 telegram.InlineKeyboardButton("🛑 Kill-Switch", callback_data="sb:kill")],
                [telegram.InlineKeyboardButton("⚙️ Settings", callback_data="sb:settings"),
                 telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:dash")],
                [telegram.InlineKeyboardButton("❓ Help", callback_data="sb:help")],
            ])
            if update.message:
                await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
            elif update.callback_query:
                await _safe_edit(update.callback_query, text, reply_markup=kb)

        async def start_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            if not self.registry.get_active_key(tg_id):
                await q.message.edit_text("🔑 Set an AI key first in Settings.",
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            self.registry.update_bot(bot_id, paused=0, is_running=1)
            if self.agent_pool:
                self.agent_pool.start(bot_id)
            await q.message.edit_text("▶️ Agent started. Neko-Chan is scanning markets.",
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton("👀 Peek", callback_data="sb:peek"),
                                            telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def peek(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            chain = b.get("chain") or "sui"
            mode = (b.get("trading_mode") or "paper").lower()
            watched = _parse_watchlist(b.get("watchlist"))
            default = {
                "sui": ["BTC", "ETH", "SOL", "SUI", "HYPE"],
                "solana": ["BTC", "ETH", "SOL", "SUI", "DOGE"],
                "hyperliquid": ["BTC", "ETH", "SOL", "SUI", "HYPE"],
            }.get(chain, ["BTC", "ETH"])
            active = (watched or default)[:5]
            active_upper = {s.upper() for s in active}

            lines = ["👀 <b>Agent Status</b>\n"]
            if watched:
                lines.append(f"🎯 <b>Your picks</b>: {', '.join(watched)}")
            lines.append(f"⛓ <b>Chain</b>: {_chain_label(chain)} · "
                         f"<b>Analyzing</b>: {', '.join(active)}\n")
            if not b.get("is_running"):
                lines.append("⏸️ Agent is not running. Tap ▶️ Start to begin.")
            else:
                # Latest decision per analyzed asset from the JSON cache (holds
                # ONLY the current decision per symbol - old trades are replaced,
                # never accumulated). Fall back to the append-only CSV if the
                # cache is missing. A hold is stored under the "__hold__" key.
                try:
                    import json as _json
                    import csv as _csv
                    from pathlib import Path
                    cache_path = Path(__file__).resolve().parents[2] / "research" / "exports" / f"live_agent_cache_bot{bot_id}.json"
                    latest: dict[str, dict] = {}
                    last_reason = ""
                    if cache_path.exists():
                        try:
                            cache = _json.loads(cache_path.read_text(encoding="utf-8"))
                            for key, row in (cache or {}).items():
                                sym = (row.get("symbol") or key or "").upper()
                                if sym in active_upper:
                                    latest[sym] = row
                                if row.get("reasoning"):
                                    last_reason = row.get("reasoning") or ""
                        except Exception:
                            pass
                    if not latest:
                        log_path = Path(__file__).resolve().parents[2] / "research" / "exports" / f"live_agent_log_bot{bot_id}.csv"
                        if log_path.exists():
                            with open(log_path, newline="", encoding="utf-8") as f:
                                for row in _csv.DictReader(f):
                                    sym = (row.get("symbol") or "").upper()
                                    if sym in active_upper:
                                        latest[sym] = row
                                    if row.get("reasoning"):
                                        last_reason = row.get("reasoning") or ""
                    # OPEN POSITIONS (paper or live): those symbols are held, the
                    # agent intentionally skips re-analyzing them (one position
                    # per symbol) — so their cache rows are STALE by design.
                    # Showing yesterday's decision as if fresh is misleading.
                    open_syms = set()
                    if mode == "paper":
                        try:
                            open_syms = {p["symbol"].upper()
                                         for p in self._paper_store().positions(bot_id)
                                         if p.get("qty")}
                        except Exception:
                            open_syms = set()
                    else:
                        try:
                            open_syms = {str(p.get("symbol") or p.get("coin") or "").upper()
                                         for p in (self._exec_account(bot_id, chain).get("positions") or [])
                                         if p.get("quantity") not in (0, None, "")}
                        except Exception:
                            open_syms = set()
                    for sym in active:
                        row = latest.get(sym.upper())
                        if sym.upper() in open_syms:
                            # held right now: show live position status, not a stale row
                            _pos_row = next((p for p in self._paper_store().positions(bot_id)
                                             if p["symbol"].upper() == sym.upper()), None) \
                                if mode == "paper" else None
                            lines.append(
                                f"📊 <b>{_esc(sym.upper())}</b> · <b>POSITION OPEN</b>"
                                + (f" · {_esc(str(_pos_row['direction']).upper())} "
                                   f"entry {_pos_row['entry_price']:,.4f}" if _pos_row else "")
                                + "\n  Agent is managing this trade (stop/target armed) "
                                  "— it resolves before {0} is re-analyzed.".format(_esc(sym.upper())))
                            continue
                        if row:
                            # Display DIRECTION (LONG/SHORT) not the order verb
                            # (buy/sell/short/cover) - that's what the user
                            # actually cares about. Hold stays HOLD.
                            direction = (row.get("direction") or "").upper()
                            action = (row.get("action") or "").upper()
                            if direction in ("LONG", "SHORT"):
                                label = direction
                            elif action in ("SELL", "COVER"):
                                label = "EXIT"
                            elif action in ("BUY", "SHORT"):
                                label = action
                            else:
                                label = action or "?"
                            qty = row.get("quantity") or row.get("qty") or ""
                            price = row.get("limit_price") or row.get("price") or ""
                            order_type = (row.get("order_type") or "").upper()
                            when = str(row.get("ts") or "")[11:19] or "?"
                            reasoning = (_esc(row.get("reasoning") or "")).strip()
                            entry = f"🚦 LIMIT @ <code>${price:,.4f}</code>" if (order_type == "LIMIT" and price) else f"Market @ <code>${price:,.4f}</code>" if price else ""
                            stop = row.get("stop_pct") or ""
                            take = row.get("take_pct") or ""
                            lev = row.get("leverage") or ""
                            lev_why = str(row.get("lev_why") or "").strip()
                            guard = f" · ⛔ {stop}% / 🎯 {take}%" if stop or take else ""
                            if lev:
                                guard += f" · ⚡ {float(lev):g}x"
                            lines.append(f"📊 <b>{_esc(sym.upper())}</b> · <b>{_esc(label)}</b> · <code>{qty}</code>\n")
                            if entry:
                                lines.append(f"  {entry}{guard}")
                            elif guard:
                                lines.append(f"  {guard.strip(' ·')}")
                            if lev_why:
                                lines.append(f"  ⚡ leverage: {_esc(lev_why)}")
                            if reasoning:
                                lines.append(f"  💡 {reasoning[:100]}")
                        else:
                            lines.append(f"📊 <b>{_esc(sym.upper())}</b> · analyzing…\n"
                                         f"  Agent is watching this asset for a setup.")
                    if not latest and last_reason:
                        lines.append(f"\n💤 <b>Holding</b>: {_esc(last_reason[:160])}")
                except Exception:
                    lines.append("Could not read agent log.")
                # Live price snapshot so tapping Refresh always returns fresh data.
                # AFTERMATH ORDERBOOK (not the platform API): the platform
                # endpoint rate-limits to 1 req/sec per token, so the old
                # back-to-back loop only ever priced the first symbol (BTC)
                # and silently dropped the rest with 429s. Aftermath has no
                # such limit and is the venue the agent trades on anyway.
                try:
                    live_px = await asyncio.get_running_loop().run_in_executor(
                        None, self._paper_mark_prices, active,
                        (b.get("network") or "mainnet"))
                    if live_px:
                        lines.append("\n💰 <b>Live</b>: " + " · ".join(
                            f"{s} ${live_px[s.upper()]:,.4f}" for s in active
                            if s.upper() in live_px))
                except Exception:
                    pass
                # Manual Take/Reject buttons for EVERY fresh pending decision.
                # A decision is stale when the agent has since emitted a hold
                # for it (cache row replaced) or its ts is older than ~30 min
                # (cache rows only update when that symbol is re-analyzed, so
                # an old buy row would otherwise linger forever - the "Take
                # BTC LONG is stuck" bug). Symbols with an open position are
                # skipped: one position per symbol, nothing to take.
                kb_rows = []
                try:
                    from datetime import datetime as _dt
                    _now = _dt.now(timezone.utc)
                    open_syms = set()
                    if mode == "paper":
                        try:
                            open_syms = {p["symbol"].upper()
                                         for p in self._paper_store().positions(bot_id)
                                         if p.get("qty")}
                        except Exception:
                            open_syms = set()
                    else:
                        try:
                            open_syms = {str(p.get("symbol") or p.get("coin") or "").upper()
                                         for p in (self._exec_account(bot_id, chain).get("positions") or [])
                                         if p.get("quantity") not in (0, None, "")}
                        except Exception:
                            open_syms = set()
                    for sym in active:
                        row = latest.get(sym.upper()) if 'latest' in locals() else None
                        if not row:
                            continue
                        act = (row.get("action") or "").lower()
                        direction = (row.get("direction") or "").lower()
                        # Only actionable opens (buy/short), not holds/exits
                        if act not in ("buy", "short") and direction not in ("long", "short"):
                            continue
                        if sym.upper() in open_syms:
                            continue
                        # FRESHNESS: ignore decisions older than 30 minutes
                        try:
                            _ts = _dt.fromisoformat(str(row.get("ts") or "").replace("Z", "+00:00"))
                            if (_now - _ts).total_seconds() > 1800:
                                continue
                        except Exception:
                            continue
                        label = "LONG" if (act == "buy" or direction == "long") else "SHORT"
                        kb_rows.append([
                            telegram.InlineKeyboardButton(f"✅ Take {sym.upper()} {label}", callback_data=f"trade:take:{sym.upper()}"),
                            telegram.InlineKeyboardButton(f"❌ Reject", callback_data=f"trade:reject:{sym.upper()}"),
                        ])
                except Exception:
                    pass
                kb_rows.append([telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:peek")])
                kb_rows.append([telegram.InlineKeyboardButton(BACK, callback_data="sb:dash")])
            try:
                await q.message.edit_text("\n".join(lines), parse_mode="HTML",
                                          reply_markup=telegram.InlineKeyboardMarkup(kb_rows))
            except telegram.error.BadRequest as _be:
                # identical content = nothing changed; answer quietly instead of crashing
                if "not modified" not in str(_be).lower():
                    raise

        async def _exec_account(self, bot_id: int, chain: str) -> dict:
            """Real on-chain account state for a bot+chain (RPC, not mock).
            Falls back to the user's generated wallet (address) even when the
            gateway has no chain keys, so Receive always shows the wallet."""
            # User's own wallet (from onboarding) - always readable.
            wallet = self._user_wallet(bot_id, chain)
            if not self._exec_ready():
                if wallet:
                    return {"balances": self._rpc_balances(bot_id, chain,
                                                           wallet.get("address") or ""),
                            "positions": [],
                            "wallet_address": wallet.get("address") or ""}
                return {"balances": {}, "positions": []}
            self._exec_path()
            try:
                self.gateway.provision_wallet(bot_id, chain)
            except Exception:
                pass
            if wallet is None:
                try:
                    wallet = self.gateway.ledger.wallet_by_bot_chain(bot_id, chain)
                except Exception:
                    pass
            if not wallet:
                return {"balances": {}, "positions": []}
            try:
                self.gateway.sync(bot_id, chain)
            except Exception:
                pass
            try:
                state = self.gateway.ledger.load_chain_state(wallet["id"]) or {}
            except Exception:
                state = {}
            return {
                "balances": state.get("balances") or {},
                "positions": state.get("positions") or [],
                "wallet_address": wallet.get("address") or "",
            }

        def _verify_wallet_signing(bot_id: int, chain: str) -> tuple[bool, str]:
            """Verify the wallet key can actually sign transactions (a MUST).

            Derives the address from the seed, checks it matches the stored
            address, and does a sign+verify round-trip. Returns (ok, message).
            Falls back to True + "no gateway" when execution isn't configured.
            """
            if not self._exec_ready():
                return True, "no gateway (paper-only mode — key not needed)"
            self._exec_path()
            try:
                from exec_vault import ExecVault
                from sui_adapter import _ed25519_pubkey, _ed25519_sign, _ed25519_verify
                wallet = self.gateway.ledger.wallet_by_bot_chain(bot_id, chain)
                if not wallet or not wallet.get("key_enc"):
                    return False, f"no wallet key stored for {chain} — generate one"
                vault = ExecVault()
                seed = vault.decrypt(wallet["key_enc"])
                if isinstance(seed, str):
                    seed = bytes.fromhex(seed[2:] if seed.startswith("0x") else seed)
                if len(seed) != 32:
                    return False, f"key is {len(seed)} bytes (expected 32) — invalid"
                pub = _ed25519_pubkey(seed)
                import hashlib
                derived = "0x" + hashlib.blake2b(b"\x00" + pub, digest_size=32).hexdigest()
                stored = wallet.get("address", "").lower()
                if derived != stored:
                    return False, (f"key mismatch: derived {derived} ≠ stored {stored}"
                                   " — the key won't sign for this wallet")
                test_msg = b"neko-key-verify"
                sig = _ed25519_sign(seed, test_msg)
                if not _ed25519_verify(pub, test_msg, sig):
                    return False, "sign+verify round-trip FAILED — key cannot sign"
                return True, f"✅ key can sign (derived address matches, sign+verify OK)"
            except Exception as exc:
                return False, f"key verification failed: {exc}"

        async def pnl_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """P&L button = print a Neko-Chan PnL card (PNG) right in the chat."""
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            chain = b.get("chain") or "sui"
            mode = (b.get("trading_mode") or "paper").lower()
            account = {"balances": {}, "positions": []}
            if mode == "paper":
                # PAPER MODE: card from the virtual portfolio (cash + unrealized
                # at Aftermath marks). The old code always read on-chain data,
                # so paper bots got the empty 'No active P&L yet' card.
                try:
                    _ps = self._paper_store()
                    _syms = [p["symbol"] for p in _ps.positions(bot_id)]
                    _paper_prices = await asyncio.get_running_loop().run_in_executor(
                        None, self._paper_mark_prices, _syms,
                        (b.get("network") or "mainnet"))
                    account = _ps.dashboard_account(bot_id, _paper_prices or None)
                except Exception:
                    pass
            else:
                try:
                    account = self._exec_account(bot_id, chain)
                except Exception:
                    pass
            bal = account.get("balances") or {}
            usdc = float(bal.get("USDC", 0))
            native = float(bal.get("native", 0))
            positions = account.get("positions") or []
            open_pnl = sum(float(p.get("pnl") or p.get("unrealized_pnl") or 0) for p in positions)
            total_pnl = float(bal.get("realized_pnl", 0)) + open_pnl

            # No active P&L: no open position and no realized profit -> show an
            # honest empty state instead of generating a meaningless card.
            if not positions and abs(total_pnl) < 0.005:
                await q.message.edit_text(
                    "📭 <b>No active P&L yet</b>\n\n"
                    "No open positions and no realized profit so far.\n"
                    "Once the agent opens a trade, your P&L card will appear here.",
                    parse_mode="HTML",
                    reply_markup=telegram.InlineKeyboardMarkup(
                        [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:pnl"),
                          telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return

            # Build the card PNG; fall back to a text panel if the renderer fails.
            try:
                from cards.generator import generate_pnl_card, random_avatar
                import tempfile as _tf
                out = os.path.join(_tf.gettempdir(),
                                   f"neko_pnl_{bot_id}_{int(time.time())}.png")
                # Pick the first open position for the card's entry/exit prices;
                # fall back to portfolio-level numbers when no position is open.
                p = positions[0] if positions else {}
                pos_entry = float(p.get("entry") or p.get("entry_px") or p.get("entry_price") or 0)
                pos_cur = float(p.get("markPrice") or p.get("mark_price") or p.get("current_price") or pos_entry)
                pos_qty = abs(float(p.get("qty") or p.get("szi") or p.get("quantity") or 0))
                pos_side = str(p.get("side") or "long")
                if pos_entry > 0 and pos_cur > 0:
                    pnl_pct = (pos_cur / pos_entry - 1.0) * 100.0 * (1 if pos_side == "long" else -1)
                    buy_price = pos_entry
                    sell_price = pos_cur
                else:
                    # Portfolio-level: use total P&L as a % of the USDC balance
                    pnl_pct = (total_pnl / max(usdc, 0.01)) * 100.0 if usdc > 0 else total_pnl
                    buy_price = usdc or 0.0
                    sell_price = (usdc + total_pnl) or 0.0
                token = (p.get("symbol") or b['bot_name']).upper() if p else "PORTFOLIO"
                generate_pnl_card(
                    avatar_path=random_avatar(),
                    pnl_pct=pnl_pct,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    token=token,
                    chain=_chain_label(chain).upper(),
                    out_path=out,
                    bot_name=b['bot_name'].upper(),
                    timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                )
                caption = (f"📊 <b>P&amp;L — {_esc(b['bot_name'])}</b>\n"
                           f"💰 USDC <code>${usdc:,.2f}</code> · SUI <code>{native:,.4f}</code>\n"
                           f"📡 Positions {len(positions)} · P&amp;L <b>{_money(total_pnl)}</b>")
                try:
                    with open(out, "rb") as f:
                        sent = await context.bot.send_photo(
                            chat_id=q.message.chat_id, photo=f, caption=caption,
                            parse_mode="HTML",
                            reply_markup=telegram.InlineKeyboardMarkup(
                                [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:pnl"),
                                  telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                    if sent and sent.message_id:
                        threading.Thread(
                            target=lambda: (_ for _ in ()).throw(TypeError("noop")) if False else _delayed_photo_delete(
                                self._master_token or "",
                                q.message.chat_id, sent.message_id),
                            daemon=True).start()
                except Exception:
                    await q.message.edit_text(caption, parse_mode="HTML")
                try:
                    os.remove(out)
                except Exception:
                    pass
            except Exception:
                # Fallback text panel (no Pillow / no templates).
                line = "─" * 28
                text = (f"<b>📊 P&amp;L - {b['bot_name']}</b>\n"
                        f"<code>{line}</code>\n"
                        f"<b>💰 CHAIN BALANCE</b>\n"
                        f"  USDC    <code>${usdc:,.2f}</code>\n"
                        f"  SUI     <code>{native:,.4f}</code>\n"
                        f"  Total   {_money(total_pnl)}\n\n"
                        f"<b>📡 POSITIONS</b>  {len(positions)} open")
                for p in positions[:5]:
                    sym = str(p.get("symbol") or p.get("coin") or "?")
                    side = str(p.get("side") or "long")
                    qty = abs(float(p.get("qty") or p.get("szi") or p.get("quantity") or 0))
                    pp = float(p.get("pnl") or p.get("unrealized_pnl") or 0)
                    text += f"\n  {sym} {side.upper()} {qty:g}  {_money(pp)}"
                text += f"\n<code>{line}</code>"
                await q.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:pnl")],
                                               [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"),
                                                telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            chain = b.get("chain") or "sui"
            mode = (b.get("trading_mode") or "paper").lower()
            account = {"balances": {}, "positions": []}
            if mode == "paper":
                try:
                    _ps = self._paper_store()
                    _syms = [p["symbol"] for p in _ps.positions(bot_id)]
                    _paper_prices = await asyncio.get_running_loop().run_in_executor(
                        None, self._paper_mark_prices, _syms, (b.get('network') or 'mainnet'))
                    account = _ps.dashboard_account(bot_id, _paper_prices or None)
                except Exception:
                    pass
            else:
                try:
                    account = self._exec_account(bot_id, chain)
                except Exception:
                    pass
            positions = account.get("positions") or []
            lines = [f"💰 Active Positions - {b['bot_name']}\n"]
            kb_rows = []
            if not positions:
                lines.append("No open positions.")
            for p in positions[:6]:
                sym = str(p.get("symbol") or p.get("coin") or "?")
                side = str(p.get("side") or "long")
                qty = abs(float(p.get("qty") or p.get("szi") or p.get("quantity") or 0))
                entry = float(p.get("entry") or p.get("entry_px") or 0)
                cur = float(p.get("markPrice") or p.get("mark_price") or p.get("current_price") or entry)
                stop = p.get("stop") or p.get("stop_loss") or ""
                tgt = p.get("target") or p.get("take_profit") or ""
                pnl = float(p.get("pnl") or p.get("unrealized_pnl") or 0)
                lev = p.get("leverage") or p.get("lev") or ""
                if entry and cur:
                    pnl_pct = (cur / entry - 1.0) * 100.0 * (1 if str(side).lower() == "long" else -1)
                else:
                    pnl_pct = 0.0
                meta = f"entry {entry:,.2f} → {cur:,.2f} ({pnl_pct:+.2f}%)"
                if stop:
                    meta += f" · SL {stop}"
                if tgt:
                    meta += f" · TP {tgt}"
                if lev:
                    meta += f" · {lev}x"
                lines.append(f"  {sym}  {side.upper()} {qty:g}  {_money(pnl)}\n     {meta}")
                # CLOSE BUTTON: one per position, closes at Aftermath mark.
                if mode == "paper":
                    kb_rows.append([telegram.InlineKeyboardButton(
                        f"❌ Close {sym} {side.upper()}",
                        callback_data=f"sb:pclose:{sym}")])
            if mode == "paper" and positions:
                lines.append("\nTap ❌ below a position to close it at market.")
            if kb_rows:
                kb_rows.append([telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:pos")])
                kb_rows.append([telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"),
                                telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")])
                await q.message.edit_text("\n".join(lines), parse_mode="HTML",
                                          reply_markup=telegram.InlineKeyboardMarkup(kb_rows))
                return
            await q.message.edit_text("\n".join(lines), parse_mode="HTML",
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:pos")],
                                           [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def paper_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Manual close of ONE paper position from the Active Positions
            view: sb:pclose:<SYMBOL> -> confirm -> sb:pclose_yes:<SYMBOL>."""
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            mode = (b.get("trading_mode") or "paper").lower()
            if mode != "paper":
                await q.answer("Live closes are executed by the bot's exit logic", show_alert=True)
                return
            sym = (q.data or "").split(":", 2)[-1].upper()
            if not q.data.endswith("_yes"):
                pos = next((p for p in self._paper_store().positions(bot_id)
                            if p["symbol"] == sym), None)
                if not pos:
                    await q.answer(f"No open paper position on {sym}", show_alert=True)
                    return
                await q.message.edit_text(
                    f"❌ <b>Close {sym} {str(pos['direction']).upper()}?</b>\n"
                    f"entry {pos['entry_price']:,.4f} · qty {pos['qty']:g}\n\n"
                    "It will close at the current Aftermath market price "
                    "It closes at the current market price.",
                    parse_mode="HTML",
                    reply_markup=telegram.InlineKeyboardMarkup([
                        [telegram.InlineKeyboardButton("✅ Close position",
                                                       callback_data=f"sb:pclose_yes:{sym}"),
                         telegram.InlineKeyboardButton("↩️ Keep it",
                                                       callback_data="sb:pos")]])
                )
                return
            # confirmed
            prices = await asyncio.get_running_loop().run_in_executor(
                None, self._paper_mark_prices, [sym], (b.get('network') or 'mainnet'))
            ref = prices.get(sym)
            if not ref:
                pos = next((p for p in self._paper_store().positions(bot_id)
                            if p["symbol"] == sym), None)
                ref = float(pos["entry_price"]) if pos else 0.0
            if ref <= 0:
                await q.answer("No price available right now — try again", show_alert=True)
                return
            fill = self._paper_gateway().close(
                bot_id, sym, ref, idempotency_key=f"manual-{bot_id}-{sym}-{int(time.time())}")
            if not fill.get("ok"):
                await q.answer(fill.get("error", "close failed"), show_alert=True)
                return
            await q.message.edit_text(
                f"✅ <b>Closed {sym}</b> at {fill['fill_price']:,.4f}\n"
                f"Realized P&L: <b>{_money(fill['pnl'])}</b>\n"
                f"Paper balance updated.",
                parse_mode="HTML",
                reply_markup=telegram.InlineKeyboardMarkup([
                    [telegram.InlineKeyboardButton("💰 Active Positions", callback_data="sb:pos"),
                     telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]])
            )

        async def live_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            lines = ["🏦 Live Markets  (real-time)\n"]
            markets = json.loads(bot["symbols"] or "{}")
            syms = []
            if markets.get("perps") or markets.get("spot"):
                syms += [("BTC", "crypto", "⚡" if markets.get("perps") else "₿"), ("ETH", "crypto", "⚡" if markets.get("perps") else "₿")]
            if markets.get("us-stock"):
                syms += [("AAPL", "us-stock", "📈"), ("NVDA", "us-stock", "📈"), ("SPY", "us-stock", "📈")]
            if markets.get("forex"):
                syms += [("EURUSD", "forex", "💱"), ("USDJPY", "forex", "💱"), ("GBPUSD", "forex", "💱")]
            for sym, market, icon in syms[:6]:
                try:
                    px = self.platform.price(platform_token, market, sym)
                    lines.append(f"{icon} {sym}  ${px:,.4f}")
                except Exception:
                    lines.append(f"{icon} {sym}  (market closed / unavailable)")
            lines.append("\n[↻ Now] refreshes live prices.")
            if markets.get("us-stock") or markets.get("forex"):
                lines.append(f"\n{USERBOT['release_live']}")
            await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("↻ Now", callback_data="sb:live")],
                 [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            lines = [
                "📈 US Stocks\n",
                "AAPL · NVDA · SPY\n",
                "\n",
                f"{USERBOT['release_live']}\n",
                "\nTokenized US stocks (AAPL, NVDA, SPY) will trade 24/7 with "
                "leverage on Solana once released.",
            ]
            await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"),
                  telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            try:
                sigs = self.platform.signals(bot["agent_id"], limit=10) if bot.get("agent_id") else []
            except Exception:
                sigs = []
            lines = ["📡 Recent decisions\n"]
            if not sigs:
                lines.append("No trades yet - your bot will push every fill here.")
            for s in sigs[:10]:
                side = str(s.get("side") or s.get("action") or "?").upper()
                sym = s.get("symbol", "?")
                px = s.get("entry_price") or s.get("price") or 0
                qty = s.get("quantity") or 0
                when = str(s.get("created_at") or s.get("executed_at") or "")[11:19] or "?"
                lines.append(f"{when}  {side} {sym} {qty} @ ${px:,.4f}")
            await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:trades")],
                 [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            try:
                lb = self.platform.leaderboard(platform_token)
            except Exception:
                await q.message.edit_text("⚠️ Platform offline.")
                return
            lines = ["🏆 Leaderboard (live)\n"]
            for i, a in enumerate(lb.get("top_agents", [])[:10], 1):
                name = a.get("name", "?")
                pct = a.get("total_profit_percent", 0)
                mark = " ← you" if name == bot["agent_name"] else ""
                lines.append(f"{i}. {name}  {pct:+.2f}%{mark}")
            await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:lb")],
                 [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def bot_controls(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            if q.data == "sb:pause_yes":
                # Pause TRADING (LLM) only — keep the Telegram bot session online
                self.registry.update_bot(bot_id, is_running=1, paused=1)
                if self.agent_pool:
                    self.agent_pool.stop(bot_id)
                await q.message.edit_text("⏸️ Trading paused — LLM stopped, bot stays online. Dashboard, Send/Receive still work.\n\nTap ▶️ Resume to restart trading.", reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            if q.data == "sb:resume":
                self.registry.update_bot(bot_id, is_running=1, paused=0)
                self.start_bot(bot_id)
                if self.agent_pool:
                    self.agent_pool.start(bot_id)
                await q.message.edit_text(USERBOT["resumed"], reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            if q.data == "sb:pause":
                await q.message.edit_text(USERBOT["pause_confirm"].format(name=b["bot_name"]),
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("✅ Pause", callback_data="sb:pause_yes")], [telegram.InlineKeyboardButton("↩️ Cancel", callback_data="sb:dash")]]))
                return
            if q.data == "sb:delete_yes":
                self.stop_bot(bot_id)
                if self.agent_pool:
                    self.agent_pool.stop(bot_id)
                self.registry.delete_bot(bot_id, tg_id)
                await q.message.edit_text(USERBOT["deleted"])
                return
            if q.data == "sb:delete":
                await q.message.edit_text(USERBOT["delete_confirm"].format(name=b["bot_name"]),
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("✅ Yes, delete", callback_data="sb:delete_yes")], [telegram.InlineKeyboardButton("↩️ Keep it", callback_data="sb:dash")]]))
                return
            if q.data in ("sb:mode_live", "sb:mode_paper"):
                # MODE SWITCH: paper -> live is the dangerous direction and
                # gets a full confirmation with the real-funds warning.
                going_live = q.data == "sb:mode_live"
                if not going_live:
                    self.registry.update_bot(bot_id, trading_mode="paper")
                    if self.agent_pool:
                        self.agent_pool.stop(bot_id)
                        self.agent_pool.start(bot_id)
                    await q.message.edit_text(
                        "🧪 <b>PAPER MODE ON</b>\n\n"
                        "Virtual $1,000 portfolio. Fills at live prices with the "
                        "real cost model (fees + slippage) so results mirror live "
                        "trading. No real money moves.\n\n"
                        "Reset the paper portfolio anytime in ⚙️ Settings.",
                        parse_mode="HTML",
                        reply_markup=telegram.InlineKeyboardMarkup(
                            [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                    return
                # live confirmation
                held = any((p.get("quantity") or 0) != 0
                           for p in (self._exec_account(bot_id, (b.get("chain") or "sui")).get("positions") or [])) \
                    if self._exec_ready() else False
                warn = ("\n⚠️ You currently have <b>open positions</b>. Switching to "
                        "paper hides them from the dashboard until you switch back." if held else "")
                await q.message.edit_text(
                    "🔴 <b>SWITCH TO LIVE TRADING?</b>\n\n"
                    f"Bot: <b>{b['bot_name']}</b>\n"
                    "From now on the bot trades your <b>real on-chain funds</b>:\n"
                    "• Entries, stops and exits execute for real on "
                    f"{(b.get('chain') or 'sui').upper()} ({b.get('network') or 'mainnet'})\n"
                    "• Every trade pays the venue fee + the platform fee\n"
                    "• Losses are real losses\n" + warn +
                    "\n\nPaper portfolio stays saved — switch back anytime.",
                    parse_mode="HTML",
                    reply_markup=telegram.InlineKeyboardMarkup([
                        [telegram.InlineKeyboardButton("🔴 Yes, go LIVE",
                                                       callback_data="sb:mode_live_yes"),
                         telegram.InlineKeyboardButton("↩️ Stay on paper",
                                                       callback_data="sb:dash")]]))
                return
            if q.data == "sb:mode_live_yes":
                self.registry.update_bot(bot_id, trading_mode="live")
                if self.agent_pool:
                    self.agent_pool.stop(bot_id)
                    self.agent_pool.start(bot_id)
                await q.message.edit_text(
                    "🔴 <b>LIVE MODE ON</b> — the bot now trades your real funds. "
                    "Trade discipline, stops and the one-shot rule all apply for real. "
                    "Paper portfolio is saved; switch back anytime.",
                    parse_mode="HTML",
                    reply_markup=telegram.InlineKeyboardMarkup(
                        [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            if q.data == "sb:paper_reset":
                self.registry.update_bot(bot_id, trading_mode="paper")
                try:
                    from paper_store import PaperStore
                    PaperStore(self.registry.path).reset(bot_id)
                except Exception as exc:
                    await q.answer("reset failed")
                    return
                await q.message.edit_text(
                    "🧪 Paper portfolio reset to <b>$1,000.00</b>. Fresh start.",
                    parse_mode="HTML",
                    reply_markup=telegram.InlineKeyboardMarkup(
                        [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            text = (f"🤖 {b['bot_name']} · bot status\n\n"
                    f"Bot:       {'🟢 ONLINE' if b['is_running'] else '⛔ OFFLINE'}\n"
                    f"Trading:   {'⏸️ PAUSED' if b['paused'] else '🟢 ACTIVE'}\n"
                    f"Heartbeat: {_ago(b['last_heartbeat'])}\n"
                    f"Profile:   {b['risk_profile']}\n"
                    f"Interval:  {b['interval_sec']}s\n"
                    f"Markets:   {b['symbols']}\n"
                    f"Leverage:  {b.get('leverage') or 1.0}x\n"
                    f"Last error: {humanize_error(b['last_error']) if b['last_error'] else 'none'}")
            kb = [[telegram.InlineKeyboardButton("⏸️ Pause Trading" if not b["paused"] else "▶️ Resume Trading", callback_data="sb:pause" if not b["paused"] else "sb:resume")],
                  [telegram.InlineKeyboardButton("🗑️ Delete Bot", callback_data="sb:delete")],
                  [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]
            await q.message.edit_text(text, reply_markup=telegram.InlineKeyboardMarkup(kb))

        async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            if q.data.startswith("sb:set_network"):
                net = q.data.rsplit(":", 1)[1]
                if net not in ("mainnet", "testnet"):
                    net = "testnet"
                self.registry.update_bot(bot_id, network=net)
                label = {"mainnet": "🌐 mainnet", "testnet": "🧪 testnet"}.get(net, net)
                await q.message.edit_text(f"✅ Network set to: <b>{label}</b>\n\n"
                                          f"⚠️ This affects which endpoints your orders hit. "
                                          f"Restart your bot for it to take effect.",
                                          parse_mode="HTML",
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(BACK, callback_data="sb:settings")]]))
                return
            if q.data.startswith("sb:set_interval"):
                seconds = int(q.data.rsplit(":", 1)[1])
                self.registry.update_bot(bot_id, interval_sec=seconds)
                await q.message.edit_text(f"Saved ✓ (interval {seconds}s)",
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(BACK, callback_data="sb:settings")]]))
                return
            if q.data.startswith("sb:set_trader_type"):
                ttype = q.data.rsplit(":", 1)[1]
                # Scalp principles baked in: tight stops + moderate 3x leverage
                # (a leverage spike + tight stop = liquidation). Suggest the
                # matching default but let the user override afterwards.
                leverage_for = {"scalp": 3.0, "intraday": 2.0, "swing": 2.0, "auto": 2.0}
                self.registry.update_bot(bot_id, trader_type=ttype,
                                         leverage=leverage_for.get(ttype, 2.0))
                await q.message.edit_text(
                    f"✅ Trader type set to: <b>{ttype.upper()}</b>\n\n"
                    f"⚖️ Suggested leverage for {ttype.upper()}: "
                    f"<b>{leverage_for.get(ttype, 2.0):g}x</b> (adopted - you can change "
                    f"it from the Leverage button below).\n\n"
                    f"Restart your bot for this to take effect on the next cycle.",
                    parse_mode="HTML",
                    reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(BACK, callback_data="sb:settings")]]))
                return
            if q.data.startswith("sb:set_leverage"):
                lev = float(q.data.rsplit(":", 1)[1])
                self.registry.update_bot(bot_id, leverage=lev)
                await q.message.edit_text(f"✅ Leverage set to: <b>{lev:g}x</b>\n\n"
                                          f"⚠️ Higher leverage = faster liquidation. Restart your bot "
                                          f"for this to take effect on new trades.",
                                          parse_mode="HTML",
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(BACK, callback_data="sb:settings")]]))
                return
            current_ttype = b.get("trader_type") or "scalp"
            icons = {"scalp": "⚡", "intraday": "⏱", "swing": "📈", "auto": "🤖"}
            ttype_label = f"{icons.get(current_ttype, '❓')} {current_ttype.upper()}"
            current_chain = b.get("chain") or "sui"
            current_network = b.get("network") or "mainnet"
            _net_label = {"mainnet": "🌐 mainnet", "testnet": "🧪 testnet"}.get(current_network, current_network)
            text = (f"⚙️ Settings - {b['bot_name']}\n\n"
                    f"Chain:     {_chain_label(current_chain)}\n"
                    f"Network:   {_net_label}\n"
                    f"Interval:  {b['interval_sec']}s\n"
                    f"Risk:      {b['risk_profile']}\n"
                    f"Leverage:  {float(b.get('leverage') or 1):g}x\n"
                    f"Trader:    {ttype_label}\n"
                    f"AI key:    {'set ✓' if self.registry.get_active_key(tg_id) else 'not set'}")
            kb = [
                [telegram.InlineKeyboardButton(f"⛓ Chain: {_chain_label(current_chain)}", callback_data="sb:chain")],
                [telegram.InlineKeyboardButton(f"🌐 Network: {'🌐 mainnet' if current_network == 'mainnet' else '🧪 testnet'}", callback_data="sb:set_network:mainnet"),
                 telegram.InlineKeyboardButton("testnet", callback_data="sb:set_network:testnet")],
                [telegram.InlineKeyboardButton(f"⏱ Interval: {b['interval_sec']}s", callback_data="sb:set_interval:120"),
                 telegram.InlineKeyboardButton("60s", callback_data="sb:set_interval:60"),
                 telegram.InlineKeyboardButton("5m", callback_data="sb:set_interval:300")],
                [telegram.InlineKeyboardButton(f"{icons.get('scalp','⚡')} Scalp", callback_data="sb:set_trader_type:scalp"),
                 telegram.InlineKeyboardButton(f"{icons.get('intraday','⏱')} Intraday", callback_data="sb:set_trader_type:intraday"),
                 telegram.InlineKeyboardButton(f"{icons.get('swing','📈')} Swing", callback_data="sb:set_trader_type:swing")],
                [telegram.InlineKeyboardButton(f"{icons.get('auto','🤖')} Auto", callback_data="sb:set_trader_type:auto")],
                [telegram.InlineKeyboardButton(f"⚖️ Lev: {float(b.get('leverage') or 20):g}x", callback_data="sb:set_leverage:20"),
                 telegram.InlineKeyboardButton("20x", callback_data="sb:set_leverage:20"),
                 telegram.InlineKeyboardButton("30x", callback_data="sb:set_leverage:30"),
                 telegram.InlineKeyboardButton("40x", callback_data="sb:set_leverage:40")],
                [telegram.InlineKeyboardButton("📋 Watchlist", callback_data="sb:watchlist")],
                [telegram.InlineKeyboardButton("🧪 Reset Paper Portfolio", callback_data="sb:paper_reset")],
                [telegram.InlineKeyboardButton("🛡️ Execution Risk", callback_data="sb:execrisk")],
                [telegram.InlineKeyboardButton("🔑 Change AI Key", callback_data="key:start")],
                [telegram.InlineKeyboardButton("🆘 Contact Support", callback_data="sb:support")],
                [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")],
            ]
            await q.message.edit_text(text, reply_markup=telegram.InlineKeyboardMarkup(kb))

        async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            chain = b.get("chain") or "sui"
            watched = _parse_watchlist(b.get("watchlist"))
            default = {
                "sui": ["BTC", "ETH", "SOL", "SUI", "HYPE"],
                "solana": ["BTC", "ETH", "SOL", "SUI", "DOGE"],
                "hyperliquid": ["BTC", "ETH", "SOL", "SUI", "HYPE"],
            }.get(chain, ["BTC", "ETH"])
            active = watched or default
            text = (f"📋 <b>Watchlist</b> - {_chain_label(chain)}\n\n"
                    f"Assets Neko-Chan is analyzing:\n"
                    + "\n".join(f"  • {_esc(s)}" for s in active)
                    + f"\n\nType <b>watch &lt;ASSET&gt;</b> (e.g. <b>watch XRP</b>) "
                      f"to add an asset. Only assets with an active perp market "
                      f"on {_chain_label(chain)} can be watched — everything else "
                      f"is rejected with the supported list.")
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(BACK, callback_data="sb:settings")]]))

        async def text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Handle "start", "pause", "resume" text commands — control the
            agent (LLM + quant) without affecting the bot connection itself."""
            raw = (update.message.text or "").strip().lower()
            if raw in ("start", "resume", "go", "trade"):
                if not self.registry.get_active_key(tg_id):
                    await update.message.reply_text("🔑 Set an AI key first in Settings.",
                                                    parse_mode="HTML")
                    return
                self.registry.update_bot(bot_id, paused=0, is_running=1)
                if self.agent_pool:
                    self.agent_pool.start(bot_id)
                try:
                    await update.message.delete()
                except Exception:
                    pass
                await update.message.reply_text("▶️ Agent started. Neko-Chan is scanning markets.",
                                                reply_markup=telegram.InlineKeyboardMarkup(
                                                    [[telegram.InlineKeyboardButton("👀 Peek", callback_data="sb:peek"),
                                                      telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            if raw in ("pause", "stop", "halt"):
                self.registry.update_bot(bot_id, is_running=1, paused=1)
                if self.agent_pool:
                    self.agent_pool.stop(bot_id)
                try:
                    await update.message.delete()
                except Exception:
                    pass
                await update.message.reply_text("⏸️ Trading paused — LLM stopped, bot stays online.",
                                                reply_markup=telegram.InlineKeyboardMarkup(
                                                    [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            # Fall through to watch_command for everything else.
            await watch_command(update, context)

        async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
            # Reply, then delete the user's command message so the chat stays clean.
            async def _respond(text: str):
                try:
                    await update.message.reply_text(text, parse_mode="HTML")
                finally:
                    try:
                        await update.message.delete()
                    except Exception:
                        pass
            raw = (update.message.text or "").strip()
            # "watch <ASSET>" or "watch <ASSET> now" - case-insensitive, any spacing
            import re as _re
            m = _re.match(r"(?i)^\s*watch\s+([a-z0-9]+)(?:\s+(now|nwo|nw))?\s*$", raw)
            if not m:
                await _respond("Type <b>watch &lt;ASSET&gt;</b>, e.g. <b>watch XRP</b> "
                               "(add <b>now</b> to demand the trade immediately).")
                return
            asset = m.group(1).upper()
            priority = bool(m.group(2))
            b = self.registry.get_bot(bot_id)
            chain = b.get("chain") or "sui"
            mode = (b.get("trading_mode") or "paper").lower()
            # Check the asset is a known token on the chain (not just the venue's
            # perp markets - e.g. IKA is a native Sui token even though Aftermath
            # doesn't list an IKA perp market).
            supported = _chain_supported_assets(chain)
            if supported is not None and asset not in supported:
                await _respond(
                    f"❌ <b>{asset}</b> isn't a known token on {_chain_label(chain)}.\n"
                    f"Known: {', '.join(sorted(supported))}")
                return
            # Check the asset is available as a perp on the venue. Not all chain
            # tokens have a perp market - e.g. IKA is a native Sui token but
            # Aftermath doesn't list IKA-PERP. If there's no perp market, the
            # agent can't fetch a price or build scenarios, so reject early.
            if not _is_perp_tradeable(chain, asset):
                perps = _is_perp_tradeable_map().get(chain) or set()
                await _respond(
                    f"❌ <b>{asset}</b> is not watchable on {_chain_label(chain)}.\n"
                    f"Only assets with an active perp market on this chain can be "
                    f"watched, analyzed, and traded.\n"
                    f"Supported here: {', '.join(sorted(perps))}")
                return
            # CHART-DATA GATE: a perp market without candle data can never be
            # analyzed (the 4h trend scan would fail) - it would sit 'analyzing…'
            # forever. Verify the same candle source the agent uses before
            # accepting the watch.
            try:
                import requests as _req
                _now_ms = int(time.time() * 1000)
                _cr = _req.post("https://api.hyperliquid.xyz/info", json={
                    "type": "candleSnapshot",
                    "req": {"coin": asset, "interval": "4h",
                            "startTime": _now_ms - 3 * 4 * 3600000,
                            "endTime": _now_ms},
                }, timeout=10)
                _candles = _cr.json() if _cr.status_code == 200 else []
                if not isinstance(_candles, list) or len(_candles) < 2:
                    await _respond(
                        f"❌ <b>{asset}</b> has a perp market but no chart data "
                        f"available for analysis yet - watching it would never "
                        f"produce a decision. Try one of: "
                        f"{', '.join(sorted(_is_perp_tradeable_map().get(chain) or set()))}")
                    return
            except Exception:
                pass  # chart API hiccup - don't block the watch on a timeout
            # Is this asset already in an open position? If so, the agent won't
            # re-analyze it until that trade resolves - adding it to the watch
            # would OVERRIDE that. Ask the user to confirm before overriding.
            try:
                account = self._exec_account(bot_id, chain)
                held = any(
                    (str(p.get("symbol") or p.get("coin") or "")).upper() == asset
                    and float(p.get("qty") or p.get("szi") or p.get("quantity") or 0) != 0
                    for p in (account.get("positions") or [])
                )
            except Exception:
                held = False
            if held:
                if not priority:
                    q = update.message
                    await q.delete()
                    await update.message.reply_text(
                        f"⚠️ <b>{asset}</b> already has an <b>OPEN position</b>.\n\n"
                        f"Neko-Chan normally waits for that trade to resolve before "
                        f"analyzing {asset} again (no stacking low-conviction entries).\n\n"
                        f"Watch it anyway for the <b>next</b> trade?  "
                        f"(or <b>watch {asset} now</b> to close it and take {asset} immediately)",
                        parse_mode="HTML",
                        reply_markup=telegram.InlineKeyboardMarkup([
                            [telegram.InlineKeyboardButton("✅ Yes, watch it", callback_data=f"watch:yes:{asset}"),
                             telegram.InlineKeyboardButton("✖️ No", callback_data=f"watch:no:{asset}")],
                        ]))
                    return
                # PRIORITY WATCH: paper mode frees the one-position slot by
                # closing open positions at market (with a note about P&L).
                if mode == "paper":
                    try:
                        _ps = self._paper_store()
                        _syms = [p["symbol"] for p in _ps.positions(bot_id)]
                        _mk = await asyncio.get_running_loop().run_in_executor(
                            None, self._paper_mark_prices, _syms,
                            (b.get("network") or "mainnet"))
                        _closed, _pnl = 0, 0.0
                        for _p in _ps.positions(bot_id):
                            _ref = _mk.get(_p["symbol"]) or _p["entry_price"]
                            _res = self._paper_gateway().close(
                                bot_id, _p["symbol"], _ref,
                                idempotency_key=f"watch-now-close-{bot_id}-{_p['symbol']}-{int(time.time())}")
                            if _res.get("ok"):
                                _closed += 1
                                _pnl += float(_res.get("pnl", 0))
                        _close_note = (f"closed {_closed} position(s) "
                                       f"(P&L {_money(_pnl)}) to free the slot"
                                       if _closed else "no open positions to close")
                    except Exception as exc:
                        _close_note = f"close failed ({str(exc)[:60]})"
                else:
                    _close_note = ("live mode: close your position(s) from Active "
                                   f"Positions first — the priority watch will take "
                                   f"the next {asset} setup once a slot is free")
                self.registry.update_bot(bot_id, priority_watch=asset)
                self._restart_agent_for_watch(bot_id, b)
                await _respond(
                    f"⚡ <b>PRIORITY WATCH: {asset}</b>\n"
                    f"{_close_note}.\n"
                    f"The bot will take the next valid <b>{asset}</b> setup — "
                    f"its one-shot is reserved for exactly this trade.")
                return

            def _apply_watch():
                watched = set(_parse_watchlist(b.get("watchlist")))
                watched.add(asset)
                self.registry.update_bot(bot_id, watchlist=",".join(sorted(watched)))
                if priority:
                    self.registry.update_bot(bot_id, priority_watch=asset)
                # Restart the agent so it immediately picks up the new watchlist
                # (WATCHED is read at agent startup). If it's not running, leave it.
                if self.agent_pool:
                    try:
                        self.agent_pool.stop(bot_id)
                    except Exception:
                        pass
                    if b.get("is_running"):
                        try:
                            self.agent_pool.start(bot_id)
                        except Exception:
                            pass

            _apply_watch()
            await _respond(
                f"✅ <b>{asset}</b> added to your watchlist."
                + ("\n⚡ <b>PRIORITY:</b> the bot will take the next valid "
                   f"<b>{asset}</b> setup — other assets wait." if priority else "")
                + "\nNeko-Chan is now focused on <b>{0}</b> for reasoning and trades.".format(asset))

        def _restart_agent_for_watch(self, bot_id: int, b: dict) -> None:
            """Restart the agent so it picks up the new watchlist/priority_watch
            immediately (agent env is read at startup). Keeps is_running state."""
            if not self.agent_pool:
                return
            try:
                self.agent_pool.stop(bot_id)
            except Exception:
                pass
            if b.get("is_running"):
                try:
                    self.agent_pool.start(bot_id)
                except Exception:
                    pass

        async def watch_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            parts = q.data.split(":")
            if len(parts) < 3:
                await q.message.edit_text("❓ Hmm, that option didn't parse. Try <b>watch &lt;ASSET&gt;</b> again.",
                                          parse_mode="HTML")
                return
            choice, asset = parts[1].upper(), parts[2].upper()
            if choice == "NO":
                await q.message.edit_text(
                    f"👌 Understood - <b>{asset}</b> stays position-only. Neko-Chan will "
                    f"analyze it again once your current trade resolves.",
                    parse_mode="HTML")
                return
            b = self.registry.get_bot(bot_id)
            watched = set(_parse_watchlist(b.get("watchlist")))
            watched.add(asset)
            self.registry.update_bot(bot_id, watchlist=",".join(sorted(watched)))
            if self.agent_pool:
                try:
                    self.agent_pool.stop(bot_id)
                except Exception:
                    pass
                if b.get("is_running"):
                    try:
                        self.agent_pool.start(bot_id)
                    except Exception:
                        pass
            await q.message.edit_text(
                f"✅ <b>{asset}</b> added to your watchlist.\n"
                f"You already hold a position, so Neko-Chan will analyze it for the "
                f"<b>next</b> trade once the current one resolves.",
                parse_mode="HTML")

        def _parse_watchlist(raw):
            try:
                raw = raw or ""
                if isinstance(raw, (list, tuple)):
                    return [str(x).upper() for x in raw if str(x).strip()]
                return [x.strip().upper() for x in str(raw).split(",") if x.strip()]
            except Exception:
                return []

        def _chain_supported_assets(chain: str) -> list[str] | None:
            """Return the tradeable assets for this chain, or None if the chain
            is not recognized (meaning any asset is allowed).

            On a recognized chain this is EXACTLY the venue's perp market list:
            a coin without an Aftermath perp market cannot be priced, analyzed,
            or traded — so it is NOT watchable in the first place. No more
            'watching DEEP forever with no decisions'."""
            perp = _is_perp_tradeable_map()
            return sorted(perp.get(chain)) if chain in perp else None

        def _is_perp_tradeable_map() -> dict[str, set[str]]:
            """Venue perp markets per chain (Aftermath Perps mainnet, verified
            from /api/ccxt/markets, 2026-09)."""
            return {
                "sui": {"BTC", "ETH", "SOL", "XAUT", "SUI", "HYPE", "XRP", "UNI",
                        "XMR", "ZEC", "MON", "XAG", "WTI", "US500", "GOOGL",
                        "NVDA", "TSLA", "INTC", "MU", "MRVL", "SNDK", "AMC",
                        "DRAM", "LLY", "IOVA", "SPCX", "PUMP", "CHIP", "LIT"},
                "solana": {"BTC", "ETH", "SOL", "SUI", "DOGE"},
                "hyperliquid": {"HYPE", "BTC", "ETH", "SOL", "SUI", "ARB", "DOGE",
                                "LINK", "SEI", "NEAR", "ATOM", "AAVE", "UNI", "PURR"},
            }

        def _is_perp_tradeable(chain: str, asset: str) -> bool:
            """Check if an asset has a perp market on the venue for this chain.
            Only assets with an active perp market can be priced and traded."""
            perps = _is_perp_tradeable_map().get(chain)
            if perps is None:
                return True  # unknown chain, allow
            return asset.upper() in perps

        async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            text = ("🆘 <b>Contact Support</b>\n\n"
                    "Need help? Reach the Neko-Chan team:\n\n"
                    "• Telegram: @support\n"
                    "• Describe what happened and include your bot name.")
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(BACK, callback_data="sb:settings")]]))

        async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            # Only trade events belong in the notification button: fills, closes,
            # stops, targets, liquidations. Milestone pings, position alerts and
            # daily reports are chat-only and never shown here.
            trade_kinds = {"watcher_fill", "watcher_close", "watcher_stop",
                           "watcher_target", "watcher_liq"}
            events = [e for e in self.registry.recent_events(tg_id, 60)
                      if e["kind"] in trade_kinds]
            lines = ["📬 Trade Notifications\n"]
            if not events:
                lines.append("No trade notifications yet.")
            for e in events[:15]:
                payload = e.get("payload") or {}
                txt = (payload.get("text") or "") if isinstance(payload, dict) else ""
                if txt:
                    first = txt.split("\n")[0]
                    lines.append(f"{e['sent_at'][11:19]} · {first[:90]}")
                else:
                    lines.append(f"{e['sent_at'][11:19]} · {e['kind']}")
            await q.message.edit_text("\n".join(lines), reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("↻ Refresh", callback_data="sb:inbox")],
                 [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"), telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def help_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            chain = b.get("chain") or "sui"
            ttype = b.get("trader_type") or "scalp"
            text = (
                "❓ <b>Help - how Neko-Chan works</b>\n\n"
                f"🤖 <b>Your bot</b>: {b['bot_name']}\n"
                f"⛓ <b>Chain</b>: {_chain_label(chain)} · trader: {ttype}\n\n"
                "🧠 <b>How decisions are made</b>\n"
                "Every few minutes I scan the markets in my watchlist, build "
                "long and short scenarios with real probabilities, then pick "
                "the strongest setup - or stay flat. My AI reads the same "
                "numbers; it never invents trades.\n\n"
                "🛡️ <b>Risk controls (always on)</b>\n"
                "• Every trade has a stop-loss and take-profit\n"
                "• Position size is capped to your risk profile\n"
                "• Daily trade limit + daily loss halt\n"
                "• Kill-switch flattens everything instantly\n\n"
                "💼 <b>Your wallet</b>\n"
                "Non-custodial: you hold the private key. Deposit USDC to your "
                "address, Neko-Chan trades it. Withdraw by sweeping the key.\n\n"
                "📊 <b>Screens</b>\n"
                "• Send / Receive - move funds\n"
                "• P&L - your performance\n"
                "• Active Positions - open trades\n"
                "• Settings - chain, trader type, leverage, watchlist, AI key\n\n"
                "⚠️ <b>Risk</b>\n"
                "Trading involves real risk and real money. Models make mistakes "
                "- even cats. Never risk what you can't afford to lose.\n\n"
                "Support: @support"
            )
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("⚙️ Settings", callback_data="sb:settings")],
                [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"),
                 telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")],
            ])
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

        async def wallet_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            chain = b.get("chain") or "sui"
            try:
                account = self._exec_account(bot_id, chain)
            except Exception:
                account = {"balances": {}, "positions": []}
            addr = account.get("wallet_address") or ""
            bal = account.get("balances") or {}
            usdc = float(bal.get("USDC", 0))
            native = float(bal.get("native", 0))
            lines = [
                f"<b>💼 Wallet - {b['bot_name']}</b>",
                f"<code>{'─' * 26}</code>",
                f"{_chain_label(chain)}",
                f"address <code>{_mask_addr(addr) if addr else 'not generated'}</code>",
                f"USDC <code>${usdc:,.2f}</code> · native {native:,.4f}",
            ]
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("📥 Receive", callback_data="sb:receive"),
                 telegram.InlineKeyboardButton("📤 Send", callback_data="sb:send")],
                [telegram.InlineKeyboardButton("🗝️ Private Keys", callback_data="sb:keys")],
                [telegram.InlineKeyboardButton("⛓ Switch Chain", callback_data="sb:chain")],
                [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash"),
                 telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")],
            ])
            await _safe_edit(q, "\n".join(lines), reply_markup=kb)

        async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            chain = b.get("chain") or "sui"
            try:
                account = self._exec_account(bot_id, chain)
            except Exception:
                account = {"balances": {}, "positions": []}
            addr = account.get("wallet_address") or ""
            if not addr:
                # HARD FALLBACK: read directly from the registry so a freshly
                # generated wallet always shows here, never asks to regenerate.
                try:
                    _b = self.registry.get_bot(bot_id)
                    addr = (_b or {}).get("wallet_addr") or ""
                    if addr:
                        account["wallet_address"] = addr
                except Exception:
                    pass
            if not addr:
                # No wallet yet: offer to generate + store one now.
                text = ("💼 <b>No wallet yet</b>\n\n"
                        "Generate a {chain} trading wallet to receive funds. "
                        "You'll be shown the private key once - store it safely.".format(
                            chain=_chain_label(chain)))
                kb = telegram.InlineKeyboardMarkup([
                    [telegram.InlineKeyboardButton("⚙️ Generate Wallet", callback_data=f"sb:gen_wallet:{chain}")],
                    [telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")],
                ])
                await _safe_edit(q, text, reply_markup=kb)
                return
            # QR code for the receive address
            photo_path = None
            try:
                import qrcode
                img = qrcode.make(addr)
                import tempfile as _tf
                photo_path = os.path.join(_tf.gettempdir(), f"neko_qr_{bot_id}.png")
                img.save(photo_path)
            except Exception:
                photo_path = None
            text = (f"📥 <b>Receive on {_chain_label(chain)}</b>\n\n"
                    f"Scan the QR or send USDC (or {chain} native) to this address:\n\n"
                    f"<code>{_esc(addr)}</code>\n\n"
                    f"Only send {_chain_label(chain)} assets here. The bot activates "
                    f"once a deposit is detected.")
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("🔍 Check Deposits", callback_data="sb:check_deposits")],
                [telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")],
            ])
            if photo_path:
                try:
                    with open(photo_path, "rb") as f:
                        sent = await q.message.reply_photo(photo=f, caption=text, parse_mode="HTML", reply_markup=kb)
                    if sent and getattr(sent, "message_id", None):
                        threading.Thread(
                            target=lambda: (_ for _ in ()).throw(TypeError("noop")) if False else _delayed_photo_delete(
                                self._master_token or "",
                                q.message.chat_id, sent.message_id),
                            daemon=True).start()
                    import os as _os
                    try:
                        _os.remove(photo_path)
                    except OSError:
                        pass
                    return
                except Exception:
                    pass
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

        async def gen_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            chain = q.data.split(":", 2)[2] if q.data.count(":") >= 2 else "sui"
            wallet = self._generate_user_wallet(bot_id, chain)
            if not wallet:
                await q.message.edit_text("⚠️ Couldn't generate a wallet for this chain. Try again.",
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            text = (ONBOARD["wallet_created"].format(
                chain=_chain_label(chain),
                address=wallet["address"],
                private_key=wallet["private_key"]))
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("🗝️ I've saved my key", callback_data="sb:gen_wallet_done")],
            ])
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            # SECURITY: private key shown once; auto-delete the message in 5 min
            _schedule_msg_delete(self._master_token or "",
                                 q.message.chat_id, q.message.message_id)

        async def gen_wallet_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            await q.message.edit_text("✅ Wallet saved. You can now receive funds.",
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton("📥 Receive", callback_data="sb:receive")],
                                           [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def send_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            text = "📤 Provide the address you're sending to:"
            await q.message.edit_text(text, reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton("❌ Cancel", callback_data="send:cancel")]]))
            return S_ADDR

        async def send_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
            addr = (update.message.text or "").strip()
            if len(addr) < 20:
                await update.message.reply_text("❌ Invalid address. Paste the full address:")
                return S_ADDR
            context.bot_data["send_dest"] = addr
            await update.message.reply_text("Amount to send (USDC):",
                                            reply_markup=telegram.ReplyKeyboardRemove())
            return S_AMOUNT

        async def send_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
            raw = (update.message.text or "").strip().replace(",", "")
            try:
                amount = float(raw)
            except ValueError:
                await update.message.reply_text("❌ Invalid amount. Enter a number:")
                return S_AMOUNT
            if amount <= 0:
                await update.message.reply_text("❌ Amount must be positive:")
                return S_AMOUNT
            dest = context.bot_data.get("send_dest", "")
            context.bot_data.pop("send_dest", None)
            b = self.registry.get_bot(bot_id)
            chain = (b or {}).get("chain") or "sui"

            # Execute the real on-chain transfer via the wallet key.
            warning = ""
            try:
                result = self._sui_transfer(bot_id, chain, dest, amount)
                if result.get("ok"):
                    digest = result.get("digest", "?")
                    text = (f"✅ <b>Sent ${amount:,.4f} USDC</b>\n"
                            f"to <code>{_esc(dest)}</code>\n\n"
                            f"Tx: <code>{_esc(digest[:20])}…</code>")
                else:
                    err = result.get("error", "unknown error")
                    warning = f"\n\n⚠️ <b>Transfer failed</b>: {_esc(err[:120])}"
                    el = err.lower()
                    if "insufficient" in el or "no usdc" in el:
                        # Distinguish USDC vs gas: check if SUI is mentioned or budget
                        if "gas" in el or "budget" in el or "sui" in el:
                            text = (f"❌ <b>Transfer failed — not enough SUI for gas</b>\n\n"
                                    f"Your wallet has USDC but needs a little SUI to pay gas. "
                                    f"Keep ~0.02 SUI for fees. {_esc(err[:120])}")
                        else:
                            text = (f"❌ <b>Transfer failed</b>\n\n"
                                    f"Your wallet doesn't have enough USDC. "
                                    f"You have less than ${amount:.2f} USDC. {_esc(err[:120])}")
                    elif "budget" in el or "gas" in el:
                        text = (f"⚠️ <b>Couldn't send — gas budget too high</b>\n\n"
                                f"Sui rejected the gas budget (too high for your 0.06 SUI). "
                                f"Try a smaller amount or add a little SUI. {_esc(err[:120])}")
                    else:
                        text = (f"⚠️ <b>Couldn't send</b> — {_esc(err[:120])}\n\n"
                                f"To execute manually, use your private key to\n"
                                f"sweep the funds in your wallet app.")
            except Exception as exc:
                text = (f"⚠️ <b>Couldn't send</b> — {_esc(str(exc)[:120])}\n\n"
                        f"To execute manually, use your private key to\n"
                        f"sweep the funds in your wallet app.")
                warning = f"\n\nError: {_esc(str(exc)[:120])}"

            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("📤 Send Again", callback_data="sb:send"),
                 telegram.InlineKeyboardButton("🗝️ Private Keys", callback_data="sb:keys")],
                [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")],
            ])
            sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
            if sent and getattr(sent, "message_id", None):
                threading.Thread(
                    target=lambda: (_ for _ in ()).throw(TypeError("noop")) if False else _delayed_photo_delete(
                        self._master_token or "",
                        update.message.chat_id, sent.message_id),
                    daemon=True).start()
            return ConversationHandler.END

        async def send_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            await q.message.edit_text("❌ Canceled.",
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
            return ConversationHandler.END

        async def send_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
            # Legacy entry point - the simplified flow confirms inline in the
            # amount step; nothing to do here.
            q = update.callback_query
            await q.answer()
            return ConversationHandler.END

        async def chain_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            target = q.data.split(":", 2)[2] if q.data.count(":") >= 2 else ""
            if target in ("sui", "solana", "hyperliquid"):
                if target == "sui":
                    self.registry.update_bot(bot_id, chain=target)
                    await q.message.edit_text(f"✅ Trading chain set to {_chain_label(target)}.")
                    await dash(update, context)
                    return
                # BLOCKED until released: cannot switch to a not-yet-live chain.
                text = (
                    f"{_chain_label(target)}\n\n"
                    f"{USERBOT['release_live']}\n\n"
                    f"Only Sui (Aftermath) is live right now. "
                    f"Switch back to Sui to trade."
                )
                await q.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton("⛓ Switch to Sui", callback_data="sb:chain:sui")],
                                               [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash")]]))
                return
            text = ("⛓ <b>Switch trading chain</b>\n\n"
                    "Your orders will execute on this chain.")
            kb = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton("⛓ Sui (live)", callback_data="sb:chain:sui")],
                [telegram.InlineKeyboardButton("⛓ Solana", callback_data="sb:chain:solana")],
                [telegram.InlineKeyboardButton("⛓ Hyperliquid", callback_data="sb:chain:hyperliquid")],
                [telegram.InlineKeyboardButton(BACK, callback_data="sb:dash")],
            ])
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

        async def wallet_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            if not self._exec_ready():
                await q.message.edit_text("🗝️ Execution isn't configured yet - no keys to show.",
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            # show each chain's private key so the owner can export/withdraw
            lines = ["🗝️ <b>Your private keys</b>\n\n"
                     "These keys control the bot's trading wallets. Export them to "
                     "move funds to your own wallet - anyone with these can spend "
                     "the funds, so keep them secret.\n"]
            for chain in self.gateway.adapters:
                try:
                    self.gateway.provision_wallet(bot_id, chain)
                    wallet = self.gateway.ledger.wallet_by_bot_chain(bot_id, chain)
                    if not wallet or not wallet.get("key_enc"):
                        continue
                    key = self.gateway._vault.decrypt(wallet["key_enc"])
                    lines.append(f"\n🔗 {chain.upper()}\n<code>{key}</code>")
                except Exception as exc:
                    lines.append(f"\n🔗 {chain.upper()} - unavailable ({str(exc)[:40]})")
            await q.message.edit_text("\n".join(lines), parse_mode="HTML",
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")],
                                           [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
            # SECURITY: private keys self-destruct after 5 min so they don't
            # persist in Telegram history.
            try:
                sent = await q.message.reply_text(
                    "🔒 This message will auto-delete in 5 minutes. Your private keys "
                    "will not persist in this chat.",
                    parse_mode="HTML")
                if sent and getattr(sent, "message_id", None):
                    threading.Thread(
                        target=lambda: (_ for _ in ()).throw(TypeError("noop")) if False else _delayed_photo_delete(
                            self._master_token or "",
                            q.message.chat_id, sent.message_id),
                        daemon=True).start()
            except Exception:
                pass

        async def wallet_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            if not self._exec_ready():
                await q.message.edit_text("💸 Execution isn't configured yet - nothing to withdraw.",
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            # Manual withdrawal = export key, move funds on-chain yourself.
            text = ("💸 <b>Withdraw funds</b>\n\n"
                    "This bot is non-custodial: only you can move funds out. To "
                    "withdraw, export the private key below and sweep the balance "
                    "to your main wallet in any standard wallet app.\n\n"
                    "⚠️ The trading wallet holds the funds; the bot signs trades "
                    "but has NO withdrawal rights on Hyperliquid (API wallet).")
            await q.message.edit_text(text, parse_mode="HTML",
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton("🗝️ Show Private Keys", callback_data="sb:keys")],
                                           [telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))

        async def wallet_fund(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            _, _, chain = q.data.split(":")
            if not self._exec_ready():
                await q.message.edit_text(USERBOT["kill_no_exec"],
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            wallet = None
            try:
                self.gateway.provision_wallet(bot_id, chain)
                wallet = self.gateway.ledger.wallet_by_bot_chain(bot_id, chain)
            except Exception:
                wallet = None
            if not wallet:
                await q.message.edit_text(USERBOT["wallet_not_connected"].format(name=self.registry.get_bot(bot_id)["bot_name"]),
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            self._exec_path()
            from wallet_ui import CHAIN_LABELS
            text = USERBOT["wallet_fund"].format(
                chain_label=CHAIN_LABELS.get(chain, chain),
                address=wallet["address"])
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")],
                 [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def check_deposits(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            chain = (b or {}).get("chain") or "sui"
            account = {"balances": {}, "positions": [], "wallet_address": ""}
            try:
                account = self._exec_account(bot_id, chain)
            except Exception:
                pass
            bal = account.get("balances") or {}
            usdc = float(bal.get("USDC", 0))
            native = float(bal.get("native", 0))
            addr = account.get("wallet_address") or ""
            # Gateway fully connected -> also run the on-chain deposit scanner.
            found_all = []
            if self._exec_ready():
                await _safe_edit(q, USERBOT["deposit_checking"])
                for ch in self.gateway.adapters:
                    try:
                        found = self.gateway.scan_deposits(bot_id, ch) or []
                        for ev in found:
                            ev["chain"] = ch
                        found_all.extend(found)
                    except Exception:
                        continue
            if found_all:
                self._exec_path()
                from wallet_ui import CHAIN_LABELS
                lines = [USERBOT["deposit_found"].format(
                    chain_label=CHAIN_LABELS.get(ev.get("chain"), ev.get("chain")),
                    amount=float(ev.get("amount") or 0),
                    asset=ev.get("asset", "")) for ev in found_all]
                await _safe_edit(
                    q,
                    "\n\n".join(lines) + "\n\n▶️ Enable your agent to start trading.",
                    reply_markup=telegram.InlineKeyboardMarkup(
                        [[telegram.InlineKeyboardButton("▶️ Enable Agent", callback_data="sb:enable_agent")],
                         [telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            if addr and (usdc > 0 or native > 0):
                # RPC-confirmed balance (works even without bonded gateway keys).
                await _safe_edit(
                    q,
                    f"💰 <b>Balance confirmed on {_chain_label(chain)}</b>\n"
                    f"  USDC  <code>${usdc:,.2f}</code>\n"
                    f"  native {native:,.4f}\n\n"
                    f"<code>{_esc(addr)}</code>\n\n"
                    f"Your wallet is ready. Enable the agent to start trading.",
                    reply_markup=telegram.InlineKeyboardMarkup(
                        [[telegram.InlineKeyboardButton("▶️ Enable Agent", callback_data="sb:enable_agent")],
                         [telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))
                return
            await _safe_edit(
                q,
                USERBOT["deposit_none"],
                reply_markup=telegram.InlineKeyboardMarkup(
                    [[telegram.InlineKeyboardButton("🔍 Check Again", callback_data="sb:check_deposits")],
                     [telegram.InlineKeyboardButton(BACK, callback_data="sb:wallet")]]))

        async def enable_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            if q.data == "sb:enable_agent_yes":
                if not self.registry.get_active_key(tg_id):
                    await q.message.edit_text(USERBOT["enable_agent_no_key"],
                                              reply_markup=telegram.InlineKeyboardMarkup(
                                                  [[telegram.InlineKeyboardButton("🔑 Set AI Key", callback_data="key:start")]]))
                    return
                self.registry.update_bot(bot_id, paused=0)
                if self.agent_pool:
                    try:
                        self.agent_pool.start(bot_id)
                    except Exception:
                        pass
                if self.gateway:
                    try:
                        self.gateway.provision_all_wallets(bot_id)
                    except Exception:
                        pass
                await q.message.edit_text(USERBOT["enable_agent_ok"],
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton("📊 Dashboard", callback_data="sb:dash")]]))
                return
            await q.message.edit_text(USERBOT["enable_agent_prompt"],
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton("▶️ Yes, enable across all chains", callback_data="sb:enable_agent_yes")],
                                           [telegram.InlineKeyboardButton("↩️ Not yet", callback_data="sb:wallet")]]))

        async def killswitch_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            if q.data == "sb:kill_yes":
                if not self._exec_ready():
                    await q.message.edit_text(USERBOT["kill_no_exec"],
                                              reply_markup=telegram.InlineKeyboardMarkup(
                                                  [[telegram.InlineKeyboardButton(BACK, callback_data="sb:dash")]]))
                    return
                try:
                    res = self.gateway.engage_killswitch(bot_id, "user requested via Telegram")
                except Exception as exc:
                    res = {"ok": False, "error": str(exc)[:200]}
                summary = f"Fully flattened: {'YES' if res.get('fully_flattened') else 'NO - see errors below'}"
                errors = []
                for chain, r in (res.get("results") or {}).items():
                    if isinstance(r, dict) and not r.get("ok"):
                        errors.append(f"• {chain}: {r.get('error', 'failed')}")
                if errors:
                    summary += "\n" + "\n".join(errors)
                await q.message.edit_text(
                    USERBOT["kill_engaged"].format(summary=summary),
                    reply_markup=telegram.InlineKeyboardMarkup(
                        [[telegram.InlineKeyboardButton("✅ Release", callback_data="sb:kill_release")],
                         [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            if q.data == "sb:kill_release":
                try:
                    if self._exec_ready():
                        self.gateway.release_killswitch(bot_id)
                except Exception:
                    pass
                await q.message.edit_text(USERBOT["kill_released"],
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            if not self._exec_ready():
                await q.message.edit_text(USERBOT["kill_no_exec"],
                                          reply_markup=telegram.InlineKeyboardMarkup(
                                              [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            await q.message.edit_text(USERBOT["kill_title"],
                                      reply_markup=telegram.InlineKeyboardMarkup(
                                          [[telegram.InlineKeyboardButton("🛑 Engage Kill-Switch", callback_data="sb:kill_yes")],
                                           [telegram.InlineKeyboardButton("↩️ Cancel", callback_data="sb:dash")]]))

        async def close_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Take-profit / manual close of one position (from a P&L alert button)."""
            q = update.callback_query
            await q.answer()
            _, _, symbol = q.data.split(":", 2)
            if q.data == f"sb:close_yes:{symbol}":
                chain = (self.registry.get_bot(bot_id) or {}).get("chain") or "sui"
                if self._exec_ready():
                    # Real execution: close on Aftermath via the gateway.
                    res = self._real_close_position(bot_id, chain, symbol)
                    if res.get("ok"):
                        await q.message.edit_text(
                            f"✅ <b>Closed {symbol} on-chain</b>\n"
                            f"• Reduce-only market order sent\n"
                            f"• Qty: {res.get('qty', '?'):g}",
                            parse_mode="HTML",
                            reply_markup=telegram.InlineKeyboardMarkup(
                                [[telegram.InlineKeyboardButton("📊 Dashboard", callback_data="sb:dash")]]))
                    else:
                        await q.message.edit_text(
                            f"⚠️ <b>Couldn't close {symbol}</b>\n{res.get('error', '?')[:120]}",
                            parse_mode="HTML",
                            reply_markup=telegram.InlineKeyboardMarkup(
                                [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                    return
                try:
                    pf = self.platform.positions(platform_token)
                    pos = next((p for p in pf.get("positions", []) if p["symbol"] == symbol), None)
                    if not pos:
                        await q.message.edit_text(f"ℹ️ No open {symbol} position.")
                        return
                    action = "sell" if pos["quantity"] > 0 else "cover"
                    r = self.platform.trade(platform_token, pos["market"], symbol, action,
                                            abs(pos["quantity"]))
                    pnl = (pos.get("current_price") or pos["entry_price"]) - pos["entry_price"]
                    pnl = pnl * pos["quantity"] if pos["quantity"] >= 0 else pnl * -pos["quantity"]
                    await q.message.edit_text(
                        f"✅ <b>{'TAKE PROFIT' if pnl >= 0 else 'Closed'}: {symbol}</b>\n"
                        f"• Realized P&L: <b>${pnl:+,.2f}</b>\n"
                        f"• Exit: ${pos.get('current_price') or pos['entry_price']:,.4f}",
                        parse_mode="HTML",
                        reply_markup=telegram.InlineKeyboardMarkup(
                            [[telegram.InlineKeyboardButton("📊 Dashboard", callback_data="sb:dash")]]))
                except Exception as exc:
                    await q.message.edit_text(f"⚠️ Couldn't close {symbol}: {str(exc)[:120]}",
                                              reply_markup=telegram.InlineKeyboardMarkup(
                                                  [[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                return
            # confirmation
            try:
                pf = self.platform.positions(platform_token)
                pos = next((p for p in pf.get("positions", []) if p["symbol"] == symbol), None)
                if not pos:
                    await q.message.edit_text(f"ℹ️ No open {symbol} position.")
                    return
                qty = pos["quantity"]
                entry = pos["entry_price"]
                cur = pos.get("current_price") or entry
                pnl = (cur - entry) * (qty if qty >= 0 else -qty)
                pct = (cur / entry - 1) * 100 * (1 if qty >= 0 else -1)
            except Exception:
                pnl = pct = 0.0
            await q.message.edit_text(
                f"💰 <b>Close {symbol}?</b>\n\n"
                f"Current P&L: <b>${pnl:+,.2f}</b> ({pct:+.2f}%)\n\n"
                f"Take the profit / cut the loss now?",
                parse_mode="HTML",
                reply_markup=telegram.InlineKeyboardMarkup(
                    [[telegram.InlineKeyboardButton("✅ Yes, close", callback_data=f"sb:close_yes:{symbol}")],
                     [telegram.InlineKeyboardButton("🐾 Keep open", callback_data=f"sb:keep:{symbol}")]]))

        async def keep_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """User chose to keep a position open - acknowledge with live P&L %."""
            q = update.callback_query
            await q.answer()
            _, _, symbol = q.data.split(":", 2)
            pnl = pct = 0.0
            try:
                pf = self.platform.positions(platform_token)
                pos = next((p for p in pf.get("positions", []) if p["symbol"] == symbol), None)
                if pos:
                    qty = pos["quantity"]
                    entry = pos["entry_price"]
                    cur = pos.get("current_price") or entry
                    pnl = (cur - entry) * (qty if qty >= 0 else -qty)
                    pct = (cur / entry - 1) * 100 * (1 if qty >= 0 else -1)
            except Exception:
                pass
            # cat persona, honest about direction
            if pct < 0:
                mood = (f"📉 <b>Keeping {symbol} open.</b>\n\n"
                        f"It's down <b>{pct:+.2f}%</b> (${pnl:+,.2f}) right now.\n"
                        "The cat is watching it closely - the stop-loss is still "
                        "on guard, so the damage stays capped. 🐾")
            elif pct > 0:
                mood = (f"📈 <b>Keeping {symbol} open.</b>\n\n"
                        f"It's up <b>{pct:+.2f}%</b> (${pnl:+,.2f}) right now.\n"
                        "The cat says: let the winner run, but we'll grab it if "
                        "it slips. 🐾")
            else:
                mood = (f"🐾 <b>Keeping {symbol} open.</b>\n\n"
                        "Flat right now. The cat is waiting for a move.")
            await q.message.edit_text(
                mood + "\n\nEvery trade gets pushed here. Tap 💼 Wallet to watch it live.",
                parse_mode="HTML",
                reply_markup=telegram.InlineKeyboardMarkup(
                    [[telegram.InlineKeyboardButton("✅ Take Profit", callback_data=f"sb:close:{symbol}")],
                     [telegram.InlineKeyboardButton("📊 Dashboard", callback_data="sb:dash")]]))

        async def trade_take(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """User manually takes Neko's pending decision — executes against real USDC."""
            q = update.callback_query
            await q.answer()
            try:
                symbol = q.data.split(":", 2)[2].upper()
            except Exception:
                await q.message.edit_text("❓ Invalid trade symbol.", reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(BACK, callback_data="sb:peek")]]))
                return
            # Load pending decision from per-bot cache
            pending = None
            try:
                from pathlib import Path
                import json as _json
                cache_path = Path(__file__).resolve().parents[2] / "research" / "exports" / f"live_agent_cache_bot{bot_id}.json"
                if cache_path.exists():
                    cache = _json.loads(cache_path.read_text(encoding="utf-8"))
                    pending = cache.get(symbol) or cache.get(symbol.upper()) or cache.get(symbol.lower())
                    # fallback: search by symbol field
                    if not pending:
                        for _, row in (cache or {}).items():
                            if (row.get("symbol") or "").upper() == symbol:
                                pending = row
                                break
            except Exception:
                pending = None
            if not pending or (pending.get("action") or "").lower() not in ("buy", "short"):
                await q.message.edit_text(f"ℹ️ No pending {symbol} decision to take (maybe already held or expired).",
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("👀 Peek", callback_data="sb:peek")]]))
                return
            b = self.registry.get_bot(bot_id) or {}
            mode = (b.get("trading_mode") or "paper").lower()
            if mode != "paper" and not self._exec_ready():
                await q.message.edit_text("⚠️ Real execution not ready — no trading keys configured.",
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(BACK, callback_data="sb:dash")]]))
                return
            try:
                chain = (b.get("chain") or "sui")
                # Fresh price: platform -> Aftermath orderbook -> decision price
                ref_price = 0.0
                try:
                    ref_price = float(self.platform.price(self.registry.platform_token(bot_id) or "", "crypto", symbol) or 0)
                except Exception:
                    pass
                if ref_price <= 0:
                    try:
                        ref_price = float((await asyncio.get_running_loop().run_in_executor(
                            None, self._paper_mark_prices, [symbol],
                            (b.get("network") or "mainnet"))).get(symbol) or 0)
                    except Exception:
                        pass
                if ref_price <= 0:
                    ref_price = float(pending.get("price") or 0)
                if ref_price <= 0:
                    await q.message.edit_text(f"⚠️ No price for {symbol} — try again in a moment.",
                                              reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("👀 Peek", callback_data="sb:peek")]]))
                    return
                direction = (pending.get("direction") or "").lower()
                stop_pct = float(pending.get("stop_pct") or pending.get("stop_loss_pct") or 5.0)
                take_pct = float(pending.get("take_pct") or pending.get("take_profit_pct") or 8.0)
                lev = float(pending.get("leverage") or pending.get("lev") or 20.0)
                stop_pct = max(2.0, min(stop_pct, 8.0))
                take_pct = max(4.0, min(take_pct, 24.0))
                if mode == "paper":
                    # PAPER TAKE: virtual fill at the live Aftermath price with
                    # the real cost model, sized off the paper portfolio.
                    pf = self._paper_store().ensure_portfolio(bot_id)
                    cash = float(pf.get("cash") or 0.0)
                    qty = (cash * 0.15) / ref_price if ref_price > 0 and cash > 0 else 0.0
                    if qty <= 0:
                        await q.message.edit_text("⚠️ No paper cash available. Reset the paper portfolio in Settings.",
                                                  reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                        return
                    lev = max(1.0, min(lev, 20.0))
                    fill = self._paper_gateway().open(
                        bot_id, symbol, "long" if direction != "short" else "short",
                        qty, ref_price, leverage=lev,
                        stop_loss=round(ref_price * (1 - stop_pct / 100) if direction != "short" else ref_price * (1 + stop_pct / 100), 6),
                        take_profit=round(ref_price * (1 + take_pct / 100) if direction != "short" else ref_price * (1 - take_pct / 100), 6),
                        idempotency_key=f"manual-take-paper:{bot_id}:{symbol}:{int(time.time() * 1000)}")
                    if not fill.get("ok"):
                        await q.message.edit_text(f"⚠️ Paper take failed: {fill.get('error', 'unknown')[:200]}",
                                                  reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("👀 Peek", callback_data="sb:peek")]]))
                        return
                    await q.message.edit_text(
                        f"🧪 <b>Paper took {symbol} {direction.upper()}</b>\n"
                        f"Filled <code>{qty:.6f}</code> @ <code>${fill.get('fill_price', ref_price):,.4f}</code> · {lev:g}x\n"
                        f"⛔ Stop {stop_pct:.1f}% · 🎯 Take {take_pct:.1f}%\n"
                        f"Virtual — no real money moved.",
                        parse_mode="HTML",
                        reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("💰 Active Positions", callback_data="sb:pos")], [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                    return
                # Recompute size against REAL equity (wallet USDC + collateral) so $0.01 test balance works
                qty = float(pending.get("quantity") or pending.get("qty") or 0)
                # If qty is 0 (was sized at $0 equity), recompute from real equity now
                if qty <= 0 or qty < 1e-8:
                    try:
                        snap = self._equity_snapshot(b)
                        real_eq = float(snap.get("equity") or snap.get("usdc") or 0.0)
                        # also try gateway ledger cash
                        if real_eq <= 0:
                            try:
                                self._exec_path()
                                from ledger import ExecLedger
                                import os as _os
                                _ledger = ExecLedger(_os.environ.get("EXEC_LEDGER_PATH", "exec_ledger.db"))
                                _w = _ledger.wallet_by_bot_chain(bot_id, chain)
                                if _w:
                                    _st = _ledger.load_chain_state(_w["id"]) or {}
                                    _bal = _st.get("balances") or {}
                                    real_eq = float(_bal.get("USDC") or _bal.get("USD") or 0.0)
                                _ledger.close()
                            except Exception:
                                pass
                        if real_eq > 0:
                            # Use same conviction sizing as live_agent: 45% cap, 20x floor
                            bal = real_eq
                            expo = 0.15  # conservative for manual take
                            notional = bal * expo
                            qty = notional / ref_price if ref_price > 0 else 0
                    except Exception:
                        pass
                if qty <= 0:
                    await q.message.edit_text(f"⚠️ Couldn't size {symbol} — no equity. Deposit USDC first.",
                                              reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("📥 Receive", callback_data="sb:receive")]]))
                    return
                # Build and route real order via gateway
                await q.message.edit_text(f"⏳ Taking <b>{symbol} {pending.get('direction','').upper() or pending.get('action','').upper()}</b> @ ${ref_price:,.2f} — routing to real USDC…", parse_mode="HTML")
                self._exec_path()
                from order_model import OrderIntent
                action = "buy" if (pending.get("direction","").lower() == "long" or pending.get("action","").lower() == "buy") else "sell" if (pending.get("direction","").lower() == "long") else "buy"
                # Map direction to side: long->buy, short->sell (open)
                direction = (pending.get("direction") or "").lower()
                side = "buy" if direction == "long" else "sell" if direction == "short" else ("buy" if (pending.get("action") or "").lower() == "buy" else "sell")
                # For perp, venue is aftermath-perp on sui
                chain = (b.get("chain") or "sui")
                venue = "aftermath-perp" if chain == "sui" else "hl-perp"
                intent = OrderIntent(
                    chain=chain, venue=venue, symbol=symbol,
                    side=side, qty=float(qty), order_type="limit",
                    limit_price=round(ref_price * (0.9998 if side == "buy" else 1.0002), 6),
                    leverage=float(lev) if pending.get("action") in ("buy","short") or direction in ("long","short") else 1.0,
                    idempotency_key=f"manual-take:{bot_id}:{symbol}:{int(time.time()*1000)}",
                )
                if stop_pct:
                    intent.stop_loss = round(ref_price * (1 - stop_pct/100) if side == "buy" else ref_price * (1 + stop_pct/100), 6)
                if take_pct:
                    intent.take_profit = round(ref_price * (1 + take_pct/100) if side == "buy" else ref_price * (1 - take_pct/100), 6)
                res = self.gateway.route_and_sync(bot_id, intent, ref_price)
                if res.get("ok"):
                    await q.message.edit_text(f"✅ <b>Took {symbol} {direction.upper() or side.upper()}</b>\n"
                                              f"🚦 LIMIT <code>{qty:.6f}</code> @ <code>${ref_price:,.4f}</code> · {lev:g}x\n"
                                              f"⛔ Stop {stop_pct:.1f}% · 🎯 Take {take_pct:.1f}%\n"
                                              f"Order #{res.get('order_id')} — real USDC on {chain}",
                                              parse_mode="HTML",
                                              reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("📊 P&L", callback_data="sb:pnl")],[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))
                else:
                    await q.message.edit_text(f"⚠️ <b>Couldn't take {symbol}</b>\n{res.get('error','unknown')[:200]}",
                                              parse_mode="HTML",
                                              reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("👀 Peek", callback_data="sb:peek")]]))
            except Exception as exc:
                await q.message.edit_text(f"⚠️ Take failed: {str(exc)[:200]}",
                                          reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("👀 Peek", callback_data="sb:peek")]]))

        async def trade_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            try:
                symbol = q.data.split(":", 2)[2].upper()
            except Exception:
                symbol = "trade"
            await q.message.edit_text(f"❌ Rejected <b>{symbol}</b> — Neko will look for the next setup.",
                                      parse_mode="HTML",
                                      reply_markup=telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton("👀 Peek", callback_data="sb:peek")],[telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        async def exec_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
            q = update.callback_query
            await q.answer()
            b = self.registry.get_bot(bot_id)
            if not self._exec_ready():
                text = USERBOT["exec_risk_disabled"].format(name=b["bot_name"])
            else:
                text = USERBOT["exec_risk"].format(name=b["bot_name"],
                                                   lines="\n".join(self._exec_risk_lines()))
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=telegram.InlineKeyboardMarkup(
                [[telegram.InlineKeyboardButton(BACK, callback_data="sb:settings")],
                 [telegram.InlineKeyboardButton(HOME, callback_data="sb:dash")]]))

        app.add_handler(CommandHandler("start", welcome))
        app.add_handler(CallbackQueryHandler(key_help, pattern=r"^key:help$"))
        app.add_handler(key_conv)
        # onboarding: how Neko trades -> trader type -> chain -> wallet backup
        app.add_handler(CallbackQueryHandler(onboarding_intro, pattern=r"^ob:intro$"))
        app.add_handler(CallbackQueryHandler(onboarding_trader, pattern=r"^ob:trader"))
        app.add_handler(CallbackQueryHandler(onboarding_chain, pattern=r"^ob:chain(?::|$)"))
        app.add_handler(CallbackQueryHandler(onboarding_chain_confirm, pattern=r"^ob:chain_confirm:"))
        app.add_handler(CallbackQueryHandler(onboarding_key_saved, pattern=r"^ob:key_saved$"))
        app.add_handler(CallbackQueryHandler(dash, pattern=r"^sb:dash$"))
        app.add_handler(CallbackQueryHandler(start_agent, pattern=r"^sb:start_agent$"))
        app.add_handler(CallbackQueryHandler(peek, pattern=r"^sb:peek$"))
        app.add_handler(CallbackQueryHandler(pnl_detail, pattern=r"^sb:pnl$"))
        app.add_handler(CallbackQueryHandler(positions, pattern=r"^sb:pos$"))
        app.add_handler(CallbackQueryHandler(paper_close, pattern=r"^sb:pclose(_yes)?:[A-Z0-9]+$"))
        app.add_handler(CallbackQueryHandler(live_markets, pattern=r"^sb:live$"))
        app.add_handler(CallbackQueryHandler(stocks, pattern=r"^sb:stocks$"))
        app.add_handler(CallbackQueryHandler(trades, pattern=r"^sb:trades$"))
        app.add_handler(CallbackQueryHandler(leaderboard, pattern=r"^sb:lb$"))
        app.add_handler(CallbackQueryHandler(bot_controls, pattern=r"^sb:(pause|resume|pause_yes|delete|delete_yes|mode_live|mode_paper|mode_live_yes|paper_reset)$"))
        app.add_handler(CallbackQueryHandler(settings, pattern=r"^sb:(settings|set_interval:\d+|set_trader_type:\w+|set_leverage:\d+|set_network:\w+)$"))
        app.add_handler(CallbackQueryHandler(inbox, pattern=r"^sb:inbox$"))
        app.add_handler(CallbackQueryHandler(help_screen, pattern=r"^sb:help$"))
        app.add_handler(CallbackQueryHandler(wallet_screen, pattern=r"^sb:wallet$"))
        app.add_handler(CallbackQueryHandler(receive, pattern=r"^sb:receive$"))
        app.add_handler(CallbackQueryHandler(gen_wallet, pattern=r"^sb:gen_wallet:"))
        app.add_handler(CallbackQueryHandler(gen_wallet_done, pattern=r"^sb:gen_wallet_done$"))
        send_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(send_start, pattern=r"^sb:send$")],
            states={
                S_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_addr)],
                S_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_amount)],
            },
            fallbacks=[CallbackQueryHandler(send_cancel, pattern=r"^send:cancel$")],
            name="userbot_send",
            allow_reentry=True,
        )

        app.add_handler(CallbackQueryHandler(send_confirm, pattern=r"^send:confirm$"))

        app.add_handler(send_conv)
        app.add_handler(CallbackQueryHandler(chain_switch, pattern=r"^sb:chain"))
        app.add_handler(CallbackQueryHandler(watchlist, pattern=r"^sb:watchlist$"))
        app.add_handler(CallbackQueryHandler(watch_confirm, pattern=r"^watch:(yes|no):[A-Z0-9]+$"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_command))
        app.add_handler(CallbackQueryHandler(support, pattern=r"^sb:support$"))
        app.add_handler(CallbackQueryHandler(wallet_fund, pattern=r"^sb:fund:\w+$"))
        app.add_handler(CallbackQueryHandler(check_deposits, pattern=r"^sb:check_deposits$"))
        app.add_handler(CallbackQueryHandler(enable_agent, pattern=r"^sb:enable_agent(_yes)?$"))
        app.add_handler(CallbackQueryHandler(wallet_keys, pattern=r"^sb:keys$"))
        app.add_handler(CallbackQueryHandler(wallet_withdraw, pattern=r"^sb:withdraw$"))
        app.add_handler(CallbackQueryHandler(killswitch_screen, pattern=r"^sb:(kill|kill_yes|kill_release)$"))
        app.add_handler(CallbackQueryHandler(exec_risk, pattern=r"^sb:execrisk$"))
        app.add_handler(CallbackQueryHandler(close_position, pattern=r"^sb:(close|close_yes):\w+$"))
        app.add_handler(CallbackQueryHandler(keep_open, pattern=r"^sb:keep:\w+$"))
        app.add_handler(CallbackQueryHandler(trade_take, pattern=r"^trade:take:"))
        app.add_handler(CallbackQueryHandler(trade_reject, pattern=r"^trade:reject:"))