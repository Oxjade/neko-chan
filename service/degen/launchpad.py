"""Launchpad venue adapters (docs §2.1) — the blast-ready seam.

`resolve_input` implements §6.3a steps 1-3 (normalize → identify → classify).
Suipump = live (verified §0). Blast = COMING_SOON: returns a locked state, trades
nothing, hardcodes nothing (§7.1 probe obligation). Non-launchpad but
Aftermath-routable CAs resolve to GENERIC (§6.3b) — tradable regardless of chip.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests

from . import constants as K
from .chain import Chain

log = logging.getLogger(__name__)

HEX_ADDR_RE = re.compile(r"(?i)^0x[0-9a-f]{1,64}$")


def _pad_addr(addr: str) -> str:
    core = addr[2:] if addr.startswith("0x") else addr
    return "0x" + core.zfill(64)


def _pad_type(t: str) -> str:
    """Pad every embedded 0x… address to full 32-byte width (GraphQL strict-match).
    {1,64} then zfill(64): leaves correct 64-char addresses untouched, pads short ones."""
    return re.sub(r"0x([0-9a-fA-F]{1,64})(?![0-9a-fA-F])",
                  lambda m: "0x" + m.group(1).zfill(64), t)


def _jint(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


# ----------------------------------------------------------------- data
@dataclass
class AssetState:
    kind: str                    # curve|graduating|pool|generic|wallet|blast_locked|unknown
    launchpad: str               # suipump|blast|generic|''
    token_type: str = ""         # padded full Move type
    curve_id: str = ""
    curve_obj: dict = field(default_factory=dict)   # Chain.object() result
    pool_id: str = ""
    creator: str = ""
    symbol: str = ""
    name: str = ""
    icon_url: str = ""
    sui_reserve_mist: int = 0
    token_reserve: int = 0
    grad_threshold_mist: int = 0
    progress_bps: int = 0
    reasons: list[str] = field(default_factory=list)
    # extras read straight off the curve contents (§3.1a / §5.3 inputs)
    curve_version: int = 0
    anti_bot_delay: int = 0
    graduation_target: int = 0
    paused: bool = False
    creator_cap_id: str = ""
    created_at_ms: int = 0


_IDX_DOWN = 0.0   # unix ts of last indexer failure (60s cool-down)


def _fill_metadata(ch: Chain, st: "AssetState") -> None:
    """Best-effort symbol/name for a token type (coinMetadata, keyless)."""
    if not st.token_type:
        return
    try:
        r = ch.query('{ coinMetadata(coinType: "%s") { symbol name decimals } }'
                     % st.token_type)
        cm = (r or {}).get("coinMetadata") or {}
        st.symbol = str(cm.get("symbol") or "")[:16]
        st.name = str(cm.get("name") or "")[:40]
    except Exception:
        pass


# ----------------------------------------------------------------- interface
class Launchpad:
    id = ""
    live = False

    def packages(self) -> tuple[str, ...]:
        raise NotImplementedError

    def curve_type_for(self, token_type: str) -> str:
        raise NotImplementedError

    def is_own_token_type(self, type_str: str) -> bool:
        raise NotImplementedError

    def build_state(self, ch: Chain, curve_obj: dict) -> AssetState:
        raise NotImplementedError


class SuipumpLaunchpad(Launchpad):
    id = "suipump"
    live = True

    def packages(self):
        return K.SUIPUMP_PACKAGES

    def curve_type_for(self, token_type: str) -> str:
        # curve objects exist under BOTH published packages — callers iterate.
        return "Curve<" + _pad_type(token_type) + ">"

    def is_own_token_type(self, type_str: str) -> bool:
        return type_str.endswith(K.SUIPUMP_TOKEN_TYPE_SUFFIX)

    def find_curve(self, ch: Chain, token_type: str) -> dict | None:
        tok = _pad_type(token_type)
        for pkg in self.packages():
            for f in ch.objects_by_type(f"{pkg}::{K.SUIPUMP_MODULE}::Curve<{tok}>"):
                o = ch.object(f["address"])
                if o:
                    return o
        return None

    def build_state(self, ch: Chain, curve_obj: dict) -> AssetState:
        j = curve_obj.get("json") or {}
        m = re.match(r"(?i)^0x[0-9a-f]+::bonding_curve::Curve<(.+)>$", curve_obj.get("type", ""))
        token_type = _pad_type(m.group(1)) if m else ""
        sui_reserve = _jint(j.get("sui_reserve"))
        tok_reserve = _jint(j.get("token_reserve"))
        # real on-chain field names (verified from curve contents):
        thr = _jint(j.get("current_grad_threshold"))
        graduated = bool(j.get("graduated"))
        pool_id = str(j.get("pool_id") or "")
        st = AssetState(
            kind="curve", launchpad="suipump", token_type=token_type,
            curve_id=curve_obj["address"], curve_obj=curve_obj,
            pool_id=_pad_addr(pool_id) if pool_id else "",
            creator=_pad_addr(str(j.get("creator", ""))),
            sui_reserve_mist=sui_reserve, token_reserve=tok_reserve,
            grad_threshold_mist=thr,
            curve_version=_jint(j.get("version")),
            anti_bot_delay=_jint(j.get("anti_bot_delay")),
            graduation_target=_jint(j.get("graduation_target")),
            paused=bool(j.get("paused")),
            creator_cap_id=str(j.get("active_creator_cap_id") or ""),
            created_at_ms=_jint(j.get("created_at_ms")),
        )
        st.symbol = str(j.get("symbol") or "")[:16]
        st.name = str(j.get("name") or "")[:40]
        if graduated:
            st.kind = "pool" if st.pool_id else "graduating"
        if thr > 0:
            st.progress_bps = min(10000, int(sui_reserve * 10000 / thr))
        # metadata via indexer API (names/icons) — creator-supplied labels, never
        # pricing. The indexer is a nicety (free-tier render app): short timeout,
        # 60s failure cool-down, DEGEN_INDEXER=0 to disable (tests).
        import os as _os, time as _t
        _idx = _os.getenv("DEGEN_INDEXER", K.SUIPUMP_INDEXER)
        global _IDX_DOWN
        if _idx and _idx != "0" and _t.time() - _IDX_DOWN > 60:
          try:
            r = requests.get(f"{_idx}/token/{curve_obj['address']}", timeout=2.5)
            if r.status_code == 200:
                d = r.json() or {}
                st.symbol = st.symbol or str(d.get("symbol") or "")[:16]
                st.name = st.name or str(d.get("name") or "")[:40]
                st.icon_url = str(d.get("iconUrl") or d.get("icon_url") or "")
                stats = d.get("stats") or {}
                if not st.grad_threshold_mist and stats.get("grad_threshold_sui"):
                    st.grad_threshold_mist = int(float(stats["grad_threshold_sui"]) * K.MIST)
                if not st.sui_reserve_mist and stats.get("reserve_sui"):
                    st.sui_reserve_mist = int(float(stats["reserve_sui"]) * K.MIST)
                if st.grad_threshold_mist > 0:
                    st.progress_bps = min(10000, int(st.sui_reserve_mist * 10000 / st.grad_threshold_mist))
                pid = str(d.get("poolId") or "")
                if pid and not st.pool_id:
                    st.pool_id = _pad_addr(pid)
                    if st.kind == "graduating":
                        st.kind = "pool"
          except Exception:
            _IDX_DOWN = _t.time()
        return st


class BlastLaunchpad(Launchpad):
    """§7.1 — NOT wired until the on-chain probe ships. Resolves blast-looking
    inputs to `blast_locked` honestly instead of guessing packages."""
    id = "blast"
    live = bool(K.BLAST_LIVE and K.BLAST_PKG)

    def packages(self):
        return (K.BLAST_PKG,) if K.BLAST_PKG else ()

    def curve_type_for(self, token_type: str) -> str:
        return ""

    def is_own_token_type(self, type_str: str) -> bool:
        return bool(K.BLAST_PKG) and type_str.startswith(_pad_addr(K.BLAST_PKG))

    def build_state(self, ch: Chain, curve_obj: dict) -> AssetState:
        raise RuntimeError("blast not wired — run the §7.1 probe first")


REGISTRY: list[Launchpad] = [SuipumpLaunchpad(), BlastLaunchpad()]


def active_launchpads() -> list[Launchpad]:
    return [lp for lp in REGISTRY if lp.live]


# ----------------------------------------------------------------- resolver
def _extract_id(raw: str) -> str:
    s = (raw or "").strip().split()[0]
    m = re.search(r"(?i)suipump\.[a-z]+/(?:token|curve|coin|p)/([0-9a-zA-Z_.:-]+)", s)
    if m:
        return m.group(1)
    if s.startswith("http") and "0x" in s:
        return "0x" + s.split("0x", 1)[1].split("/")[0]
    return s


def resolve_input(ch: Chain, raw: str) -> AssetState:
    """§6.3a: accept type string / object id / URL / wallet address → classify."""
    s = _extract_id(raw)
    if not s:
        return AssetState(kind="unknown", launchpad="", reasons=["empty input"])

    # ---- Move type string ----
    if "::" in s:
        for lp in REGISTRY:
            if lp.is_own_token_type(s):
                if not lp.live:
                    return AssetState(kind="blast_locked", launchpad=lp.id, token_type=_pad_type(s))
                o = lp.find_curve(ch, s) if hasattr(lp, "find_curve") else None
                if o:
                    return lp.build_state(ch, o)
                return AssetState(kind="unknown", launchpad=lp.id, token_type=_pad_type(s),
                                  reasons=["token type has no live curve — was it launched here?"])
        # unknown type → DEX-routed token (Aftermath probe at card level). Pull
        # its real symbol/name so the UI can show WHAT it is, not what it isn't.
        st = AssetState(kind="generic", launchpad="generic", token_type=_pad_type(s),
                        reasons=["routed via DEX aggregators"])
        _fill_metadata(ch, st)
        return st

    if not HEX_ADDR_RE.match(s):
        return AssetState(kind="unknown", launchpad="", reasons=["unrecognized input"])
    addr = _pad_addr(s)

    # ---- object id? ----
    o = ch.object(addr)
    if o and o.get("type"):
        tr = o["type"]
        for lp in active_launchpads():
            if "::bonding_curve::Curve<" in tr:
                return lp.build_state(ch, o)
            if "::pool::Pool<" in tr:
                m = re.match(r"(?i)^.*::pool::Pool<([^,]+),", tr)
                tok = _pad_addr(m.group(1)) if m else ""
                curve = lp.find_curve(ch, tok) if (tok and hasattr(lp, "find_curve")) else None
                st = AssetState(kind="pool", launchpad=lp.id, token_type=tok,
                                curve_id=curve["address"] if curve else "",
                                pool_id=addr)
                if curve:
                    cs = lp.build_state(ch, curve)
                    st.creator, st.sui_reserve_mist = cs.creator, cs.sui_reserve_mist
                    st.grad_threshold_mist, st.progress_bps = cs.grad_threshold_mist, cs.progress_bps
                    st.symbol, st.name, st.icon_url = cs.symbol, cs.name, cs.icon_url
                return st
        # token package id / metadata object pasted instead of the type
        mmt = re.match(r"(?i)^0x2::coin::CoinMetadata<(.+)>$", tr)
        if mmt:                       # pasted the metadata object → recover type
            st = AssetState(kind="generic", launchpad="generic",
                            token_type=_pad_type(mmt.group(1)),
                            reasons=["routed via DEX aggregators"])
            _fill_metadata(ch, st)
            return st
        guess = f"{addr}::suipump::SUIPUMP"
        for lp in active_launchpads():
            if hasattr(lp, "find_curve"):
                curve = lp.find_curve(ch, guess)
                if curve:
                    return lp.build_state(ch, curve)
        return AssetState(kind="unknown", launchpad="",
                          reasons=["paste the full token type (0x…::pkg::TOK) — "
                                   "that object isn't a curve, pool or metadata"])

    # ---- not an object → wallet address ----
    return AssetState(kind="wallet", launchpad="", creator=addr)


def curve_pkg(curve_type_repr: str) -> str:
    """Which published package a curve belongs to (for ABI dispatch)."""
    m = re.match(r"(?i)^(0x[0-9a-f]+)::", curve_type_repr or "")
    return _pad_addr(m.group(1)) if m else ""
