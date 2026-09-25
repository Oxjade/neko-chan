"""WalletTracker: background alerts when a tracked wallet trades on Sui.

Polls the SuiPump bonding-curve TokensPurchased / TokensSold event streams
(cursor-resumed, same pattern as SuipumpStreamer), filters nodes where the
buyer/seller is one of the in-chat tracked wallets (chat_track, chattrack.py),
and posts a compact HTML alert back to the chat that subscribed — including a
one-tap "buy it" button that executes a degen buy for the tagger.

The original @neko username is resolved once via Telegram getMe so alert CTAs
always reference the REAL master bot handle, not a hard-coded name.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from degen.constants import MIST, SUI_COIN_TYPE, SUIPUMP_PACKAGES, SUIPUMP_MODULE
from degen.runtime import get_ledger, env_on
from degen.chain import Chain
from degen.launchpad import resolve_input
from degen.db import DegenLedger
from notifier import Notifier, SendBudget

log = logging.getLogger(__name__)

POLL_S = float(os.getenv("TRACKER_POLL_S", "2.0"))
PAGE = 50
WALLET_COOLDOWN_S = float(os.getenv("TRACKER_COOLDOWN_S", "20.0"))
SYMBOL_TTL_S = float(os.getenv("TRACKER_SYMBOL_TTL_S", "120.0"))
DEFAULT_MIN_SUI = 1.0
TX_PAGE = 15          # recent txs fetched per tracked wallet per poll
CATCHUP_MAX = 5       # max balance alerts replayed per wallet in one poll (burst cap)
SUI_DECIMALS = 9

_EVENTS = ("TokensPurchased", "TokensSold")
_SIDE = {"TokensPurchased": ("buyer", "sui_in", "bought"),
         "TokensSold": ("seller", "sui_out", "sold")}

# CTA registry: token -> payload. Repopulated by alerts, consumed by the
# `sb:tbuy:` callback. Keyed with a short id (Telegram callback_data <= 64B).
_TRACKER: "WalletTracker | None" = None


def get_tracker() -> "WalletTracker | None":
    return _TRACKER


def resolve_neko_username(token: str | None, fallback: str = "neko_tradesbot") -> str:
    """The REAL @username of the master bot, via Telegram getMe (cached once).

    This is what alert CTAs (@neko buy …) should reference. Falls back to the
    configured/default handle if getMe is unreachable or the token is empty."""
    try:
        import requests
        if not token:
            return fallback
        r = requests.post(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        j = r.json() if r.ok else {}
        uname = (j.get("result") or {}).get("username")
        return uname or fallback
    except Exception:
        return fallback


class WalletTracker:
    def __init__(self, ch: Chain, led: DegenLedger, notifier: Notifier,
                 neko_username: str = "neko_tradesbot",
                 poll_s: float = POLL_S, page: int = PAGE):
        self.ch = ch
        self.led = led
        self.notifier = notifier
        self.neko_username = neko_username
        self.poll_s = poll_s
        self.page = page
        self._stop = threading.Event()
        self._seen: set[tuple] = set()
        self._seen_ring: list[tuple] = []
        self._cooldown: dict[tuple, float] = {}
        self._symbols: dict[str, tuple[str, str, float]] = {}  # curve -> (symbol, ca, ts)
        self._ctas: dict[str, dict] = {}
        self._cta_lock = threading.Lock()
        self._wallet_set: set[str] = set()
        self._rows: list[dict] = []
        self._coin_meta: dict[str, tuple[str, int, float]] = {}  # coin_type -> (label, decimals, ts)
        self._event_digests: set[tuple[str, str]] = set()  # (wallet, digest) already event-alerted

    # ---------------- subscription snapshot ----------------
    def _reload(self) -> None:
        self._rows = self.led.chat_track_all()
        self._wallet_set = {r["wallet"].lower() for r in self._rows}

    # ---------------- cursor ----------------
    def _cursor_key(self, ev: str, pkg: str) -> str:
        return f"events:track:{ev}:{pkg}"

    def _mark_seen(self, key: tuple) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        self._seen_ring.append(key)
        if len(self._seen_ring) > 5000:
            old = self._seen_ring.pop(0)
            self._seen.discard(old)
        return True

    def _alert_eligible(self, row: dict, ev_name: str, amt: float) -> bool:
        want = int(row.get("buys", 3))
        if ev_name == "TokensSold" and want == 1:
            return False
        if ev_name == "TokensPurchased" and want == 2:
            return False
        return amt >= float(row.get("min_sui", DEFAULT_MIN_SUI))

    # ---------------- symbol resolution ----------------
    def _symbol_for(self, curve_id: str) -> tuple[str, str]:
        """(symbol, contract_address) for a curve, cached; graceful fallback."""
        now = time.time()
        hit = self._symbols.get(curve_id)
        if hit and now - hit[2] < SYMBOL_TTL_S:
            return hit[0], hit[1]
        symbol, ca = f"curve:{curve_id[:8]}", curve_id
        try:
            st = resolve_input(self.ch, curve_id)
            symbol = (getattr(st, "symbol", "") or "")[:16] or \
                     (getattr(st, "name", "") or "")[:24] or symbol
            ca = getattr(st, "token_type", "") or curve_id
        except Exception as exc:
            log.debug("symbol resolve failed curve=%s: %s", curve_id, exc)
        self._symbols[curve_id] = (symbol, ca, now)
        return symbol, ca

    # ---------------- one poll ----------------
    def poll_once(self) -> int:
        self._reload()
        if not self._wallet_set:
            return 0
        fired = 0
        for pkg in SUIPUMP_PACKAGES:
            for ev in _EVENTS:
                base = f"{pkg}::{SUIPUMP_MODULE}::{ev}"
                key = self._cursor_key(ev, pkg)
                try:
                    nodes, cursor, _has = self.ch.events(base, first=self.page,
                                                         after=self.led.get_cursor(key))
                except Exception as exc:
                    log.warning("tracker poll %s: %s", ev, exc)
                    continue
                for node in nodes:
                    if self._handle(node, ev):
                        fired += 1
                if cursor:
                    self.led.set_cursor(key, cursor)
        fired += self._poll_balances()
        return fired

    # ------- generic any-coin balance alerts (sends / receives / any move) -------
    def _poll_balances(self) -> int:
        """For each tracked wallet, walk its recent txs (newest-first) and alert on
        any coin balance change (not just SuiPump trades). Per-wallet low-water
        digest cursor avoids alerting history or re-alerting the same digests."""
        fired = 0
        for wallet in sorted(self._wallet_set):
            try:
                digests = self.ch.wallet_txs(wallet, first=TX_PAGE)
            except Exception as exc:
                log.warning("tracker bal poll %s: %s", wallet, exc)
                continue
            low_key = f"tx:track:{wallet}"
            low = self.led.get_cursor(low_key)
            if not low:
                if digests:
                    self.led.set_cursor(low_key, digests[0])   # baseline: no history spam
                continue
            if not digests:
                continue
            new = []
            for d in digests:
                if d == low:
                    break
                new.append(d)
                if len(new) >= TX_PAGE:
                    break
            if not new:
                continue
            new = new[:CATCHUP_MAX]
            consumed_newest = None
            for d in new:
                f, consumed = self._handle_balance_digest(wallet, d)
                fired += f
                # Newest-first: the first consumed digest is the high-water mark
                # we may advance to. Anything newer that failed stays above the
                # cursor and is retried on the next poll.
                if consumed and consumed_newest is None:
                    consumed_newest = d
            if consumed_newest is not None:
                self.led.set_cursor(low_key, consumed_newest)
        return fired

    def _handle_balance_digest(self, wallet: str, digest: str) -> tuple[int, bool]:
        """Process one wallet tx: fetch its deltas once, then alert every
        eligible subscriber row for that wallet on it.

        Returns (fired, consumed). `consumed` is False only when the digest must
        be retried (delta lookup failed, or a Telegram send was not delivered).
        """
        if self._event_digests_contain(wallet, digest):
            return 0, True
        if not self._mark_seen(("bal", wallet, digest)):
            return 0, True
        try:
            deltas = self.ch.tx_balance_deltas(wallet, digest)
        except Exception as exc:
            log.warning("tracker bal deltas %s %s: %s", wallet, digest, exc)
            return 0, False
        if not deltas:
            return 0, True
        # aggregate per coin type
        agg: dict[str, int] = {}
        for d in deltas:
            t = d.get("coinType") or ""
            agg[t] = agg.get(t, 0) + int(d.get("amount") or 0)
        if not any(agg.values()):
            return 0, True
        fired = 0
        consumed = True
        for row in self._rows:
            if (row.get("wallet") or "").lower() != wallet:
                continue
            # eligibility: ignores pure-gas dust and honors buys/sells preference.
            if not self._balance_eligible(row, agg):
                continue
            result = self._fire_balance(row, wallet, agg, digest, len(deltas))
            if result == "sent":
                fired += 1
            elif result == "failed":
                consumed = False
        return fired, consumed

    def _balance_eligible(self, row: dict, agg: dict) -> bool:
        """Honor `buys` (1=buys, 2=sells, 3=both) and `min_sui`. For a generic
        balance move, 'received'≈buy-sides, 'sent'≈sell-sides; if both present the
        dominant side decides. Pure SUI dust (gas) below min_sui is ignored."""
        want = int(row.get("buys", 3))
        received = sum(a for a in agg.values() if a > 0)
        sent = -sum(a for a in agg.values() if a < 0)
        if want == 1 and sent > received:
            return False
        if want == 2 and received >= sent:
            return False
        min_sui = float(row.get("min_sui", DEFAULT_MIN_SUI))
        if received + sent <= 0:
            return False
        # If only SUI moved and it's dust, skip; token moves always alert.
        sui = agg.get(SUI_COIN_TYPE, 0)
        non_sui = sum(a for t, a in agg.items() if t != SUI_COIN_TYPE and a)
        if not non_sui and abs(sui) < min_sui * (10 ** SUI_DECIMALS):
            return False
        return True

    def _coin_label(self, coin_type: str) -> str:
        """(label, decimals) for a coin type — metadata-cached for 60s."""
        now = time.time()
        hit = self._coin_meta.get(coin_type)
        if hit and now - hit[2] < SYMBOL_TTL_S:
            return hit[0], hit[1]
        label, dec = coin_type, SUI_DECIMALS
        try:
            if coin_type == SUI_COIN_TYPE:
                label, dec = "SUI", SUI_DECIMALS
            else:
                dec = self.ch.coin_decimals(coin_type)
                tail = coin_type.rsplit("::", 2)
                if len(tail) == 3:
                    label = f"{tail[1]}:{tail[2]}"
                else:
                    label = tail[-1]
        except Exception as exc:
            log.debug("coin label failed %s: %s", coin_type, exc)
        self._coin_meta[coin_type] = (label, dec, now)
        return label, dec

    def _fmt_balance(self, coin_type: str, amount: int) -> str:
        label, dec = self._coin_label(coin_type)
        val = amount / (10 ** dec)
        sign = "+" if amount >= 0 else "−"
        return f"{sign}{abs(val):,.4f} {label}"

    def _fire_balance(self, row: dict, wallet: str, agg: dict, digest: str,
                      n_sources: int) -> str:
        """Send one balance alert. Returns "sent", "cooldown" or "failed".

        Tri-state matters: only "failed" may keep the digest above the cursor for
        retry. "cooldown" is a deliberate suppression and counts as consumed.
        """
        ck = (row["tg_uid"], wallet, "balance")
        now = time.time()
        if ck in self._cooldown and now - self._cooldown[ck] < WALLET_COOLDOWN_S:
            return "cooldown"
        tag = f'<a href="tg://user?id={int(row["tg_uid"])}">@{esc_html(row.get("username") or "you")}</a>'
        lines = [self._fmt_balance(t, a) for t, a in sorted(agg.items())]
        head = "received" if sum(agg.values()) > 0 else "sent"
        text = (
            f"💰 {tag} — wallet balance moved\n"
            f"<code>{esc_html(wallet)}</code> <b>{head}</b>\n"
            + "\n".join(lines) +
            f"\n🔗 https://suiscan.xyz/mainnet/tx/{digest}"
        )
        try:
            ok = self.notifier._send(None, int(row["chat_id"]), text)
        except Exception as exc:
            log.warning("tracker bal send failed chat=%s: %s", row["chat_id"], exc)
            return "failed"
        if not ok:
            log.warning("tracker bal send not delivered chat=%s wallet=%s", row["chat_id"], wallet)
            return "failed"
        # Cooldown only after a confirmed delivery, so a failed send is retried
        # instead of being swallowed as "recently fired".
        self._cooldown[ck] = now
        log.info("tracker bal alert chat=%s wallet=%s n=%s coins=%s",
                 row["chat_id"], wallet, n_sources, len(agg))
        return "sent"

    def _handle(self, node: dict, ev_name: str) -> bool:
        j = node.get("json") or {}
        field, amt_field, verb = _SIDE[ev_name]
        wallet = (j.get(field) or "").lower()
        if wallet not in self._wallet_set:
            return False
        key = (ev_name, node.get("seq", 0), node.get("digest", ""))
        if not self._mark_seen(key):
            return False
        amt_atoms = int(j.get(amt_field) or 0)
        amt = amt_atoms / MIST
        curve_id = str(j.get("curve_id") or j.get("id") or "")
        fired = False
        for row in self._rows:
            if row["wallet"].lower() != wallet:
                continue
            if not self._alert_eligible(row, ev_name, amt):
                continue
            if self._fire(row, ev_name, verb, amt, curve_id, node):
                fired = True
                self._event_digests.add((wallet, str(node.get("digest", ""))))
                if len(self._event_digests) > 5000:
                    self._event_digests = set(
                        list(self._event_digests)[-3000:])
        return fired

    def _event_digests_contain(self, wallet: str, digest: str) -> bool:
        return (wallet.lower(), digest) in self._event_digests

    # ---------------- alert ----------------
    def _fire(self, row: dict, ev_name: str, verb: str, amt: float,
              curve_id: str, node: dict) -> bool:
        ck = (row["tg_uid"], row["wallet"], ev_name)
        now = time.time()
        if ck in self._cooldown and now - self._cooldown[ck] < WALLET_COOLDOWN_S:
            return False
        self._cooldown[ck] = now

        symbol, ca = self._symbol_for(curve_id)
        digest = node.get("digest", "")
        cta_id = self._register_cta(
            {"tg_uid": row["tg_uid"], "bot_id": row.get("bot_id"),
             "amount": max(DEFAULT_MIN_SUI, round(amt, 2)), "ca": ca})
        tag = f'<a href="tg://user?id={int(row["tg_uid"])}">@{esc_html(row.get("username") or "you")}</a>'
        text = (
            f"🐟 {tag} — tracked wallet moved\n"
            f"<code>{esc_html(row['wallet'])}</code> "
            f"<b>{verb}</b> <b>{amt:.2f} SUI</b> of <b>{esc_html(symbol)}</b>\n"
            f"🔗 https://suiscan.xyz/mainnet/tx/{digest}\n\n"
            f"⚡ trade it now with @{self.neko_username}: "
            f"<code>@{self.neko_username} buy {amt:.2f} sui {ca}</code>"
        )
        try:
            ok = self.notifier._send(None, int(row["chat_id"]), text,
                                     buttons=[[f"⚡ Buy {amt:.2f} SUI", f"sb:tbuy:{cta_id}"]])
            if ok:
                log.info("tracker alert chat=%s wallet=%s %s %s SUI %s",
                         row["chat_id"], row["wallet"], verb, round(amt, 2), symbol)
            return bool(ok)
        except Exception as exc:
            log.warning("tracker send failed chat=%s: %s", row["chat_id"], exc)
            return False

    def _register_cta(self, payload: dict) -> str:
        import uuid
        cta_id = uuid.uuid4().hex[:8]
        with self._cta_lock:
            if len(self._ctas) > 200:
                self._ctas.clear()
            self._ctas[cta_id] = payload
        return cta_id

    def pop_cta(self, cta_id: str) -> dict | None:
        with self._cta_lock:
            return self._ctas.pop(cta_id, None)

    # ---------------- loop ----------------
    def prime_baseline(self) -> int:
        """Point every wallet cursor at its current newest tx.

        Alerts are meant to be live, not a replay of history. On startup each
        cursor is snapped forward to the newest digest so the first poll has
        nothing above the cursor and stays silent, instead of dumping a backlog
        of old trades into the chat. Returns the number of wallets primed.
        """
        primed = 0
        self._reload()
        for wallet in sorted(self._wallet_set):
            try:
                digests = self.ch.wallet_txs(wallet, first=1)
            except Exception as exc:
                log.warning("tracker prime %s: %s", wallet, exc)
                continue
            if not digests:
                continue
            self.led.set_cursor(f"tx:track:{wallet}", digests[0])
            primed += 1
        log.info("tracker primed %d wallet cursor(s) to newest tx (live-only)", primed)
        return primed

    def run_forever(self) -> None:
        self.prime_baseline()
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.poll_once()
            except Exception:
                log.exception("tracker poll crashed; backoff")
                time.sleep(3)
            self._stop.wait(max(0.0, self.poll_s - (time.time() - t0)))

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.run_forever, name="wallet-tracker", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()


def esc_html(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def start_wallet_tracker(notifier: Notifier | None = None,
                         budget: SendBudget | None = None) -> "WalletTracker | None":
    """Build + start the tracker thread. Returns None when degen is disabled.

    Resolves the real master-bot @username (getMe) so CTAs say @neko, whatever
    the deployed handle actually is. Stores the singleton for the sb:tbuy
    callback handler (get_tracker())."""
    global _TRACKER
    if not env_on():
        return None
    _led = get_ledger()
    _ch = Chain("mainnet")
    master_token = os.getenv("TG_MASTER_TOKEN", "") or os.getenv("TG_BOT_TOKEN", "")
    uname = resolve_neko_username(master_token, "neko_tradesbot")
    try:
        import tg_config as _cfg
        master_token = master_token or _cfg.require_master_token()
    except Exception:
        pass
    if notifier is None:
        notifier = Notifier(None, budget=budget)
    _TRACKER = WalletTracker(_ch, _led, notifier, neko_username=uname)
    _TRACKER.start()
    log.info("wallet tracker started (neko=@%s)", uname)
    return _TRACKER