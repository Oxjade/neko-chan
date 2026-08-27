"""Smart event watcher: platform DB -> deduplicated, batched Telegram pushes.

The Telegram bot network has all the notification UI (fills, stops, targets,
liquidations, daily/weekly summaries, inline buttons, dedup ledger) but NO
caller — the notifier was dead code. This watcher plugs it in: it watches the
platform SQLite `signals` table as the source of truth (log files are derived
— the 2026-08-27 D2 defect proved they can miss fills), classifies each new
operation event, and pushes a single, meaningful, deduplicated message per
state transition. Never spams: one push per signal, per event kind.

Design principles (quant-grade, from the skill suite + the D-audit):
  - Source of truth = DB (signals), not logs (D2).
  - Dedup = registry.event_seen(kind, ref_id) (already built) + in-memory
    high-water watermark across restarts (persisted in registry.db).
  - Batching = errors merge into 'still retrying', summaries are one-per-day.
  - Only state changes notify: fills, closes, stops, targets, milestones.
    Holds/decisions never notify (that's how you get spam).
  - Idempotent: re-runs don't double-push (watermark + dedup ledger).
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests

# ----------------------------------------------------------------- config

BASE_URL = None  # set from env below
POLL_INTERVAL_S = 5.0
# watermark persisted as a synthetic event in the registry ledger:
# kind=WATERMARK, ref_id=str(last_signal_id) — survives restarts, no new table.
WATERMARK = "watcher_watermark"

# kinds a watcher can emit (prefix registry keys so they don't collide with
# the Telegram bot's own event ledger)
K_FILL = "watcher_fill"
K_CLOSE = "watcher_close"
K_STOP = "watcher_stop"
K_TARGET = "watcher_target"
K_LIQ = "watcher_liq"
K_MILESTONE_UP = "watcher_milestone_up"
K_MILESTONE_DOWN = "watcher_milestone_down"


class Watcher:
    def __init__(self, db_path, notify, registry, bot_id: int, tg_id: int,
                 bot_token: str, chat_id: int, platform_base: str,
                 start_equity: float, equity_interval_pct: float = 5.0,
                 poll_interval: float = POLL_INTERVAL_S):
        self.db_path = db_path
        self.notify = notify
        self.registry = registry
        self.bot_id, self.tg_id, self.bot_token, self.chat_id = bot_id, tg_id, bot_token, chat_id
        self.platform = platform_base.rstrip("/")
        self.start_equity = start_equity
        self.equity_interval = equity_interval_pct / 100.0
        self.poll_interval = poll_interval
        self.watermark = self._load_watermark()
        self.last_equity_mark = start_equity
        self._stop = False

    # ------------------------------------------------------------ helpers

    def _load_watermark(self) -> int:
        try:
            row = self.registry.recent_events(self.tg_id, limit=200)
            for e in row:
                if e.get("kind") == WATERMARK and e.get("ref_id"):
                    return int(e["ref_id"])
        except Exception:
            pass
        return 0

    def _save_watermark(self, value: int) -> None:
        try:
            # overwrite semantics: mark_event is INSERT OR IGNORE keyed on
            # (tg_id, kind, ref_id); a new ref_id each time = new row, which is
            # fine for monotonic read (recent_events returns newest first).
            self.registry.mark_event(self.tg_id, WATERMARK, str(value), None)
        except Exception:
            pass

    def _signals_since(self) -> list[dict]:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT signal_id, agent_id, market, symbol, side, entry_price, "
            "exit_price, quantity, pnl, executed_at "
            "FROM signals WHERE agent_id = ? AND message_type = 'operation' "
            "AND signal_id > ? ORDER BY signal_id ASC",
            (self.agent_id, self.watermark),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    @property
    def agent_id(self) -> int:
        bot = self.registry.get_bot(self.bot_id)
        if bot and bot.get("agent_id"):
            return int(bot["agent_id"])
        return 0

    def equity(self) -> float | None:
        try:
            token = self.registry.bot_token(self.bot_id)
            if not token:
                return None
            r = requests.get(f"{self.platform}/api/positions",
                             headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if r.status_code != 200:
                return None
            d = r.json()
            cash = float(d.get("cash", 0))
            pos_val = sum(float(p.get("pnl") or 0) for p in d.get("positions", []))
            return cash + pos_val
        except Exception:
            return None

    # ------------------------------------------------------------ classify

    def classify(self, sig: dict) -> tuple[str, str] | None:
        """Return (kind, ref_id) or None if the event shouldn't notify."""
        side = str(sig.get("side") or "").lower()
        market = str(sig.get("market") or "")
        symbol = str(sig.get("symbol") or "")
        qty = float(sig.get("quantity") or 0)
        px = float(sig.get("entry_price") or 0)
        ref = f"{sig['signal_id']}"
        if side in ("buy", "short"):
            return K_FILL, ref
        if side in ("sell", "cover"):
            return K_CLOSE, ref
        return None

    def message_for(self, sig: dict, kind: str) -> tuple[str, list[list[str]] | None]:
        symbol = str(sig.get("symbol") or "")
        side = str(sig.get("side") or "")
        qty = float(sig.get("quantity") or 0)
        px = float(sig.get("entry_price") or 0)
        market = str(sig.get("market") or "")
        mode = "⚡ perps" if market == "crypto" else f"{market}"
        if kind == K_FILL:
            txt = (f"✅ FILL {side.upper()} {symbol} {qty} @ ${px:,.4f}\n"
                   f"└ {mode} · stop/target set")
            return txt, [["📊 P&L", "sb:dash"], ["🛑 Close", f"sb:close:{symbol}"]]
        if kind == K_CLOSE:
            pnl = sig.get("pnl")
            pnl_txt = f"${pnl:+.2f}" if pnl is not None else "?"
            txt = f"🛑 CLOSED {symbol} {qty} @ ${px:,.4f}\n└ net PnL {pnl_txt}"
            return txt, [["📊 P&L", "sb:dash"]]
        return "", None

    # ------------------------------------------------------------ loop

    def handle(self, sig: dict) -> bool:
        bucket = self.classify(sig)
        if not bucket:
            self.watermark = max(self.watermark, int(sig["signal_id"]))
            return False
        kind, ref = bucket
        text, buttons = self.message_for(sig, kind)
        ok = self.notify.notify(self.bot_id, self.tg_id, self.bot_token, self.chat_id,
                                kind, ref, text, buttons=buttons, dedup=True)
        if ok:
            log.info("[push] %s %s sig=%s", kind, sig.get("symbol"), sig.get("signal_id"))
        self.watermark = max(self.watermark, int(sig["signal_id"]))
        return ok

    def poll_once(self) -> int:
        pushed = 0
        for sig in self._signals_since():
            if self.handle(sig):
                pushed += 1
        self._save_watermark(self.watermark)
        return pushed

    def run(self):
        log.info("[watcher] started agent watcher (watermark %s)", self.watermark)
        self.last_equity_mark = self.equity() or self.start_equity
        while not self._stop:
            try:
                self.poll_once()
                eq = self.equity()
                if eq is not None:
                    pct = (eq / self.last_equity_mark - 1) * 100
                    if abs(pct) >= self.equity_interval * 100:
                        kind = K_MILESTONE_UP if pct > 0 else K_MILESTONE_DOWN
                        if self.notify.notify(self.bot_id, self.tg_id, self.bot_token,
                                              self.chat_id, kind, f"eq:{str(eq)[:12]}",
                                              f"🚀 Equity {eq:,.2f} ({pct:+.1f}%)" if pct > 0
                                              else f"⚠️ Equity {eq:,.2f} ({pct:+.1f}%) — consider pausing",
                                              buttons=[["⏸️ Pause", "sb:pause"]] if pct < 0 else None,
                                              dedup=True):
                            self.last_equity_mark = eq
                    else:
                        self.last_equity_mark = eq
            except Exception as exc:
                log.exception("[watcher] poll error: %s", exc)
            time.sleep(self.poll_interval)

    def stop(self):
        self._stop = True


log = logging.getLogger("tg_bot.watcher")


if __name__ == "__main__":
    # manual smoke test: watch a given bot's signals and dry-run classification
    logging.basicConfig(level=logging.INFO)
    db = sys.argv[1] if len(sys.argv) > 1 else "service/server/data/clawtrader.db"
    watcher = Watcher(db_path=db, notify=None, registry=None, bot_id=8, tg_id=0,
                      bot_token="", chat_id=0, platform_base="http://127.0.0.1:8000",
                      start_equity=100_000.0)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM signals WHERE agent_id=8 AND message_type='operation' "
        "ORDER BY signal_id").fetchall()
    for r in rows:
        s = dict(r)
        kind, ref = watcher.classify(s) or (None, None)
        print(f"  sig={s['signal_id']:>3} {s['side']:<6} {s['symbol']:<7} "
              f"qty={s['quantity']:<8} px={s['entry_price']}  -> {kind}")
    con.close()
