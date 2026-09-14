import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service"))

from degen.db import DegenLedger
from degen.ui import DegenUI
from test_degen_core import MockChain, curve_json  # noqa: F401

CID = "0x" + "cc" * 32
DEV = "0x" + "ab" * 32


class Rec:
    def __init__(self):
        self.calls = []

    async def reply_text(self, text, **kw):
        self.calls.append(("reply", text, kw.get("reply_markup")))
        return SimpleNamespace(chat_id=1, message_id=7)

    async def delete(self):
        self.calls.append(("delete",))


class Q:
    def __init__(self, data, msg):
        self.data = data
        self.answered = False
        self.message = msg

    async def answer(self, *a, **k):
        self.answered = True

    async def edit_message_text(self, text, **kw):
        self.message.rec.calls.append(("edit", text, kw.get("reply_markup")))


def _mk(led, bot_id=1):
    ui = DegenUI(MockChain({CID: curve_json(CID, DEV, 2 * 10 ** 9, 4 * 10 ** 14,
                                            int(9000e9), sym="SUICAT", name="Suicat")}),
                 led)
    ui._bot_of = lambda tg: {"id": bot_id, "tg_id": tg, "has_ai_key": 1}
    return ui


class Msg(Rec):
    def __init__(self, text=""):
        super().__init__()
        self.text = text
        self.chat_id = 1
        self.message_id = 7
        self.rec = self          # tests read .rec.calls; Q edits land on the msg


def _msg(text=""):
    return Msg(text)


def _update(msg=None, q=None):
    return SimpleNamespace(effective_user=SimpleNamespace(id=42), message=msg or _msg(),
                           callback_query=q)


# ---------------------------------------------------------------- renderers
def test_degen_view_needs_no_ai_key():
    led = DegenLedger(":memory:")
    ui = _mk(led)
    bot = {"id": 1, "tg_id": 42, "has_ai_key": 0}
    kb = ui.degen_keyboard(bot, led.get_config(1))
    flat = str(kb.inline_keyboard)
    assert "Connect AI Key" not in flat          # user policy: keyless degen
    assert "dg:on" in flat                       # straight to Enable
    assert "Main Dashboard" in flat


def test_degen_view_strip_and_keyboard():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1, budget_sui=12)
    ui = _mk(led)
    bot = {"id": 1, "tg_id": 42, "has_ai_key": 1, "bot_name": "x"}
    strip = ui.degen_strip(bot, led.get_config(1))
    assert "🎰 DEGEN" in strip and "budget" in strip
    kb = ui.degen_keyboard(bot, led.get_config(1))
    flat = str(kb.inline_keyboard)
    for want in ("Suipump", "Blast 🔒", "Both venues", "Buy a meme",
                 "Sniper", "Copy", "Bundle", "KILL", "Main Dashboard", "↻ Refresh"):
        assert want in flat, want
    # positions are NOT a second button/screen: they live in the dashboard's
    # single 📡 POSITIONS space (perp + degen merged by dash()).
    assert "Pos" not in flat
    # main-dashboard rhythm: max 2 buttons per row
    for row in kb.inline_keyboard:
        assert len(row) <= 2, [b.text for b in row]
    # view is a flag, not a message
    assert not ui.degen_on(1)
    ui.enter(1)
    assert ui.degen_on(1)
    ui.exit(1)
    assert not ui.degen_on(1)


def test_card_pre_grad_and_expiry_scheduled():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1)
    ui = _mk(led)
    from degen.launchpad import resolve_input
    st = resolve_input(ui.ch, CID)
    msg = _msg("0x" + "c" * 64)
    update = _update(msg=msg)
    ok = asyncio.get_event_loop().run_until_complete(
        ui.handle_text(update, SimpleNamespace()))
    assert ok is True
    txt = msg.rec.calls[0][1]
    assert "SUICAT" in txt and "LIVE on curve" in txt
    assert "Grad" in txt and "MCap" in txt        # dashboard-style metric block
    assert "untested" not in txt.lower()          # user policy: no honeypot noise
    kb = msg.rec.calls[0][2]
    assert all(len(row) <= 2 for row in kb.inline_keyboard)   # dash rhythm
    assert any(b.text == "🚀 BUY 0.5 SUI" for r in kb.inline_keyboard for b in r)
    # input deleted + card scheduled for 3-min expiry
    assert ("delete",) in msg.rec.calls
    assert led.due_deletes((led._conn.execute("SELECT datetime('now','+250 seconds')").fetchone()[0]))


# ---------------------------------------------------------------- callbacks
def _run(ui, data, led):
    msg = _msg()
    q = Q(data, msg)
    update = _update(q=q, msg=msg)
    asyncio.get_event_loop().run_until_complete(ui.on_cb(update, SimpleNamespace()))
    return msg.rec.calls


def test_cb_enable_via_hub_and_disable():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=0, ai_key_ok=1)
    ui = _mk(led)
    _run(ui, "dg:on", led)
    assert led.get_config(1)["enabled"] == 1 and ui.degen_on(1)
    _run(ui, "dg:off", led)
    assert led.get_config(1)["enabled"] == 0 and not ui.degen_on(1)
    _run(ui, "dg:on", led)
    _run(ui, "dg:main", led)          # back to main WITHOUT disabling
    assert not ui.degen_on(1) and led.get_config(1)["enabled"] == 1


def test_cb_kill_switch():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1)
    ui = _mk(led)
    _run(ui, "dg:kill", led)
    assert (led.get_config(1)["caps"] or {}).get("killed") is True
    _run(ui, "dg:unkill", led)
    assert not (led.get_config(1)["caps"] or {}).get("killed")


def test_cb_venue_chips():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1)
    ui = _mk(led)
    calls = _run(ui, "dg:lp:suipump", led)
    assert led.get_config(1)["launchpads"] == "suipump"
    _run(ui, "dg:lp:blast", led)                   # locked, never trades
    assert led.get_config(1)["launchpads"] == "suipump"


def test_cb_watch_and_copy_verbs():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1)
    ui = _mk(led)
    ref = ui._ref(1, "wallet", DEV)
    _run(ui, f"dg:watch:{ref}", led)
    assert led.watched_deployers(1)
    ref2 = ui._ref(1, "wallet", DEV)
    _run(ui, f"dg:copyadd:{ref2}", led)
    assert led.tracked_wallets(1)


def test_sections_render():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1)
    led.watch_deployer(1, DEV)
    led.track_wallet(1, DEV)
    led.add_order(1, wallet="", intent="buy", otype="limit", launchpad="suipump",
                  idempotency_key="s1", curve_id=CID, target_price=1e-8, qty_sui=0.5)
    ui = _mk(led)
    for sec in ("pos", "orders", "sniper", "copy", "bundle", "risk"):
        calls = _run(ui, f"dg:{sec}", led)
        assert calls and calls[-1][1], sec
    assert any("#1" in c[1] or "buy" in c[1] for c in
               [x for x in _run(ui, "dg:orders", led) if x[0] == "edit"])


# ---------------------------------------------------------------- PTB-real guards
def test_no_phantom_ptb_methods_in_ui():
    """CallbackQuery.edit_text does not exist in PTB — using it crashes every
    degen callback with AttributeError (observed live 2026-09-14: 'no response')."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                            "service", "degen", "ui.py")).read()
    assert "q.edit_text(" not in src
    assert ".edit_text(" not in src.replace("edit_message_text(", "")


def test_all_degen_buttons_are_callbacks():
    """B() must bind the 2nd positional to callback_data — not InlineKeyboardButton's
    `url`, which Telegram rejects (BadRequest → hub silently fails to render)."""
    from telegram import InlineKeyboardButton
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=0, ai_key_ok=1)
    ui = _mk(led)
    bot = {"id": 1, "tg_id": 42, "has_ai_key": 1}
    kb_off = ui.degen_keyboard(bot, led.get_config(1))
    kb_hub = ui._hub_kb()
    rows = list(kb_off.inline_keyboard) + list(kb_hub.inline_keyboard)
    assert any(b.callback_data == "dg:on" for r in rows for b in r)
    for r in rows:
        for b in r:
            assert isinstance(b, InlineKeyboardButton)
            assert b.url is None, f"url button leaked: {b.text} -> {b.url}"
            assert b.callback_data, f"button has no callback_data: {b.text}"


def test_card_buttons_callbacks_and_cbdata_limit():
    """Token card chips must be callback buttons AND fit Telegram's 64-byte cap."""
    import asyncio
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1)
    ui = _mk(led)
    from degen.launchpad import resolve_input
    st = resolve_input(ui.ch, CID)
    _, kb = asyncio.get_event_loop().run_until_complete(
        ui.card({"id": 1, "tg_id": 42}, led.get_config(1), st))
    seen = 0
    for row in kb.inline_keyboard:
        for b in row:
            assert b.url is None, f"url leaked: {b.text}"
            assert b.callback_data, f"no callback_data: {b.text}"
            assert len(b.callback_data.encode()) <= 64, b.callback_data
            seen += 1
    assert seen >= 8  # chips + slippage + buy + burst + hub


def test_slip_and_amt_chips_persist_and_repaint():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1)
    ui = _mk(led)
    ref = ui._ref(1, "curve", CID)
    calls = _run(ui, f"dg:slp:{ref}:25", led)
    caps = led.get_config(1)["caps"]
    assert caps["_slip_" + ref] == "25"
    assert calls and calls[-1][0] == "edit" and "MCap" in calls[-1][1]
    assert any(b.text == "✓ 25%" for r in calls[-1][2].inline_keyboard for b in r)
    calls = _run(ui, f"dg:amt:{ref}:1", led)
    assert led.get_config(1)["caps"]["_amt_" + ref] == "1"
    assert any(b.text == "✓ 1 SUI" for r in calls[-1][2].inline_keyboard for b in r)


def test_card_graduated_pool_gets_full_buy_dashboard():
    """SUIFROG fix v2: a graduated token shows the SAME buy dashboard as curve
    tokens (chips + BUY + slippage), minus curve-only extras (bundled burst).
    No 'live soon' text, no 99%/threshold math, no false dev-hold block."""
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1)
    ui = _mk(led)
    from degen.launchpad import AssetState
    from degen.validate import run_gauntlet
    st = AssetState(kind="pool", launchpad="suipump", curve_id=CID,
                    token_type="0x" + "ee" * 32 + "::suifrog::SUIFROG",
                    pool_id="0x" + "11" * 32, creator=DEV, symbol="SUIFROG",
                    sui_reserve_mist=8_910_000_000_000, token_reserve=0,
                    grad_threshold_mist=9_000_000_000_000)
    g = run_gauntlet(ui.ch, st)
    assert not any("dev holds" in b for b in g.blocks), g.blocks
    txt, kb = asyncio.get_event_loop().run_until_complete(
        ui.card({"id": 1, "tg_id": 42}, led.get_config(1), st))
    assert "graduated" in txt.lower() and "live soon" not in txt.lower()
    assert g.allowed
    flat = str(kb.inline_keyboard)
    assert "dg:buy" in flat and "dg:amt" in flat        # full controls
    assert "dg:burst" not in flat                       # no curve-bundle on DEX
    rows = [r for r in kb.inline_keyboard]
    assert any(b.text.startswith("🚀 BUY") for r in rows for b in r)


def test_x_sui_custom_amount_flow():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1)
    ui = _mk(led)
    ref = ui._ref(1, "curve", CID)
    # button label + callback exist on the card
    from degen.launchpad import resolve_input
    st = resolve_input(ui.ch, CID)
    _, kb = asyncio.get_event_loop().run_until_complete(
        ui.card({"id": 1, "tg_id": 42}, led.get_config(1), st, ref=ref))
    btns = [b for r in kb.inline_keyboard for b in r]
    assert any(b.text == "✏️ X SUI" and b.callback_data == f"dg:cust:{ref}" for b in btns)
    # tap it -> ask state set
    _run(ui, f"dg:cust:{ref}", led)
    assert led.get_config(1)["caps"]["_ask_amt"] == ref
    # numeric reply -> stored as the selected amount, ask cleared
    msg = _msg("0.75 sui")
    upd = _update(msg=msg)
    handled = asyncio.new_event_loop().run_until_complete(
        ui.handle_text(upd, SimpleNamespace()))
    assert handled is True
    caps = led.get_config(1)["caps"]
    assert caps.get("_amt_" + ref) == "0.75" and "_ask_amt" not in caps
    # cancel clears the ask
    _run(ui, f"dg:cust:{ref}", led)
    _run(ui, "dg:custx", led)
    assert "_ask_amt" not in (led.get_config(1)["caps"] or {})


def test_track_works_in_view_mode_and_shows_list():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=0, ai_key_ok=1)     # NOT enabled, only view
    ui = _mk(led)
    ui.enter(1)
    msg = _msg(f"Track {DEV}")
    upd = _update(msg=msg)
    assert asyncio.get_event_loop().run_until_complete(
        ui.handle_text(upd, SimpleNamespace())) is True
    assert any(t[0] == "track_wallet" or (t[0] == "reply" and "COPY" in t[1])
               for t in msg.rec.calls), msg.rec.calls
    assert led.tracked_wallets(1)               # list shows right in the reply
    txt = [t for t in msg.rec.calls if t[0] == "reply"][0][1]
    assert DEV[:10] in txt                      # the wallet itself is on screen


def test_strip_shows_balance_and_pnl_and_keyboard_has_pnl():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1, budget_sui=10)
    ui = _mk(led)
    ui._wallet_addr = lambda bot: "0x" + "cd" * 32
    ui.ch.balance = lambda a, ct=None: 2_500_000_000      # 2.5 SUI
    led.upsert_position(1, "suipump", CID, "0x" + "ee" * 32 + "::t::T",
                        "0xw", add_sui=1.0, add_tokens=int(1e9))
    strip = ui.degen_strip({"id": 1}, led.get_config(1))
    assert "P&L" in strip and "2.50 SUI" in strip and "balance" in strip
    flat = str(ui.degen_keyboard({"id": 1}, led.get_config(1)).inline_keyboard)
    assert "dg:pnl" in flat and "sb:rewards" in flat
    calls = _run(ui, "dg:pnl", led)
    assert "DEGEN P&L" in calls[-1][1] and "marked" in calls[-1][1]


def test_generic_token_card_is_dex_routed_not_dead():
    """A non-Suipump type must: badge as DEX token (not 'generic token'), fill
    price/mcap from the Aftermath probe, and carry working buy controls."""
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1)
    ui = _mk(led)
    def fake_quote(tok, atoms):
        return {"routes": [{"paths": [{
            "protocolName": "Cetus",
            "poolMetadata": {"tbData": {"protocol": "Cetus"}},
            "coinOut": {"amount": "10000000000n"}}]}]}
    ui._af_quote = fake_quote
    ui.ch.object = lambda a: {"type": "pkg", "json": {}}          # pkg exists
    ui.ch.objects_by_type = lambda t, first=5: []                 # no TreasuryCap
    from degen.launchpad import AssetState
    st = AssetState(kind="generic", launchpad="generic",
                    token_type="0x" + "ee" * 32 + "::foo::FOO")
    ui.ch.balance = lambda a, ct=None: 0
    txt, kb = asyncio.get_event_loop().run_until_complete(
        ui.card({"id": 1, "tg_id": 42}, led.get_config(1), st))
    assert "DEX token" in txt and "generic token" not in txt
    assert "Route" in txt and "Cetus" in txt
    flat = str(kb.inline_keyboard)
    assert "dg:buy" in flat                       # live button, not a dead end
    price_line = [l for l in txt.splitlines() if "Price" in l][0]
    assert "0.00005 SUI" in price_line           # 0.5 SUI / 10,000 tok from probe
