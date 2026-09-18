"""Degen executor (§2 / §3.2a) — the single write choke-point.

Wraps the existing `SUIAdapter` (signing, gas selection, broadcast) and feeds it
PTBs from `ptb.py`. ALL degen caps + kill-switch + idempotency are enforced HERE,
across launchpads, so no strategy (sniper/copy/DCA/bundle) can bypass them.
Fees are atomic PTB legs (never a gas-budget redirect).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "execution"))

from sui_adapter import SUIAdapter  # signing/broadcast primitives, single-sourced

from . import constants as K
from . import ptb
from .chain import Chain
from .db import DegenLedger

log = logging.getLogger(__name__)


class Caps:
    """§5.1 — enforced globally per bot, summed across launchpads + bundle legs."""

    def __init__(self, per_order_sui: float = 20.0, daily_loss_sui: float = 30.0,
                 max_open: int = 8, budget_sui: float = 40.0,
                 per_symbol_sui: float = 25.0):
        self.per_order_sui = per_order_sui
        self.daily_loss_sui = daily_loss_sui
        self.max_open = max_open
        self.budget_sui = budget_sui
        self.per_symbol_sui = per_symbol_sui

    @classmethod
    def from_config(cls, cfg: dict) -> "Caps":
        c = cfg.get("caps", {}) or {}
        return cls(float(c.get("per_order", 20.0)), float(c.get("daily_loss", 30.0)),
                   int(c.get("max_open", 8)), float(cfg.get("budget_sui") or 40.0),
                   float(c.get("per_symbol", 25.0)))


def _h(*p) -> str:
    m = hashlib.sha256()
    for x in p:
        m.update(str(x).encode())
        m.update(b"\x1f")
    return m.hexdigest()


class DegenExecutor:
    def __init__(self, ch: Chain, ledger: DegenLedger,
                 adapter_for_wallet, fee_recipient: str = ""):
        """adapter_for_wallet(wallet_address) -> (SUIAdapter | None, key_ref).
        Supplied by the caller (userbot) so key custody stays in exec_vault; the
        executor never sees plaintext keys, only an adapter bound to one wallet."""
        self.ch = ch
        self.ledger = ledger
        self._adapter_for = adapter_for_wallet     # legacy single-arg fallback
        # Per-bot wallet resolvers. Wallet custody is per-bot while the runtime is
        # process-wide: keying resolvers on bot_id (instead of a mutable global)
        # removes the cross-bot wallet race (TBP-02). Set via
        # set_bot_wallet_factory(); falls back to _adapter_for for tests/legacy.
        self._bot_factories: dict[int, object] = {}
        self._spot = None            # set via set_spot_adapter (post-grad, §3.5)
        self.fee_recipient = fee_recipient or os.environ.get("NEKO_FEE_ADDR", "")

    def set_bot_wallet_factory(self, bot_id: int, factory) -> None:
        """Register a per-bot resolver: factory(wallet_addr) -> (SUIAdapter|None, addr)."""
        self._bot_factories[int(bot_id)] = factory

    def _adapter(self, bot_id: int, wallet: str):
        """Resolve (adapter, address) for THIS bot. Never uses another bot's
        resolver, so concurrent/cached chat-buys cannot sign from the wrong wallet."""
        factory = self._bot_factories.get(int(bot_id)) if bot_id is not None else None
        if factory is not None:
            return factory(wallet)
        return self._adapter_for(wallet)

    def _reserve_order(self, bot_id: int, *, wallet: str, intent: str, otype: str,
                       launchpad: str, idempotency_key: str, curve_id: str = "",
                       token_type: str = "", qty_sui: float = 0.0,
                       lane_index: int = -1) -> int:
        """Claim the idempotency key with state='pending' BEFORE signing/broadcast.
        Returns the order id, or -1 when a prior submission already holds the key
        (caller must abort). set_order() later flips it to fired/failed."""
        return self.ledger.add_order(
            bot_id, wallet=wallet or "", intent=intent, otype=otype, launchpad=launchpad,
            idempotency_key=idempotency_key, curve_id=curve_id, token_type=token_type or "",
            qty_sui=qty_sui, lane_index=lane_index, state="pending")

    # ---------------- caps / kill-switch ----------------
    def _check(self, bot_id: int, cfg: dict, sui_amount: float, curve_id: str,
               count_open: bool = True) -> str:
        if not cfg.get("enabled"):
            return "degen disabled"
        # NO AI-key gate: degen fills are user-initiated (card tap, sniper on a
        # watched deployer, bundle burst). The LLM key only ever drives swing
        # decisions; requiring it here would block manual users for no reason.
        if self.is_killed(bot_id):
            return "kill-switch engaged"
        caps = Caps.from_config(cfg)
        if sui_amount > caps.per_order_sui:
            return f"order {sui_amount} > per-order cap {caps.per_order_sui}"
        if count_open:
            opens = self.ledger.positions(bot_id)
            if len(opens) >= caps.max_open and not any(p["curve_id"] == curve_id for p in opens):
                return "max open positions reached"
            sym = sum(p["entry_sui"] for p in opens if p["curve_id"] == curve_id)
            if sym + sui_amount > caps.per_symbol_sui:
                return f"per-symbol exposure would exceed {caps.per_symbol_sui}"
            if sum(p["entry_sui"] for p in opens) + sui_amount > caps.budget_sui:
                return "degen budget exhausted"
        # daily loss floor
        if self.ledger.realized_pnl_today(bot_id) < -caps.daily_loss_sui:
            return "daily loss cap hit"
        return ""

    def is_killed(self, bot_id: int) -> bool:
        cfg = self.ledger.get_config(bot_id)
        return bool((cfg.get("caps") or {}).get("killed"))

    def kill(self, bot_id: int, on: bool = True) -> None:
        cfg = self.ledger.get_config(bot_id)
        caps = cfg.get("caps", {}) or {}
        caps["killed"] = bool(on)
        self.ledger.set_config(bot_id, caps=caps)

    # ---------------- gas / coin pick ----------------
    def _pick_sui_coin(self, adapter: SUIAdapter, need_mist: int):
        coins = []
        for attempt in range(3):
            try:
                coins = self.ch.coins(adapter.address)
            except Exception:
                coins = []
            coins = [c for c in coins if c["objectId"] != getattr(adapter, "_gas_excl", None)]
            big = [c for c in coins if c["balance_mist"] >= need_mist]
            if big:
                # don't use the same coin for gas and value: pick two distinct
                # when possible
                big.sort(key=lambda c: c["balance_mist"])
                return big[-1], big[:-1] or coins
            time.sleep(0.4 * (attempt + 1))
        return None, coins

    def _sui_sufficient(self, adapter: SUIAdapter, need_mist: int) -> bool:
        """SUI sufficiency for need + ~0.01 SUI gas margin.

        GraphQL is eventually consistent and its objects-coin query can serve a
        partial page (both observed live on mainnet), so a single balance read
        sometimes reads 0/stale right after a fill. Refuse ONLY when every
        source agrees we're short:
          - retry the address balance briefly,
          - then cross-check coins() (which carries an RPC fallback).
        """
        margin = need_mist + K.MIST // 100
        have = 0
        for _ in range(3):
            try:
                have = self.ch.balance(adapter.address)
            except Exception:
                have = 0
            if have >= margin:
                return True
            time.sleep(0.5)
        try:
            have = max(have, sum(c["balance_mist"] for c in self.ch.coins(adapter.address)))
        except Exception:
            pass
        return have >= margin

    # ---------------- quote / slippage guard (§5.2: never naked) ----------------
    def _min_out(self, st, sui_spend: float, slip_bps: int = 500) -> int:
        """Exact curve output for this spend (under (x+vX)(y+vY)=k), minus
        slippage. NOT the marginal-price approximation: a buy that moves the
        curve gets the average, and virtual reserves change the level entirely."""
        from .metrics import compute, expected_tokens_out, virtual_reserves
        if st is None or getattr(st, "kind", "") != "curve":
            from .metrics import compute as _c
            m = _c(self.ch, st) if st is not None else None
            if not m or not m.price_sui:
                return 0
            return int(sui_spend / m.price_sui * (10 ** m.decimals) * (1 - slip_bps / 10000))
        vx, vy = virtual_reserves(self.ch, st.curve_id)
        out = expected_tokens_out(st.sui_reserve_mist, st.token_reserve,
                                  int(sui_spend * 1e9), vx, vy)
        return int(out * (1 - slip_bps / 10000))

    def _received_tokens(self, wallet: str, token_type: str, before_mist: int) -> int:
        """Actual token atoms received = post-balance delta (real, not estimated).
        GraphQL balance is eventually consistent, so poll briefly for the delta."""
        for _ in range(4):
            try:
                after = self.ch.balance(wallet, token_type)
            except Exception:
                after = before_mist
            if after > before_mist:
                return after - before_mist
            import time as _t
            _t.sleep(0.5)
        return 0

    # ---------------- buy ----------------
    def buy(self, bot_id: int, *, launchpad: str, curve_id: str, token_type: str,
            curve_isv: int, sui_amount: float, min_out: int, wallet: str = "",
            idem: str = "", otype: str = "market", target_price: float | None = None,
            lane_index: int = -1, fee_bps: int = 0, source: str = "manual",
            dry_run: bool = False, sender_check: bool = True, slip_bps: int = 500) -> dict:
        cfg = self.ledger.get_config(bot_id)
        err = self._check(bot_id, cfg, sui_amount, curve_id)
        if err:
            return {"ok": False, "error": "caps: " + err}
        wallet = wallet or ""
        adapter, _ = self._adapter(bot_id, wallet) if wallet else (None, None)
        if wallet == "":
            adapter, waddr = self._adapter(bot_id, cfg.get("main_wallet") or "")
        if adapter is None:
            return {"ok": False, "error": "no wallet/adapter for bot"}
        # venue check: ONLY a live curve is buyable (graduating/pool/wallet/unknown
        # must never reach the broadcaster — §5.3 trust boundary).
        st = self._state(curve_id)
        if st is None or st.kind != "curve":
            return {"ok": False, "error": f"not a live curve ({getattr(st,'kind','none')})"}
        if st.kind == "pool":
            return {"ok": False, "error": "post-grad: use Aftermath path (§3.5 P1)"}
        # slippage floor if caller didn't supply one (§5.2). The curve model is
        # launchpad-specific (suipump uses a PriceConfig/reputation curve, not
        # constant-product), so we PROBE the chain: dry-run the buy with min_out=0
        # and read TokensPurchased.tokens_out, then apply slippage. Falls back to
        # the analytic estimate when simulation is unavailable.
        auto_min = (not min_out or min_out <= 0)
        need = int(sui_amount * K.MIST)
        # entry fee is charged on the SELL leg (deterministic SUI); buys pay none —
        # do NOT pretend an uncollectable entry fee on the confirm sheet.
        if not self._sui_sufficient(adapter, need):
            return {"ok": False, "error": "insufficient SUI balance"}
        coin, _ = self._pick_sui_coin(adapter, need)
        if not coin:
            return {"ok": False, "error": "insufficient SUI balance"}
        gas_coin = next((c for c in self.ch.coins(adapter.address)
                         if c["objectId"] != coin["objectId"] and c["balance_mist"] >= 2_000_000), None)
        before = self.ch.balance(adapter.address, st.token_type or token_type)
        from .launchpad import curve_pkg
        curve_pkg_id = curve_pkg((getattr(st, "curve_obj", {}) or {}).get("type", ""))

        def _build(mo):
            if gas_coin is None:
                # Single SUI coin: it must be both value and gas. Referencing the
                # same ObjectRef as an owned input AND gas is rejected by the node
                # (invalid withdraw reservation, 2x balance). Derive the buy coin
                # from GasCoin via SplitCoins and cap the gas budget.
                budget = min(K.SNIPE_GAS_BUDGET_MIST,
                             max(5_000_000, coin["balance_mist"] - need - 1_000_000))
                _in, _cmds = ptb.build_buy(
                    curve_id, curve_isv, token_type, None, mo,
                    user_addr=adapter.address, fee_atoms=0, fee_recipient="", referrer="",
                    buy_from_gas=True, buy_atoms=need, pkg=curve_pkg_id)
                return _in, _cmds, budget, coin
            budget = K.SNIPE_GAS_BUDGET_MIST
            _in, _cmds = ptb.build_buy(
                curve_id, curve_isv, token_type,
                (coin["objectId"], coin["version"], coin["digest"]), mo,
                user_addr=adapter.address, fee_atoms=0, fee_recipient="", referrer="",
                pkg=curve_pkg_id)
            return _in, _cmds, budget, gas_coin

        inputs, commands, budget, gas_sel = _build(0 if auto_min else min_out)
        if auto_min:
            exp = 0
            try:
                exp = adapter.simulate_event_u64(
                    inputs, commands, self.ch.gas_price(), budget, gas_sel,
                    "::bonding_curve::TokensPurchased", "tokens_out")
            except Exception:
                log.warning("buy simulation failed; using analytic estimate", exc_info=True)
            min_out = (int(exp * (1 - slip_bps / 10000)) if exp > 0
                       else (self._min_out(st, sui_amount) or 0))
            if min_out <= 0:
                return {"ok": False, "error": "no price to set slippage floor"}
            inputs, commands, budget, gas_sel = _build(min_out)
        if dry_run:
            return {"ok": True, "dry_run": True, "gas_price": self.ch.gas_price(),
                    "budget_cap_mist": K.SNIPE_GAS_BUDGET_MIST, "min_out": min_out}
        # Reserve the idempotency key BEFORE signing: a redelivered/replayed
        # message must not re-broadcast (TBP-03). 'pending' is inert — the order
        # evaluator only fires 'armed' orders — until set_order below.
        oid = self._reserve_order(bot_id, wallet=wallet, intent="buy", otype=otype,
                                  launchpad=launchpad,
                                  idempotency_key=idem or _h(bot_id, curve_id, "buy",
                                                             time.time_ns()),
                                  curve_id=curve_id, token_type=token_type,
                                  qty_sui=sui_amount, lane_index=lane_index)
        if oid <= 0:
            return {"ok": False, "error": "duplicate order (already submitted)"}
        try:
            out = adapter._broadcast_ptb(inputs, commands, self.ch.gas_price(),
                                         budget,
                                         gas_coin=gas_sel and {"objectId": gas_sel["objectId"],
                                                               "version": gas_sel["version"],
                                                               "digest": gas_sel["digest"]})
        except Exception as exc:
            log.exception("buy broadcast failed")
            self.ledger.set_order(oid, "failed", error=f"broadcast: {exc}"[:160])
            return {"ok": False, "error": f"broadcast: {exc}"}
        status = str(out.get("status", "")).upper()
        filled = status == "SUCCESS"            # §5.5: never assume a fill on unknown
        self.ledger.set_order(oid, "fired" if filled else "failed",
                              tx_digest=out.get("digest", ""),
                              error="" if filled else (status or "unknown"))
        got = self._received_tokens(adapter.address, st.token_type or token_type, before)
        if filled:
            self.ledger.record_fill(oid, out.get("digest", ""), sui=sui_amount,
                                    tokens=got, price=(sui_amount / (got / 10 ** 6) if got else 0.0))
            self.ledger.upsert_position(bot_id, launchpad, curve_id, token_type, wallet,
                                        symbol=getattr(st, "symbol", "") or "",
                                        add_sui=sui_amount, add_tokens=got)
        return {"ok": filled, "digest": out.get("digest", ""), "order_id": oid,
                "status": status, "tokens": got, "min_out": min_out}

    def _state(self, curve_id: str):
        from .launchpad import resolve_input
        try:
            return resolve_input(self.ch, curve_id)
        except Exception:
            return None

    def _inp_json(self, i):  # placeholder for future full dry-run JSON path
        return None

    # ---------------- sell (§3.2a fee on SUI side — deterministic) ----------------
    def sell(self, bot_id: int, o: dict, st, pos: dict, fee_bps: int = 0,
             idem: str = "", sell_atoms: int | None = None,
             sell_pct: float | None = None) -> dict:
        cfg = self.ledger.get_config(bot_id)
        if self.is_killed(bot_id):
            return {"ok": False, "error": "kill-switch engaged"}
        if sell_atoms is not None and sell_pct is not None:
            return {"ok": False, "error": "specify either sell_atoms or sell_pct, not both"}
        pct: float | None = None
        if sell_atoms is not None:
            if type(sell_atoms) is not int or not 0 < sell_atoms < 2 ** 64:
                return {"ok": False, "error": "sell_atoms must be a positive u64 integer"}
            if st.kind != "curve":
                return {"ok": False, "error": "amount-based sells are not supported for this venue"}
        elif sell_pct is not None:
            try:
                pct = float(sell_pct)
            except (TypeError, ValueError):
                return {"ok": False, "error": "sell_pct must be a number in (0, 100]"}
            if not 0 < pct <= 100:
                return {"ok": False, "error": "sell_pct must be a number in (0, 100]"}
            if st.kind != "curve":
                return {"ok": False, "error": "percentage sells are not supported for this venue"}
        wallet = o.get("wallet") or ""
        if wallet:
            adapter, _ = self._adapter(bot_id, wallet)
        else:
            adapter, _ = self._adapter(bot_id, cfg.get("main_wallet") or "")
        if adapter is None:
            return {"ok": False, "error": "no wallet/adapter for bot"}
        if st.kind == "curve":
            # Sell from the ENTIRE token balance: merge every token coin into the
            # largest in one PTB. Selling only the largest coin (the old behaviour)
            # stranded the rest on-chain while the ledger booked a full exit —
            # the 2026-09-17 "missing position" bug. `sell_atoms=None` exits 100%;
            # a smaller value splits off that slice and returns the remainder.
            tok_coins = [c for c in self.ch.coins(adapter.address, st.token_type)
                         if int(c.get("balance_mist") or 0) > 0]
            if not tok_coins:
                return {"ok": False, "error": "no token coins to sell"}
            tok_coins.sort(key=lambda c: c["balance_mist"], reverse=True)
            coin = tok_coins[0]
            token_ids = {c["objectId"] for c in tok_coins}
            tokens_before = sum(int(c["balance_mist"]) for c in tok_coins)
            extra = [(c["objectId"], c["version"], c["digest"]) for c in tok_coins[1:]]
            if pct is not None:
                # Resolve the percentage against the REAL on-chain balance (the
                # ledger can lag the chain); 100% stays the full merge + exit path.
                if pct >= 100:
                    sell_atoms = None
                else:
                    sell_atoms = int(tokens_before * pct / 100)
                    if sell_atoms <= 0:
                        return {"ok": False, "error": "sell percentage rounds to zero tokens"}
            if sell_atoms is not None and sell_atoms > tokens_before:
                return {"ok": False, "error": "sell amount exceeds available token balance"}
            partial = sell_atoms is not None and sell_atoms < tokens_before
            sold_atoms = sell_atoms if sell_atoms is not None else tokens_before
            est_sui = (sold_atoms * (st.sui_reserve_mist / max(1, st.token_reserve)) / K.MIST
                       if st.token_reserve else 0.0)
            gas = next((c for c in self.ch.coins(adapter.address)
                        if c["objectId"] not in token_ids
                        and c["balance_mist"] >= 2_000_000), None)
            gas_sel = gas and {"objectId": gas["objectId"], "version": gas["version"],
                               "digest": gas["digest"], "balance_mist": gas["balance_mist"]}
            from .launchpad import curve_pkg as _curve_pkg
            curve_pkg_id = _curve_pkg((getattr(st, "curve_obj", {}) or {}).get("type", ""))

            def _build_sell(fee_m: int, min_s: int):
                return ptb.build_sell(
                    o["curve_id"], st.curve_obj.get("shared_version", 0), st.token_type,
                    (coin["objectId"], coin["version"], coin["digest"]), min_s,
                    user_addr=adapter.address, fee_sui_mist=fee_m,
                    fee_recipient=self.fee_recipient, pkg=curve_pkg_id,
                    extra_coins=extra, sell_atoms=sold_atoms if partial else None)

            # The platform fee is charged on the SUI side, so it must track REAL
            # proceeds. Probe the chain by dry-running the exact sell (min floor 0)
            # and reading TokensSold.sui_out: the marginal x/y estimate understates
            # a large sell by orders of magnitude (2026-09-17: booked 7.3e-5 SUI
            # while the sale actually returned 0.392 SUI). Falls back to the
            # analytic estimate when simulation is unavailable.
            fee_mist = int(est_sui * 1e9 * fee_bps / 10000) if (fee_bps and self.fee_recipient) else 0
            min_sui = int(est_sui * 1e9 * 0.9)  # 10% slippage guard (pre-sim fallback)
            inputs, commands = _build_sell(fee_mist, min_sui)
            sim_mist = 0
            try:
                sim_mist = adapter.simulate_event_u64(
                    inputs, commands, self.ch.gas_price(), K.SNIPE_GAS_BUDGET_MIST,
                    gas_sel, "::bonding_curve::TokensSold", "sui_out")
            except Exception:
                log.warning("sell simulation failed; using analytic estimate", exc_info=True)
            if sim_mist > 0:
                fee_mist = (int(sim_mist * fee_bps / 10000)
                            if (fee_bps and self.fee_recipient) else 0)
                min_sui = int(sim_mist * 0.9)
                inputs, commands = _build_sell(fee_mist, min_sui)
            qty_est = (sim_mist / K.MIST) if sim_mist > 0 else est_sui
            # Reserve before signing (TBP-03) so a replayed sell cannot double-exit.
            oid = self._reserve_order(bot_id, wallet=wallet, intent="sell",
                                      otype=o.get("otype", "market"),
                                      launchpad=o["launchpad"], curve_id=o["curve_id"],
                                      token_type=st.token_type, qty_sui=round(qty_est, 6),
                                      idempotency_key=idem or _h(bot_id, o["curve_id"], "sell",
                                                                 time.time_ns()))
            if oid <= 0:
                return {"ok": False, "error": "duplicate order (already submitted)"}
            try:
                out = adapter._broadcast_ptb(inputs, commands, self.ch.gas_price(),
                                             K.SNIPE_GAS_BUDGET_MIST, gas_coin=gas_sel)
            except Exception as exc:
                self.ledger.set_order(oid, "failed", error=f"broadcast: {exc}"[:160])
                return {"ok": False, "error": f"broadcast: {exc}"}
            filled = str(out.get("status", "")).upper() == "SUCCESS"
            self.ledger.set_order(oid, "fired" if filled else "failed",
                                  tx_digest=out.get("digest", ""),
                                  error="" if filled else str(out.get("status", "unknown")))
            if filled:
                digest = out.get("digest", "")
                # Book the EXECUTED tx, not the estimate: read TokensSold ground
                # truth (sui_out/tokens_in), falling back to the dry-run then est.
                got_mist = (adapter.event_u64(digest, "::bonding_curve::TokensSold", "sui_out")
                            if hasattr(adapter, "event_u64") else 0) or sim_mist
                got_tokens = (adapter.event_u64(digest, "::bonding_curve::TokensSold", "tokens_in")
                              if hasattr(adapter, "event_u64") else 0) or sold_atoms
                if got_mist <= 0:
                    got_mist = int(round(est_sui * K.MIST))
                proceeds = got_mist / K.MIST
                remaining = max(0, tokens_before - got_tokens)
                self.ledger.record_fill(oid, digest, sui=proceeds, tokens=got_tokens,
                                        price=proceeds / max(1e-9, got_tokens / 1e6),
                                        fee_sui=fee_mist / K.MIST)
                entry = float(pos.get("entry_sui") or 0.0)
                cost_removed = entry * got_tokens / max(1, tokens_before)
                pid = self.ledger.upsert_position(bot_id, o["launchpad"], o["curve_id"],
                                                  st.token_type, wallet,
                                                  add_sui=-cost_removed,
                                                  add_tokens=-got_tokens)
                # Position status follows the ACTUAL amount sold, so a partial exit
                # stays open with the unsold remainder and only a 100% exit closes.
                if remaining <= 0:
                    self.ledger.set_position(pid, status="closed", tokens=0.0, entry_sui=0.0)
                else:
                    self.ledger.set_position(
                        pid, status="open", tokens=float(remaining),
                        entry_sui=entry * remaining / tokens_before,
                        avg_entry_sui=entry / tokens_before)
                sold_atoms = got_tokens
            else:
                remaining = tokens_before
            return {"ok": filled, "digest": out.get("digest", ""), "order_id": oid,
                    "status": out.get("status", ""), "tokens_sold": sold_atoms if filled else 0,
                    "tokens_left": remaining if filled else tokens_before}
        # pool venue → Aftermath legs (resting limit w/ native SL works today;
        # market-swap composition is the spot adapter's own open TODO)
        return self.buy_post_grad(bot_id, o, st, side="sell", pos=pos)

    # ---------------- post-grad via Aftermath SOR (§3.5) ----------------
    def set_spot_adapter(self, factory) -> None:
        """Optional override: factory(addr) -> AftermathSpotAdapter-like object.
        Tests inject a fake; production uses the real (keyless) REST adapter."""
        self._spot = factory

    def _spot_client(self):
        if self._spot:
            return self._spot("")
        from spot.adapter import AftermathSpotAdapter
        return AftermathSpotAdapter("", self.ch.network)

    @staticmethod
    def _kind_b64_of(resp) -> str:
        """The build endpoint returns the base64 TransactionKind; tolerate both
        a bare string and {transaction|txBytes|...: b64} wrappers."""
        if isinstance(resp, str):
            return resp
        if isinstance(resp, dict):
            for k in ("transaction", "txBytes", "txnBytes", "tx", "base64"):
                v = resp.get(k)
                if isinstance(v, str) and v:
                    return v
            for v in resp.values():          # any long b64-looking string
                if isinstance(v, str) and len(v) > 32:
                    return v
        raise ValueError("unexpected tx-build response shape")

    def _dex_swap(self, bot_id: int, coin_in_type: str, coin_out_type: str,
                  in_atoms: int, slip_bps: int, idem: str, intent: str,
                  launchpad: str, curve_id: str, token_type: str,
                  pos: dict | None = None, cetus_only: bool = False,
                  symbol: str = "") -> dict:
        """Quote -> build -> sign -> broadcast one Aftermath-routed swap.
        The 0.5% integrator fee rides INSIDE the route (externalFee), so fills
        are booked net of fee from real balance deltas — same integrity rules
        as curve buys."""
        import base64
        key = curve_id or token_type      # generics have no curve: type is the key
        adapter, waddr = self._adapter(bot_id, "")
        if adapter is None:
            return {"ok": False, "error": "no wallet for DEX swap"}
        try:
            have = self.ch.balance(adapter.address, coin_in_type)
        except Exception:
            have = 0
        if have < in_atoms:
            return {"ok": False, "error": "insufficient SUI balance" if coin_in_type == K.SUI_COIN_TYPE
                    else "insufficient token balance"}
        client = self._spot_client()
        fee = ({"recipient": self.fee_recipient, "feePercentage": 0.5}
               if self.fee_recipient and K.PLATFORM_FEE_BPS else None)
        # suipump graduates: their Cetus graduation pool is the primary market;
        # pinning the route guarantees the price we DISPLAY is the venue we
        # EXECUTE on (the SOR picks Cetus for them anyway, verified live).
        wl = ["Cetus"] if cetus_only else None
        try:
            q = client.quote_route(coin_in_type, coin_out_type,
                                   amount_in_atoms=in_atoms, slippage_bps=slip_bps,
                                   external_fee=fee, protocols_whitelist=wl)
        except Exception:
            if not wl:
                raise
            q = client.quote_route(coin_in_type, coin_out_type,
                                   amount_in_atoms=in_atoms, slippage_bps=slip_bps,
                                   external_fee=fee)   # Cetus absent → best route
        # Reserve before signing (TBP-03): 'pending' is inert until set_order.
        oid = self._reserve_order(bot_id, wallet=waddr, intent=intent, otype="market",
                                  launchpad=launchpad, curve_id=key,
                                  token_type=token_type, qty_sui=in_atoms / 1e9,
                                  idempotency_key=idem)
        if oid <= 0:
            return {"ok": False, "error": "duplicate order (already submitted)"}
        try:
            before = self.ch.balance(adapter.address, coin_out_type)
        except Exception:
            before = 0
        try:
            kind_b64 = self._kind_b64_of(client.swap_tx_b64(q, adapter.address, slip_bps))
            kind = base64.b64decode(kind_b64)
            if kind[:1] != b"\x00":
                raise ValueError("TransactionKind variant not ProgrammableTransaction")
            out = adapter._broadcast_raw_ptb(kind[1:], self.ch.gas_price(),
                                             K.SNIPE_GAS_BUDGET_MIST)
        except Exception as exc:
            msg = str(exc)
            self.ledger.set_order(oid, "failed", error=msg[:120])
            if "nsufficient" in msg:
                return {"ok": False, "error": "insufficient SUI balance", "order_id": oid}
            return {"ok": False, "error": "aftermath: " + msg[:120], "order_id": oid}
        status = str(out.get("status", "")).upper()
        filled = status == "SUCCESS"
        self.ledger.set_order(oid, "fired" if filled else "failed",
                              tx_digest=out.get("digest", ""),
                              error="" if filled else (status or "unknown"))
        if filled:
            try:
                got = max(0, self.ch.balance(adapter.address, coin_out_type) - before)
            except Exception:
                got = 0
            fee_sui = 0.0
            for fb in (q.get("feeBreakdown") or []):
                if fee and fb.get("recipient") == fee["recipient"]:
                    try:
                        fee_sui += int(str(fb.get("amount", "0")).rstrip("n")) / 1e9
                    except Exception:
                        pass
            if intent == "buy":
                sui_spent = in_atoms / 1e9
                self.ledger.record_fill(oid, out.get("digest", ""), sui=sui_spent,
                                        tokens=got,
                                        price=sui_spent / max(1e-9, got / 1e6),
                                        fee_sui=fee_sui)
                self.ledger.upsert_position(bot_id, launchpad, key, token_type,
                                            adapter.address, symbol=symbol,
                                            add_sui=sui_spent,
                                            add_tokens=got)
            else:
                sui_got = got / 1e9
                self.ledger.record_fill(oid, out.get("digest", ""), sui=sui_got,
                                        tokens=in_atoms,
                                        price=sui_got / max(1e-9, in_atoms / 1e6),
                                        fee_sui=fee_sui)
                entry = (pos or {}).get("entry_sui") or sui_got
                self.ledger.upsert_position(bot_id, launchpad, key, token_type,
                                            adapter.address,
                                            add_sui=-min(entry, sui_got),
                                            add_tokens=-in_atoms)
        return {"ok": filled, "digest": out.get("digest", ""), "order_id": oid,
                "status": status}

    def buy_post_grad(self, bot_id: int, o: dict, st, *, side: str = "buy",
                      pos: dict | None = None, slip_bps: int = 500,
                      idem: str = "") -> dict:
        """Graduated tokens: exact same flow as curve trades, executed as an
        Aftermath SOR swap (routes through the Cetus graduation pool)."""
        if side == "buy":
            return self._dex_swap(bot_id, K.SUI_COIN_TYPE, st.token_type,
                                  int(float(o.get("qty_sui") or 0) * 1e9), slip_bps,
                                  idem or f"pgbuy{st.curve_id}{time.time_ns()}",
                                  "buy", st.launchpad or "suipump", st.curve_id,
                                  st.token_type,
                                  cetus_only=(st.launchpad or "suipump") == "suipump",
                                  symbol=getattr(st, "symbol", "") or "")
        atoms = int((pos or {}).get("tokens") or 0)
        if atoms <= 0:
            return {"ok": False, "error": "no tokens to sell"}
        return self._dex_swap(bot_id, st.token_type, K.SUI_COIN_TYPE, atoms,
                              max(slip_bps, 500),    # exits need >=5% room
                              idem or f"pgsell{st.curve_id}{time.time_ns()}",
                              "sell", st.launchpad or "suipump", st.curve_id,
                              st.token_type, pos=pos,
                              cetus_only=(st.launchpad or "suipump") == "suipump")

    # ---------------- spread-burst (§3.6) ----------------
    def spread_burst(self, bot_id: int, *, launchpad: str, curve_id: str,
                     token_type: str, curve_isv: int, total_sui: float,
                     min_out_each: int, legs: list[dict],
                     burst_fee_mist: int = K.BUNDLE_FEE_MIST, idem: str = "") -> dict:
        """1 decision → N own wallets, back-to-back. Non-atomic across wallets
        (documented honestly). burst_fee → NEKO_FEE_WALLET from the MAIN wallet's
        first leg. legs = [{wallet, amount_sui}] summing ≤ caps."""
        cfg = self.ledger.get_config(bot_id)
        if not cfg.get("enabled"):
            return {"ok": False, "error": "degen off"}
        caps = Caps.from_config(cfg)
        if total_sui > caps.budget_sui:
            return {"ok": False, "error": "burst exceeds budget"}
        if self.is_killed(bot_id):
            return {"ok": False, "error": "kill-switch engaged"}
        # ---- fee: flat 5 SUI → NEKO_FEE_WALLET, charged ONCE from the main wallet,
        # BEFORE any leg (real transfer, not a display promise). §3.2a ----
        fee_res = {"ok": True, "skipped": True}
        if burst_fee_mist and self.fee_recipient:
            adapter, _ = self._adapter(bot_id, "")        # this bot's main wallet
            if adapter is None:
                return {"ok": False, "error": "no main wallet to pay bundle fee"}
            fee_sui = burst_fee_mist / K.MIST
            have = self.ch.balance(adapter.address) / K.MIST
            if have < fee_sui + 0.05:
                return {"ok": False, "error": f"bundle fee 5 SUI needs {fee_sui+0.05:.2f} SUI in main"}
            fee_res = adapter.transfer_asset(self.fee_recipient, fee_sui, asset="SUI")
            if not fee_res.get("ok"):
                return {"ok": False, "error": "bundle fee transfer failed: "
                        + str(fee_res.get("error", "?"))}
            self.ledger.record_fill(0, fee_res.get("digest", ""), sui=0.0, tokens=0,
                                    price=0.0, fee_sui=fee_sui)   # fee-only row
        results = []
        for i, leg in enumerate(legs):
            r = self.buy(bot_id, launchpad=launchpad, curve_id=curve_id,
                         token_type=token_type, curve_isv=curve_isv,
                         sui_amount=leg["amount_sui"], min_out=min_out_each,
                         wallet=leg["wallet"], idem=idem and _h(idem, leg["wallet"]),
                         source="bundle")
            results.append(r)
            if not r.get("ok") and i == 0:
                # orphan policy: first leg failed → abort before risking more wallets
                return {"ok": False, "error": "first leg failed, aborting", "legs": results}
        ok_n = sum(1 for r in results if r.get("ok"))
        return {"ok": ok_n > 0, "legs_ok": ok_n, "legs_total": len(legs),
                "results": results, "bundle_fee_sui": burst_fee_mist / K.MIST,
                "orphan_policy": "alert" if ok_n != len(legs) else "clean"}
