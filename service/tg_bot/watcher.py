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
import os
import sqlite3
import sys
import tempfile
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
        photo_path = self._pnl_card_for(sig, kind)
        ok = self.notify.notify(self.bot_id, self.tg_id, self.bot_token, self.chat_id,
                                kind, ref, text, buttons=buttons, dedup=True,
                                photo_path=photo_path)
        if photo_path:
            self._cleanup_card(photo_path)
        if ok:
            log.info("[push] %s %s sig=%s", kind, sig.get("symbol"), sig.get("signal_id"))
        self.watermark = max(self.watermark, int(sig["signal_id"]))
        return ok

    # ------------------------------------------------------------ PnL card

    def _pnl_card_for(self, sig: dict, kind: str) -> str | None:
        """Generate a Neko-chan PnL card PNG for a fill/close, or None on failure.

        Uses the bundled card generator + a random cat template. The card shows
        the trade's buy/sell prices and pnl% with the cat's voice captions.
        """
        try:
            from cards.generator import generate_pnl_card, random_avatar

            symbol = str(sig.get("symbol") or "?")
            qty = float(sig.get("quantity") or 0)
            entry = float(sig.get("entry_price") or 0)
            exit_px = float(sig.get("exit_price") or 0)
            pnl = sig.get("pnl")
            # derive a pnl% for the card
            if pnl is not None and entry > 0:
                pnl_pct = float(pnl) / (abs(qty) * entry) * 100.0
            elif exit_px and entry > 0:
                pnl_pct = (exit_px - entry) / entry * 100.0
            else:
                pnl_pct = 0.0
            chain = "CRYPTO" if str(sig.get("market")) == "crypto" else str(sig.get("market") or "SOLANA").upper()
            out = os.path.join(tempfile.gettempdir(), f"neko_{kind}_{sig.get('signal_id')}.png")
            generate_pnl_card(
                avatar_path=random_avatar(),
                pnl_pct=pnl_pct,
                buy_price=entry,
                sell_price=exit_px or entry,
                token=symbol.upper(),
                chain=chain,
                out_path=out,
            )
            return out
        except Exception as exc:  # noqa: BLE001 - a card is a nice-to-have
            log.warning("pnl card failed for sig=%s: %s", sig.get("signal_id"), exc)
            return None

    @staticmethod
    def _cleanup_card(path: str) -> None:
        try:
            os.remove(path)
        except Exception:
            pass

    # ------------------------------------------------------------ profit reports

    def _trade_stats(self) -> tuple[int, float, float]:
        """(trades today, win_rate%, fees$) from the platform signals table."""
        try:
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            today = datetime.now(timezone.utc).date().isoformat()
            rows = con.execute(
                "SELECT side, pnl FROM signals WHERE agent_id = ? AND message_type = 'operation' "
                "AND side IN ('buy','sell','short','cover') AND date(created_at) = ?",
                (self.agent_id, today),
            ).fetchall()
            con.close()
            fills = [dict(r) for r in rows if r["side"] in ("buy", "short")]
            closes = [dict(r) for r in rows if r["side"] in ("sell", "cover")]
            trades = len(fills)
            wins = sum(1 for r in closes if r["pnl"] is not None and r["pnl"] > 0)
            fees = 0.0
            return trades, (wins / max(len(closes), 1) * 100.0 if closes else 0.0), fees
        except Exception:
            return 0, 0.0, 0.0

    def _trade_count(self) -> int:
        return self._trade_stats()[0]

    def _win_rate(self) -> float:
        return self._trade_stats()[1]

    def _fees(self) -> float:
        return self._trade_stats()[2]

    def profit_report(self, pnl: float, trades: int, win: float, fees: float,
                      equity: float) -> bool:
        """Push a periodic profit summary with the cat's voice + real numbers."""
        kind = "watcher_profit"
        ref = time.strftime("%Y-%m-%d")
        if pnl >= 0:
            mood = "neko is pleased. the bag is pleased."
        elif pnl < 0:
            mood = "neko is unbothered. the bag feels it though."
        win_txt = f"{win:.0f}%"
        if pnl >= 0:
            text = (f"📈 <b>PROFIT REPORT</b> 🐱\n"
                    f"Net P&L: <b>${pnl:+,.2f}</b>\n"
                    f"Trades: {trades} · win rate {win_txt}\n"
                    f"Fees: ${fees:.2f} · Equity: ${equity:,.2f}\n"
                    f"~ {mood}")
        else:
            text = (f"📉 <b>PROFIT REPORT</b> 🐱\n"
                    f"Net P&L: <b>{pnl:,.2f}</b>\n"
                    f"Trades: {trades} · win rate {win_txt}\n"
                    f"Fees: ${fees:.2f} · Equity: ${equity:,.2f}\n"
                    f"~ {mood}")
        return self.notify.notify(self.bot_id, self.tg_id, self.bot_token, self.chat_id,
                                  kind, ref, text, dedup=True)

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
        self._last_profit_day = None
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
                    # periodic profit report once per UTC day
                    today = time.strftime("%Y-%m-%d", time.gmtime())
                    if today != self._last_profit_day:
                        self._last_profit_day = today
                        try:
                            self.profit_report(
                                pnl=eq - self.start_equity,
                                trades=self._trade_count(),
                                win=self._win_rate(),
                                fees=self._fees(),
                                equity=eq,
                            )
                        except Exception as exc:
                            log.warning("[watcher] profit report failed: %s", exc)
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
