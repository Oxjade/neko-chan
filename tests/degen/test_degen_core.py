import os
import sys

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "..", "service", "execution"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "service"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "service", "degen"))

import pytest  # noqa: E402

from degen import constants as K  # noqa: E402
from degen.chain import Chain, _digest_to_hex  # noqa: E402
from degen.db import DegenLedger, MAX_BUNDLE_WALLETS  # noqa: E402
from degen import metrics as MX  # noqa: E402
from degen.launchpad import resolve_input, SuipumpLaunchpad  # noqa: E402
from degen import ptb  # noqa: E402
from degen.streamer import SuipumpStreamer, EventBus  # noqa: E402
from degen import validate as V  # noqa: E402


CID = "0x" + "cc" * 32
TOK = "0x" + "dd" * 32 + "::suipump::SUIPUMP"

# ---------------------------------------------------------------- helpers
def curve_json(curve_id, creator, sui_reserve, token_reserve, thr, graduated=False,
               pool_id="", anti_bot=0, sym="AAA", name="A", isv=900000001):
    tok = "0x" + "dd" * 32 + "::suipump::SUIPUMP"
    return {
        "address": curve_id, "version": 5, "digest": "aa" * 32,
        "type": f"{K.SUIPUMP_PKG}::bonding_curve::Curve<{tok}>",
        "shared_version": isv, "owner": "",
        "json": {"id": curve_id, "sui_reserve": sui_reserve,
                 "token_reserve": token_reserve, "creator": creator,
                 "current_grad_threshold": thr, "graduated": graduated, "pool_id": pool_id,
                 "anti_bot_delay": anti_bot, "graduation_target": 0, "version": 1,
                 "paused": False, "symbol": sym, "name": name, "created_at_ms": 1,
                 "active_creator_cap_id": "0x" + "ee" * 32, "price_config_id": K.SUIPUMP_PRICE_CONFIG},
    }


class MockChain(Chain):
    def __init__(self, objects=None, events=None, coins=None):
        super().__init__(post=lambda u, b: {"data": {}})
        self._objects = objects or {}
        self._events = events or {}
        self._coins = coins or {}

    def object(self, address):
        return self._objects.get(address)

    def objects_by_type(self, t, first=20):
        for a, o in self._objects.items():
            if o and o.get("type", "").lower() == t.lower():
                return [{"address": a, "version": o["version"]}]
        return []

    def events(self, t, first=50, after=""):
        return self._events.get(t, []), ("c", len(self._events.get(t, [])))

    def coins(self, owner, coin_type=K.SUI_COIN_TYPE):
        return self._coins.get(owner, [])

    def balance(self, owner, coin_type=K.SUI_COIN_TYPE):
        return sum(c["balance_mist"] for c in self._coins.get(owner, []))

    def gas_price(self):
        return 1000

    def query(self, q, variables=None):
        # coinMetadata default 6 decimals
        if "coinMetadata" in q:
            return {"coinMetadata": {"decimals": 6}}
        return {}


class MockAdapter:
    def __init__(self, address):
        self.address = address
        self.broadcasts = []

    def _broadcast_ptb(self, inputs, commands, gas_price, budget, gas_coin=None):
        self.broadcasts.append({"inputs": inputs, "commands": commands,
                                "budget": budget, "gas_coin": gas_coin})
        return {"digest": "0xdead", "status": "SUCCESS"}


def test_mainnet_sui_coins_prefer_rpc_over_partial_graphql_page():
    """A non-empty GraphQL page can still omit the spendable SUI coin."""
    gql_coin = {"address": "0xgql", "version": 1, "digest": "aa" * 32,
                "contents": {"json": {"balance": "1000"}}}
    rpc_coins = [{"objectId": "0xrpc", "version": 2, "digest": "bb" * 32,
                  "balance_mist": 2_000_000_000}]
    ch = Chain(post=lambda _url, _body: {
        "data": {"address": {"objects": {"nodes": [gql_coin]}}}
    })
    ch._rpc_coins = lambda _owner, _type: rpc_coins

    assert ch.coins("0xowner") == rpc_coins


def test_digest_decoder_handles_base58_without_optional_dependency():
    # 31 zero bytes plus 0xff encode as 31 leading "1"s followed by "5Q".
    assert _digest_to_hex("1" * 31 + "5Q") == "00" * 31 + "ff"


# ---------------------------------------------------------------- constants
def test_constants_are_verified():
    assert K.SUIPUMP_PKG == "0xb205fea41ccedac051bc66498e6ca68cb802c4a6ea06da12e524bed09c80d9b0"
    assert K.SUIPUMP_MODULE == "bonding_curve"
    assert K.BUNDLE_FEE_MIST == 5 * K.MIST
    assert K.PLATFORM_FEE_BPS == 50
    assert K.BLAST_LIVE is False and K.BLAST_PKG == ""


# ---------------------------------------------------------------- resolver
def test_resolve_curve_graduating_pool():
    cid = "0x" + "cc" * 32
    creator = "0x" + "ab" * 32
    ch = MockChain({cid: curve_json(cid, creator, int(8910e9), 1, int(9000e9), graduated=True, pool_id="")})
    st = resolve_input(ch, cid)
    assert st.kind == "graduating" and st.launchpad == "suipump"
    ch2 = MockChain({cid: curve_json(cid, creator, int(8910e9), 1, int(9000e9), graduated=True,
                                     pool_id="0x" + "ff" * 32)})
    st2 = resolve_input(ch2, cid)
    assert st2.kind == "pool" and st2.pool_id == "0x" + "ff" * 32


def test_resolve_wallet_and_unknown():
    ch = MockChain()
    st = resolve_input(ch, "0x" + "12" * 32)
    assert st.kind == "wallet"
    st = resolve_input(ch, "not-an-address")
    assert st.kind == "unknown"
    st = resolve_input(ch, "0x0123::weird::TOKEN")
    assert st.kind == "generic" and "DEX" in st.reasons[0]


def test_resolve_blast_locked_shape():
    # a token type that ISN'T suipump::SUIPUMP and blast is off → generic, not crash
    ch = MockChain()
    st = resolve_input(ch, "0x0123::suipump::SUIPUMP")
    assert st.kind == "unknown" and st.launchpad == "suipump"  # suipump-named but no curve found


# ---------------------------------------------------------------- metrics
def test_curve_fit_recovers():
    vx, vy, tot = 8e12, 2.58e14, 8e14
    C = vx * (tot + vy)
    pts = [(x, int(C / (x + vx) - vy)) for x in (10 ** 8, 2 * 10 ** 9, 9 * 10 ** 9, 4 * 10 ** 11, 9 * 10 ** 12)]
    fit = MX.solve_curve_fit(pts)
    assert fit and fit["fit_err"] < 1e-6
    assert abs(fit["vx_mist"] - vx) / vx < 1e-5


def test_curve_fit_none_below_three_points():
    assert MX.solve_curve_fit([(10 ** 9, 1), (2 * 10 ** 9, 2)]) is None


def test_metrics_curve_price_mcap():
    cid = "0x" + "cc" * 32
    ch = MockChain({cid: curve_json(cid, "0x" + "ab" * 32, 2 * 10 ** 9, 4 * 10 ** 14, int(9000e9))})
    st = resolve_input(ch, cid)
    m = MX.compute(ch, st)
    assert m.price_sui > 0 and m.progress_bps == int(2e9 * 10000 / 9e12)
    assert m.grad_left_sui == 8998.0


# ---------------------------------------------------------------- gauntlet
def test_honeypot_never_false_pass_without_funds():
    cid = "0x" + "cc" * 32
    ch = MockChain({cid: curve_json(cid, "0x" + "ab" * 32, 10 ** 9, 10 ** 14, int(9000e9))},
                   coins={})  # no coins → untested/skip, not pass
    st = resolve_input(ch, cid)
    assert V.honeypot_dryrun(ch, st, "0x" + "ab" * 32) in ("untested", "skipped")


def test_gauntlet_blocks_bad_curve_pkg():
    cid = "0x" + "cc" * 32
    bad = curve_json(cid, "0x" + "ab" * 32, 10 ** 9, 10 ** 14, int(9000e9))
    bad["type"] = "0x" + "99" * 32 + "::bonding_curve::Curve<0x" + "dd" * 32 + "::suipump::SUIPUMP>"
    ch = MockChain({cid: bad})
    st = resolve_input(ch, cid)
    errs = V.curve_validator(ch, st)
    assert any("not a known Suipump" in e for e in errs)


def test_gauntlet_pause_blocks():
    cid = "0x" + "cc" * 32
    j = curve_json(cid, "0x" + "ab" * 32, 10 ** 9, 10 ** 14, int(9000e9))
    j["json"]["paused"] = True
    ch = MockChain({cid: j})
    st = resolve_input(ch, cid)
    assert "curve is paused by creator/admin" in V.curve_validator(ch, st)


# ---------------------------------------------------------------- ptb bytes
def _arg(b):
    return int.from_bytes(b[1:3], "little") if b[0] in (1, 2) else (int.from_bytes(b[1:3], "little"), int.from_bytes(b[3:5], "little"))


def test_buy_ptb_fee_leg_nesting():
    inp, cmds = ptb.build_buy("0x" + "cc" * 32, 200, "0x" + "dd" * 32 + "::suipump::SUIPUMP",
                              ("0x" + "11" * 32, 7, "22" * 32), 999,
                              user_addr="0x" + "ee" * 32, fee_atoms=500,
                              fee_recipient="0x" + "ff" * 32)
    assert len(inp) == 9 and len(cmds) == 4
    assert cmds[1][:1] == b"\x02"          # SplitCoins
    assert _arg(cmds[1][1:6]) == (0, 0)    # nested(0,0)=tokens
    # transfer: tag(1) + vec-len(1) + nested(1,0) + to input7
    assert cmds[2][0] == 1 and cmds[2][1] == 1 and _arg(cmds[2][2:7]) == (1, 0)
    assert _arg(cmds[2][7:10]) == 7


def test_sell_ptb_fee_leg():
    inp, cmds = ptb.build_sell("0x" + "cc" * 32, 100, "0x" + "dd" * 32 + "::suipump::SUIPUMP",
                               ("0x" + "aa" * 32, 5, "bb" * 32), 123, user_addr="0x" + "ee" * 32,
                               fee_sui_mist=1000, fee_recipient="0x" + "ff" * 32)
    assert len(inp) == 7 and len(cmds) == 4
    assert cmds[1][:1] == b"\x02" and cmds[1][1:4] == b"\x02\x00\x00"  # split Result(0)


def test_short_address_padded():
    # Clock 0x6 must encode as 32 bytes
    inp, cmds = ptb.build_buy("0x" + "cc" * 32, 200, "0x" + "dd" * 32 + "::suipump::SUIPUMP",
                              ("0x" + "11" * 32, 7, "22" * 32), 1, user_addr="0x6")
    assert len(inp) == 7 and all(isinstance(i, bytes) for i in inp)


# ---------------------------------------------------------------- streamer
def test_streamer_dedup_and_cursor():
    led = DegenLedger(":memory:")
    bus = EventBus()
    got = []
    bus.on("CurveCreated", lambda e: got.append(e.curve_id))
    pkg = K.SUIPUMP_PKG
    ev_nodes = [{"type": f"{pkg}::bonding_curve::CurveCreated", "sender": "0xa", "seq": 1,
                 "digest": "d1", "json": {"curve_id": "0xc1", "token_type": "", "symbol": "S", "created_at_ms": 1}}]

    class MC(MockChain):
        def events(self, t, first=50, after=""):
            if "CurveCreated" in t and not after:
                return ev_nodes, "cur1", False
            return [], "", False

    s = SuipumpStreamer(MC(), led, bus, packages=(pkg,))
    assert s.poll_once() == 1
    assert s.poll_once() == 0  # dedup via seen + empty page
    assert got == ["0xc1"]


# ---------------------------------------------------------------- ledger rules
def test_bundle_wallet_uniqueness_and_cap():
    led = DegenLedger(":memory:")
    for i in range(MAX_BUNDLE_WALLETS):
        led.add_bundle_wallet(1, f"w{i}", f"0x{i:064d}", b"k", f"h{i}")
    with pytest.raises(ValueError):
        led.add_bundle_wallet(1, "21", "0xdead", b"k", "h999")
    # address reuse across bots rejected
    with pytest.raises(ValueError):
        led.add_bundle_wallet(2, "x", "0x" + "00" * 31 + "1", b"k", "hx")


def test_idempotency_and_fill():
    led = DegenLedger(":memory:")
    oid = led.add_order(1, wallet="0x", intent="buy", otype="market", launchpad="suipump",
                        idempotency_key="same", curve_id="0xc")
    assert oid > 0
    assert led.add_order(1, wallet="0x", intent="buy", otype="market", launchpad="suipump",
                         idempotency_key="same") == -1


# ---------------------------------------------------------------- executor caps
def _mk_exec(led):
    from degen.executor import DegenExecutor
    adapter = MockAdapter("0x" + "ab" * 32)
    # chain that resolves the CID as a live curve + hands back a SUI coin
    ch = MockChain({CID: curve_json(CID, "0x" + "ab" * 32, int(2e9), int(4e14), int(9000e9))},
                   coins={"0x" + "ab" * 32: [
                       {"objectId": "0x" + "11" * 32, "version": 1, "digest": "22" * 32,
                        "balance_mist": 10 ** 10}]})
    ex = DegenExecutor(ch, led, lambda w: (adapter, None), fee_recipient="0x" + "ff" * 32)
    return ex, adapter, ch


def test_executor_caps_per_order():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1, budget_sui=20, caps={"per_order": 1.0})
    ex, _, _ = _mk_exec(led)
    r = ex.buy(1, launchpad="suipump", curve_id=CID, token_type=TOK,
               curve_isv=1, sui_amount=2.0, min_out=1)
    assert not r["ok"] and "per-order" in r["error"]


def test_executor_rejects_non_curve():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1, budget_sui=20)
    ex, ad, _ = _mk_exec(led)
    r = ex.buy(1, launchpad="suipump", curve_id="0x" + "99" * 32, token_type=TOK,
               curve_isv=1, sui_amount=0.5, min_out=1)     # not a known curve
    assert not r["ok"] and "not a live curve" in r["error"]
    assert ad.broadcasts == []                              # never reached the broadcaster


def test_executor_buy_records_real_fill_and_position():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1, budget_sui=20)
    ex, ad, ch = _mk_exec(led)
    r = ex.buy(1, launchpad="suipump", curve_id=CID, token_type=TOK, curve_isv=1,
               sui_amount=0.5, min_out=0)
    assert r["ok"] and r["min_out"] > 0, r          # slippage floor auto-set (§5.2)
    assert len(ad.broadcasts) == 1
    assert led.spend_today(1) == 0.5                # real spend recorded → cap is live
    pos = led.positions(1)
    assert len(pos) == 1 and pos[0]["tokens"] >= 0  # booked from actual balance delta
    fills = led.fills_for(r["order_id"])
    assert fills and fills[0]["sui"] == 0.5


def test_executor_kill_switch():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1, budget_sui=20)
    ex, ad, _ = _mk_exec(led)
    ex.kill(1, True)
    r = ex.buy(1, launchpad="suipump", curve_id=CID, token_type=TOK, curve_isv=1,
               sui_amount=0.1, min_out=1)
    assert not r["ok"] and "kill" in r["error"]
    assert ad.broadcasts == []
    ex.kill(1, False)
    assert not ex.is_killed(1)


def test_executor_needs_no_ai_key():
    # degen is USER-initiated execution — no LLM key required anywhere.
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=0, budget_sui=20)
    ex, _, _ = _mk_exec(led)
    r = ex.buy(1, launchpad="suipump", curve_id=CID, token_type=TOK, curve_isv=1,
               sui_amount=0.1, min_out=0)
    assert r["ok"], r


def test_executor_burst_first_leg_abort():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1, budget_sui=20)

    class FailFirst(MockAdapter):
        def __init__(self, *a):
            super().__init__(*a)
            self.n = 0

        def _broadcast_ptb(self, *a, **k):
            self.n += 1
            self.broadcasts.append(self.n)
            if self.n == 1:
                return {"digest": "0x0", "status": "FAILURE"}
            return {"digest": "0x1", "status": "SUCCESS"}

    from degen.executor import DegenExecutor
    ch = MockChain({CID: curve_json(CID, "0x" + "ab" * 32, int(2e9), int(4e14), int(9000e9))},
                   coins={"0x" + "ab" * 32: [{"objectId": "0x" + "11" * 32, "version": 1,
                                              "digest": "22" * 32, "balance_mist": 10 ** 10}]})
    ad = FailFirst("0x" + "ab" * 32)
    ex = DegenExecutor(ch, led, lambda w: (ad, None))   # no fee_recipient → fee skipped
    legs = [{"wallet": "0x" + "ab" * 32, "amount_sui": 0.5}] * 3
    r = ex.spread_burst(1, launchpad="suipump", curve_id=CID, token_type=TOK,
                        curve_isv=1, total_sui=1.5, min_out_each=0, legs=legs)
    assert r["ok"] is False and "first leg" in r["error"]
    assert len(ad.broadcasts) == 1  # aborted after first failure, no orphan legs


def test_executor_burst_charges_flat_5sui_fee():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=1, budget_sui=20)
    ex, ad, ch = _mk_exec(led)   # _mk_exec sets fee_recipient 0xffff...
    transfers = []

    def fake_transfer(recipient, amount, asset="USDC"):
        transfers.append((recipient, amount, asset))
        return {"ok": True, "digest": "0xfeefee"}
    # the MAIN adapter pays the fee via transfer_asset before legs
    ex._adapter_for = lambda w: (ad, None)
    ad.transfer_asset = fake_transfer
    legs = [{"wallet": "0x" + "ab" * 32, "amount_sui": 1.0}] * 2
    r = ex.spread_burst(1, launchpad="suipump", curve_id=CID, token_type=TOK,
                        curve_isv=1, total_sui=2.0, min_out_each=0, legs=legs)
    assert r.get("ok"), r
    assert transfers and transfers[0][1] == 5.0 and transfers[0][2] == "SUI"  # 5 SUI → fee
    assert r["bundle_fee_sui"] == 5.0


def test_get_runtime_no_deadlock(tmp_path, monkeypatch):
    # Regression: get_runtime() holds the singleton lock while DegenRuntime.__init__
    # re-enters it via get_ledger(). A non-reentrant threading.Lock self-deadlocked
    # the caller (froze the PTB event loop on first dashboard render). Must return.
    import threading
    from degen import runtime as rt
    monkeypatch.setenv("DEGEN_LEDGER_PATH", str(tmp_path / "d.db"))
    rt._SINGLETON.clear()
    box = {}

    def _w():
        box["rt"] = rt.get_runtime()
    t = threading.Thread(target=_w, daemon=True)
    t.start()
    t.join(timeout=15)
    assert "rt" in box, "get_runtime() deadlocked"
    assert box["rt"].network == "mainnet"


def _pool_state():
    from degen.launchpad import AssetState
    return AssetState(kind="pool", launchpad="suipump", curve_id=CID, token_type=TOK,
                      pool_id="0x" + "11" * 32, symbol="SUIFROG")


def test_dex_swap_post_grad_records_fill_and_position():
    import base64
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=0, budget_sui=20)
    ex, ad, ch = _mk_exec(led)
    calls = {"n": 0}

    class FakeSpot:
        def quote_route(self, ci, co, amount_in_atoms=None, slippage_bps=100,
                        external_fee=None, protocols_whitelist=None):
            calls["quote"] = (ci, co, amount_in_atoms, external_fee)
            calls["wl"] = protocols_whitelist
            return {"feeBreakdown": [{"recipient": external_fee["recipient"],
                                      "amount": str(int(amount_in_atoms * 0.005)) + "n"}],
                    "routes": []}
        def swap_tx_b64(self, q, wallet, slip):
            kind = b"\x00" + b"PTBBODY"           # variant tag + raw body
            return {"transaction": base64.b64encode(kind).decode()}

    ex._spot = lambda addr: FakeSpot()
    # reads: SUI pre-flight, token before, token after
    balances = iter([1_000_000_000, 1_000_000, 100_000_000_000])
    ch.balance = lambda addr, ct=None: next(balances)
    ad.broadcasts_raw = []
    ad._broadcast_raw_ptb = lambda body, gp, budget, gas_coin=None: (
        ad.broadcasts_raw.append(body) or {"digest": "0xdex", "status": "SUCCESS"})
    st = _pool_state()
    r = ex.buy_post_grad(1, {"qty_sui": 0.5}, st, side="buy", idem="t1")
    assert r["ok"], r
    assert ad.broadcasts_raw == [b"PTBBODY"]
    fills = led.fills_for(r["order_id"])
    assert fills and fills[0]["tokens"] == 99_999_000_000, fills
    pos = led.positions(1)
    assert pos and pos[0]["entry_sui"] == 0.5
    # integrator fee actually requested on the route
    assert calls["quote"][3] and calls["quote"][3]["feePercentage"] == 0.5
    assert calls["wl"] == ["Cetus"]        # suipump graduates pinned to Cetus


def test_insufficient_balance_says_so_plainly():
    led = DegenLedger(":memory:")
    led.set_config(1, enabled=1, ai_key_ok=0, budget_sui=20)
    ex, ad, ch = _mk_exec(led)
    ch.balance = lambda a, ct=None: 0                # broke wallet
    ch.coins = lambda a, ct=K.SUI_COIN_TYPE: []      # ... every source agrees
    r = ex.buy(1, launchpad="suipump", curve_id=CID, token_type=TOK, curve_isv=1,
               sui_amount=0.5, min_out=0)
    assert r["ok"] is False and r["error"] == "insufficient SUI balance", r
    assert ad.broadcasts == []                        # never reached broadcaster
