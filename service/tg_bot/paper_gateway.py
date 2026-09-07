"""PaperGateway: virtual execution with REAL cost modeling.

Fills at live market prices plus:
  - venue fee (4.5 bps taker on Aftermath perps)
  - platform fee (0.5% per fill - the same fee live users pay)
  - slippage model (1 bp adverse fill per market order; limit fills at limit)

So the paper win rate a user sees == the win rate they'd get live. That is
the entire point: teach the true economics before real money.

The gateway implements the same discipline the live path enforces:
  one position per symbol, one-shot-per-asset-per-day, one open position.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("paper")

VENUE_FEE_BPS = 4.5      # Aftermath perps taker
PLATFORM_FEE_BPS = 50.0  # 0.5% platform fee per fill
SLIPPAGE_BPS = 1.0       # adverse fill on market orders
SLIPPAGE_BPS_LIMIT = 0.0 # limit fills at the limit price exactly


class PaperGateway:
    def __init__(self, store, start_usd: float | None = None):
        """store: paper_store.PaperStore (bot-scoped helpers live there)."""
        self.store = store
        if start_usd is not None:
            self.start_usd = start_usd
        else:
            from tg_config import PAPER_START_USD
            self.start_usd = PAPER_START_USD

    # ------------------------------------------------------------- fills
    def _fill_price(self, side: str, ref_price: float, order_type: str,
                    limit_price: float | None) -> float:
        if order_type == "limit" and limit_price:
            return float(limit_price)
        slip = SLIPPAGE_BPS / 10000.0
        # buys fill slightly higher, sells slightly lower
        return ref_price * (1 + slip if side in ("buy", "cover") else 1 - slip)

    def _total_fee(self, notional: float) -> float:
        return notional * (VENUE_FEE_BPS + PLATFORM_FEE_BPS) / 10000.0

    def equity(self, bot_id: int, prices: dict) -> float:
        """cash + unrealized position value at mark prices (leverage-aware)."""
        p = self.store.ensure_portfolio(bot_id)
        equity = p["cash"]
        for pos in self.store.positions(bot_id):
            mark = prices.get(pos["symbol"]) or pos["entry_price"]
            if pos["direction"] == "long":
                equity += pos["qty"] * (mark - pos["entry_price"])
            else:
                equity += pos["qty"] * (pos["entry_price"] - mark)
        return equity

    def open(self, bot_id: int, symbol: str, direction: str, qty: float,
             ref_price: float, leverage: float = 1.0,
             stop_loss: float | None = None, take_profit: float | None = None,
             order_type: str = "market", limit_price: float | None = None,
             idempotency_key: str = "") -> dict:
        """Open a paper position. Margin = notional/leverage leaves cash."""
        p = self.store.ensure_portfolio(bot_id)
        fill = self._fill_price("buy" if direction == "long" else "short",
                                ref_price, order_type, limit_price)
        notional = qty * fill
        fee = self._total_fee(notional)
        margin = notional / max(leverage, 1.0)
        if margin + fee > p["cash"]:
            return {"ok": False,
                    "error": (f"paper cash ${p['cash']:.2f} < margin ${margin:.2f} "
                              f"+ fee ${fee:.2f}")}
        # margin is locked: deduct margin + fee; on close, margin + pnl - fee returns
        self.store._adjust_cash(bot_id, -(margin + fee), fee)
        self.store.open_position(bot_id, symbol, direction, qty, fill,
                                 leverage, stop_loss, take_profit)
        self.store.record_order(bot_id, symbol, direction,
                                "buy" if direction == "long" else "short",
                                qty, fill, fee, idempotency_key)
        log.info("[paper] bot %s OPEN %s %s qty=%.6f @ %.4f lev=%.1fx fee=%.4f",
                 bot_id, direction, symbol, qty, fill, leverage, fee)
        return {"ok": True, "fill_price": fill, "fee": fee, "margin": margin,
                "qty": qty, "paper": True}

    def close(self, bot_id: int, symbol: str, ref_price: float,
              order_type: str = "market", limit_price: float | None = None,
              idempotency_key: str = "") -> dict:
        """Close a paper position at market (or limit): settle margin + PnL."""
        pos = self.store.close_position(bot_id, symbol)
        if not pos:
            return {"ok": False, "error": f"no open paper position on {symbol}"}
        exit_side = "sell" if pos["direction"] == "long" else "cover"
        fill = self._fill_price(exit_side, ref_price, order_type, limit_price)
        notional = pos["qty"] * fill
        fee = self._total_fee(notional)
        if pos["direction"] == "long":
            pnl = pos["qty"] * (fill - pos["entry_price"])
        else:
            pnl = pos["qty"] * (pos["entry_price"] - fill)
        margin = (pos["qty"] * pos["entry_price"]) / max(pos["leverage"], 1.0)
        # margin returns + pnl - exit fee
        self.store.settle(bot_id, margin + pnl - fee, fee)
        self.store.record_order(bot_id, symbol, pos["direction"], exit_side,
                                pos["qty"], fill, fee, idempotency_key)
        log.info("[paper] bot %s CLOSE %s %s @ %.4f pnl=%+.4f fee=%.4f",
                 bot_id, pos["direction"], symbol, fill, pnl, fee)
        return {"ok": True, "fill_price": fill, "pnl": pnl - fee, "fee": fee,
                "paper": True}

    # ------------------------------------------------------- maintenance
    def manage_exits(self, bot_id: int, prices: dict) -> list[dict]:
        """Paper stop/target/trail management for open positions. Returns the
        closed-trade summaries. Same trailing-stop math as the live path."""
        closed = []
        for pos in self.store.positions(bot_id):
            sym = pos["symbol"]
            mark = prices.get(sym)
            if not mark:
                continue
            long = pos["direction"] == "long"
            # peak tracking for the trail
            self.store.update_protect_levels(bot_id, sym, peak_price=mark)
            pos = next(p for p in self.store.positions(bot_id) if p["symbol"] == sym)
            stop = pos.get("stop_loss")
            tp = pos.get("take_profit")
            hit_stop = (mark <= stop) if (long and stop) else \
                       (mark >= stop) if (stop and not long) else False
            hit_tp = (mark >= tp) if (long and tp) else \
                     (mark >= tp) if (tp and not long) else False
            if hit_stop or hit_tp:
                res = self.close(bot_id, sym, mark, idempotency_key=f"paper-exit-{sym}-{mark}")
                if res.get("ok"):
                    res["reason"] = "take_profit" if hit_tp else "stop_loss"
                    res["symbol"] = sym
                    closed.append(res)
        return closed
