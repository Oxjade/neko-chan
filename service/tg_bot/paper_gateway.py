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
import time

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
        """Open a paper position. Margin = notional/leverage leaves cash.

        LIMIT ORDERS BEHAVE LIKE REAL ONES: a marketable limit (buy at/above
        the market, short at/below) fills immediately at the limit price; a
        non-marketable limit RESTS in paper_pending until the market touches
        it (filled by manage_exits). The old instant-fill-at-limit behavior
        faked fills the market never agreed to."""
        self.store.ensure_portfolio(bot_id)
        if order_type == "limit" and limit_price and limit_price > 0:
            limit_price = float(limit_price)
            marketable = (limit_price >= ref_price) if direction == "long" \
                else (limit_price <= ref_price)
            if not marketable:
                self.store.upsert_pending(
                    bot_id, symbol, direction, qty, limit_price, leverage,
                    stop_loss, take_profit, idempotency_key
                    or f"paper-pending-{symbol}-{int(time.time() * 1000)}")
                log.info("[paper] bot %s LIMIT RESTING %s %s qty=%.6f @ %.4f "
                         "(market %.4f)", bot_id, direction, symbol, qty,
                         limit_price, ref_price)
                return {"ok": True, "pending": True, "limit_price": limit_price,
                        "fill_price": limit_price, "qty": qty, "paper": True}
            fill = limit_price
        else:
            fill = self._fill_price("buy" if direction == "long" else "short",
                                    ref_price, order_type, limit_price)
        p = self.store.ensure_portfolio(bot_id)
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
                "qty": qty, "leverage": leverage, "direction": direction,
                "paper": True}

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
        """Paper stop/target/trail management for open positions + resting
        limit fills. Returns closed-trade summaries and triggered fills
        (each dict carries a 'kind': 'take_profit' | 'stop_loss' | 'limit_fill').
        Same trailing-stop math as the live path."""
        events = []
        # 1) Resting LIMIT fills: a buy/long limit fills when the market
        #    drops to it, a short limit when the market rises to it.
        for pend in self.store.pending_orders(bot_id):
            mark = prices.get(pend["symbol"])
            if not mark:
                continue
            touched = (mark <= pend["limit_price"]) if pend["direction"] == "long" \
                else (mark >= pend["limit_price"])
            if not touched:
                continue
            self.store.remove_pending(bot_id, pend["symbol"])
            fill = self.open(
                bot_id, pend["symbol"], pend["direction"], pend["qty"], mark,
                leverage=pend["leverage"], stop_loss=pend["stop_loss"],
                take_profit=pend["take_profit"], order_type="limit",
                limit_price=pend["limit_price"],
                idempotency_key=pend["idempotency_key"])
            if fill.get("ok"):
                fill["kind"] = "limit_fill"
                fill["symbol"] = pend["symbol"]
                fill["direction"] = pend["direction"]
                events.append(fill)
                log.info("[paper] bot %s LIMIT FILLED %s %s qty=%.6f @ %.4f",
                         bot_id, pend["direction"], pend["symbol"], pend["qty"],
                         pend["limit_price"])
        # 2) Stop / target / trail management on open positions
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
                    res["kind"] = "take_profit" if hit_tp else "stop_loss"
                    res["reason"] = res["kind"]
                    res["symbol"] = sym
                    events.append(res)
        return events
