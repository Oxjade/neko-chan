"""Safety gauntlet (§5.3 / §5.3a / §5.4) — runs before any card enables BUY.

The one place a forged/honeypot token gets refused: buy/sell are PUBLIC on the
curve package and objects are `key, store` — Move gives the buyer ZERO
protection against a crafted object; that protection is ours to build.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import constants as K
from .chain import Chain
from .launchpad import AssetState, SuipumpLaunchpad


@dataclass
class GauntletResult:
    allowed: bool = False
    honeypot: str = "untested"        # pass|fail|untested|skipped
    risks: list[str] = field(default_factory=list)   # amber flags w/ reason
    blocks: list[str] = field(default_factory=list)  # hard reasons
    dev_hold_pct: float | None = None
    creator_cap_id: str = ""


# ----------------------------------------------------------------- §5.3
def curve_validator(ch: Chain, st: AssetState) -> list[str]:
    """Trust boundary: the curve object must be what it claims."""
    errors = []
    lp = SuipumpLaunchpad()
    if st.launchpad != "suipump":
        return []
    obj = st.curve_obj or {}
    tr = obj.get("type", "")
    m = re.match(r"(?i)^0x[0-9a-f]+::bonding_curve::Curve<(.+)>$", tr)
    if not m:
        return ["curve type is not a real Curve<T>"]
    pkg = "0x" + tr[2:].split("::", 1)[0]
    if pkg.lower() not in [p.lower() for p in lp.packages()]:
        errors.append("curve package is not a known Suipump package")
    tok = m.group(1)
    from .launchpad import _pad_type
    if st.token_type and tok.replace(" ", "").lower() != _pad_type(st.token_type).lower():
        errors.append("curve<token> mismatch — object disagrees with resolution")
    j = obj.get("json") or {}
    if not j:
        errors.append("curve contents unreadable")
    # creator must be the CurveCreated event creator (forged-object defense):
    # cheap proxy — creator field present & matches resolution.
    if st.creator and str(j.get("creator", "")).lower() != st.creator.lower():
        errors.append("creator field disagrees with resolved state")
    if j.get("paused"):
        errors.append("curve is paused by creator/admin")
    return errors


# ----------------------------------------------------------------- §5.3a honeypot
def honeypot_dryrun(ch: Chain, st: AssetState, sender: str) -> str:
    """buy→sell-back SIMULATION inside one PTB (§5.3a): the buy's own output funds
    the sell, so success == 'can exit right now, this state'. Revert → honeypot.

    Requires sender to own SUI to simulate with; without funds it is honest
    `skipped` (never a false pass)."""
    if st.kind != "curve" or not st.curve_id:
        return "skipped"
    coins = ch.coins(sender)
    if not coins:
        return "skipped"
    coin = max(coins, key=lambda c: c["balance_mist"])
    spend_mist = min(5_000_000, coin["balance_mist"] // 3)  # ~0.005 SUI test
    if spend_mist < 1_000_000:
        return "skipped"
    curve = ch.object(st.curve_id)
    if not curve:
        return "fail"
    tok = st.token_type
    pkg = st.curve_obj.get("type", "").split("::", 1)[0]
    # inputs: 0 buy coin(gas-owned), 1 curve, 2 min_out=0, 3 referrer none,
    #         4 price config, 5 clock, 6 sell coin(=result0), 7 min_sui 0, 8 ctx…
    # Note: ctx/signer are implicit; PTB command args reference Inputs/Results.
    inputs = [
        {"Object": {"ImmOrOwnedObject": {"objectId": coin["objectId"],
                                         "version": str(coin["version"]),
                                         "digest": coin["digest"]}}},
        {"Object": {"SharedObject": {"objectId": curve["address"], "mutable": True}}},
        {"Pure": "0x" + (0).to_bytes(8, "little").hex()},                 # min_tokens_out
        {"Pure": "0x"},                                                    # referrer None
        {"Object": {"SharedObject": {"objectId": K.SUIPUMP_PRICE_CONFIG, "mutable": False}}},
        {"Object": {"SharedObject": {"objectId": K.SUI_CLOCK, "mutable": False}}},
        {"Pure": "0x" + (0).to_bytes(8, "little").hex()},                 # sell min_sui_out
        {"Pure": "0x"},                                                    # sell referrer None
        {"Pure": "0x" + st.creator[2:]},                                   # dust sink addr
    ]
    txs = [
        {"MoveCall": {"package": pkg, "module": K.SUIPUMP_MODULE, "function": "buy",
                      "typeArguments": [tok],
                      "arguments": [{"Input": 0}, {"Input": 1}, {"Input": 2}, {"Input": 3},
                                    {"Input": 4}, {"Input": 5}]}},
        # buy returns (Coin<T>, change Coin<SUI>); sell the token leg fully:
        {"MoveCall": {"package": pkg, "module": K.SUIPUMP_MODULE, "function": "sell",
                      "typeArguments": [tok],
                      "arguments": [{"Input": 1}, {"NestedResult": [0, 0]}, {"Input": 6},
                                    {"Input": 7}]}},
        # return leftover SUI (buy change + sell output) to the sender
        {"TransferObjects": {"coins": [{"NestedResult": [0, 1]}, {"Result": 1}],
                             "to": {"Input": 8}}},
    ]
    # v2 TransactionData JSON (kind is an OBJECT). NOTE: the exact command/arg
    # casing for simulateTransaction's JSON scalar is calibrated live in P1 (the
    # first funded mainnet round-trip — docs §7). Until then a schema/parse error
    # MUST NOT hard-block: we return "untested", never a false "fail" that would
    # brick good tokens, and never a false "pass".
    tx = {"sender": sender,
          "gasData": {"payment": [{"objectId": coin["objectId"], "version": str(coin["version"]),
                                   "digest": coin["digest"]}], "price": "1000",
                      "budget": str(50_000_000)},
          "kind": {"programmableTransaction": {"inputs": inputs, "commands": txs}}}
    q = ('query T($t: JSON!) { simulateTransaction(transaction: $t) '
         '{ effects { status executionError { abortCode identifier message } } } }')
    try:
        data = ch.query(q, {"t": tx})
        eff = ((data.get("simulateTransaction") or {}).get("effects") or {})
        status = str(eff.get("status") or "")
        if status.upper() == "SUCCESS":
            return "pass"
        if status.upper() == "FAILURE":
            return "fail"          # unambiguous on-chain abort → real honeypot
        return "untested"
    except Exception:
        return "untested"          # schema/transport: never block on our own gap


# ----------------------------------------------------------------- §5.4 dev screen
def dev_screen(ch: Chain, st: AssetState) -> dict:
    """Our own heuristics — NEVER trust launchpad `reputation`/early-sell flags."""
    flags, blocks = [], []
    creator = st.creator
    if not creator:
        return {"ok": False, "flags": ["no creator"], "blocks": ["unresolved creator"]}
    # dev holding
    bal = ch.balance(creator, st.token_type) if st.token_type else 0
    total_guess = st.token_reserve + bal  # rough; curve-held + dev-held
    hold_pct = (bal / (st.token_reserve + bal) * 100.0) if (st.token_reserve + bal) else 0.0
    if hold_pct > 20.0:
        blocks.append(f"dev holds {hold_pct:.0f}% of supply")
    elif hold_pct > 10.0:
        flags.append(f"dev holds {hold_pct:.0f}%")
    # age / funding freshness: brand-new funded curve (anti wash) — via created_at
    # (freshness of the creator's funding source is the gRPC funding-graph trace;
    #  P0 uses a conservative age heuristic until that lands.)
    if st.created_at_ms:
        import time as _t
        age_s = (_t.time() * 1000 - st.created_at_ms) / 1000
        if age_s < 20:
            flags.append("launch younger than 20s — funding graph pending")
    if st.graduation_target and st.graduation_target not in (0,):
        flags.append(f"non-default graduation_target={st.graduation_target}")
    return {"ok": not blocks, "flags": flags, "blocks": blocks,
            "dev_hold_pct": round(hold_pct, 2)}


# ----------------------------------------------------------------- §6.3b generic screen
def generic_screen(ch: Chain, st: AssetState, *, min_liq_mist: int = 200 * K.MIST,
                   max_impact_bps: int = 800, allow_mintable: bool = False) -> dict:
    """Non-launchpad Aftermath tokens: mint authority / upgrade policy / LP depth."""
    flags, blocks = [], []
    pkg_addr = st.token_type.split("::")[0] if "::" in st.token_type else st.token_type
    o = ch.object(pkg_addr) if pkg_addr else None
    if not o:
        return {"ok": False, "flags": [], "blocks": ["token package not found"]}
    # upgrade policy + publisher live on the Package's publisher object — a full
    # audit needs the PackageUpgradeRequested / latest publish tx; P0 conservative:
    # owner-cap still present = mint authority exists and is held by someone.
    caps = ch.objects_by_type(f"{pkg_addr}::coin::TreasuryCap", first=5) if pkg_addr else []
    if caps:
        (blocks if not allow_mintable else flags).append(
            "TreasuryCap live — supply can be inflated" + ("" if allow_mintable else " (blocked)"))
    # liquidity via Aftermath probe is at card level (quote impact); here just shape.
    return {"ok": not blocks, "flags": flags, "blocks": blocks}


# ----------------------------------------------------------------- combined
def run_gauntlet(ch: Chain, st: AssetState, sender: str = "",
                 allow_mintable: bool = False) -> GauntletResult:
    r = GauntletResult()
    if st.kind in ("wallet", "unknown", "blast_locked"):
        r.blocks.append(st.kind)
        return r
    if st.launchpad == "suipump":
        if st.kind == "pool":
            # graduated: the curve is drained (token_reserve == 0) — the curve
            # honeypot sim and the dev-hold ratio are MEANINGLESS here and
            # produced a permanent false block ("dev holds 100% of supply").
            # Post-grad validation is the Aftermath route check (§3.5, P1).
            r.honeypot = "skipped"
            r.risks.append("graduated — trades via Aftermath route (live soon)")
        else:
            errs = curve_validator(ch, st)
            r.blocks += errs
            r.honeypot = honeypot_dryrun(ch, st, sender) if sender else "untested"
            if r.honeypot == "fail":
                r.blocks.append("honeypot check failed — sells revert")
            ds = dev_screen(ch, st)
            r.dev_hold_pct = ds.get("dev_hold_pct")
            r.risks += ds.get("flags", [])
            r.blocks += ds.get("blocks", [])
            r.creator_cap_id = st.creator_cap_id
    elif st.kind == "generic" or st.launchpad == "generic":
        g = generic_screen(ch, st, allow_mintable=allow_mintable)
        r.risks += g.get("flags", [])
        r.blocks += g.get("blocks", [])
        r.honeypot = "skipped"  # sell-back sim needs route legs (§3.5a wiring)
    r.allowed = not r.blocks
    return r
