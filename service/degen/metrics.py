"""Metrics engine (docs §3.0): price, mcap, graduation — keyless reads only.

All inputs come from Chain object reads + our own event stream. Verified live:
Suipump tokens are 6-decimals on mainnet (NOT testnet's 9) — decimals are read per
token type, never assumed. Total supply is captured per curve from the first
observed reserves (curve starts holding the whole supply).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .chain import Chain, GqlError
from .launchpad import AssetState

DEFAULT_SUI_USD_FALLBACK = 1.0  # used only when the on-chain oracle is unusable

# Live SUI/USD for DISPLAY math. The venue's own PriceConfig oracle is stale by
# ~15% sometimes; the DEX quote is the number traders actually see. Cached.
_suiusd_cache = {"v": 0.0, "ts": 0.0}


def sui_usd_live() -> float:
    """SUI/USD for display math: CoinGecko, Binance fallback; 5-min cached.
    Zero/None → caller falls back to the (stale) on-chain oracle."""
    import time as _t
    now = _t.time()
    if now - _suiusd_cache["ts"] < 300 and _suiusd_cache["v"]:
        return _suiusd_cache["v"]
    v = 0.0
    try:
        import requests
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "sui", "vs_currencies": "usd"}, timeout=8)
        v = float((r.json().get("sui") or {}).get("usd") or 0)
    except Exception:
        v = 0.0
    if not v:
        try:
            import requests
            r = requests.get("https://api.binance.com/api/v3/ticker/price",
                             params={"symbol": "SUIUSDT"}, timeout=8)
            v = float(r.json().get("price") or 0)
        except Exception:
            v = 0.0
    if v:
        _suiusd_cache.update(v=v, ts=now)
    return v or _suiusd_cache["v"]


_supply_cache: dict[str, int] = {}


# ---------------------------------------------------------------- metadata
@dataclass
class Metrics:
    price_sui: float = 0.0           # SUI per whole token
    price_mist_per_atom: int = 0     # micro-mist scale kept float-safe
    decimals: int = 6
    total_supply_atoms: int = 0
    circulating_atoms: int = 0
    mcap_sui: float = 0.0
    fdv_usd: float = 0.0             # price x TOTAL supply (curve-held included)
    virtual: tuple = (0.0, 0.0)      # fitted (vX_mist, vY_atoms); (0,0)=unknown
    sui_usd: float = 0.0
    mcap_usd: float = 0.0
    sui_reserve_mist: int = 0
    token_reserve_atoms: int = 0
    grad_threshold_mist: int = 0
    progress_bps: int = 0
    grad_left_sui: float = 0.0
    eta_hours: tuple | None = None   # (low, high) or None = insufficient data
    grad_mcap_usd: float = 0.0       # projected mcap at graduation (fit-based)
    fit_err: float | None = None     # None = not enough observations
    liq_sui: float = 0.0             # curve reserve or pool SUI side


_dec_cache: dict[str, int] = {}


def token_decimals(ch: Chain, coin_type: str) -> int:
    if coin_type in _dec_cache:
        return _dec_cache[coin_type]
    try:
        r = ch.query('{ coinMetadata(coinType: "' + coin_type + '") { decimals } }')
        d = int(((r or {}).get("coinMetadata") or {}).get("decimals", 6))
    except (GqlError, ValueError, TypeError):
        d = 6
    _dec_cache[coin_type] = d
    return d


def sui_usd_price(ch: Chain) -> float:
    """From the on-chain PriceConfig oracle (sui_price_scaled) — venue-stale but
    keyless. Display math prefers sui_usd_live(); this is the fallback."""
    try:
        o = ch.object("0xaebff66fd224e4fefae2426b5cdb30c6e5c488dce5ec34a730246e8b91d44ff9")
        v = int(((o or {}).get("json") or {}).get("sui_price_scaled") or 0)
        if v <= 0:
            return 0.0
        p = v / 1000.0                     # verified: 3-decimal USD fixed point
        return p if 0.01 <= p <= 10_000 else 0.0
    except Exception:
        return 0.0


def token_total_supply(ch: Chain, coin_type: str) -> int:
    """Real total supply via coinMetadata(coinType:){ supply } — the canonical
    GraphQL field (verified live 2026-09-14; BigInt atoms). 0 = unknown."""
    if coin_type in _supply_cache:
        return _supply_cache[coin_type]
    total = 0
    try:
        r = ch.query('{ coinMetadata(coinType: "%s") { supply } }' % coin_type)
        v = ((r or {}).get("coinMetadata") or {}).get("supply")
        if v:
            total = int(v)
    except Exception:
        total = 0
    _supply_cache[coin_type] = total
    return total


# ------------------------------------------------- per-curve virtual reserves
# Verified on mainnet 2026-09-14: Suipump curves are NOT plain x*y=k. Each is
# launched with private VIRTUAL reserves (randomized per token, e.g. 4342 SUI /
# 266.7M tok and 4993 SUI / 412.1M tok on two live curves) and trades satisfy
# (x+vX)(y+vY)=k. Pricing off raw x/y UNDERQUOTES early markets 2-4x.
# We recover (vX, vY) per curve from three historical object versions and cache.
_vfit_cache: dict[str, tuple] = {}


def virtual_reserves(ch: Chain, curve_addr: str) -> tuple[float, float]:
    """(vX_mist, vY_atoms); (0,0) = unknown/not-enough-history (callers then
    fall back to raw x/y, still an honest floor, never a silent 2x error claim)."""
    if not curve_addr:
        return (0.0, 0.0)
    if curve_addr in _vfit_cache:
        return _vfit_cache[curve_addr]
    try:
        q = ('{ transactions(first: 30, filter: { affectedObject: "%s" }) { nodes'
             ' { effects { objectChanges { nodes { address outputState { version'
             ' } } } } } } }' % curve_addr)
        r = ch.query(q)
        vers = sorted({int(c["outputState"]["version"])
                       for n in ((r or {}).get("transactions") or {}).get("nodes") or []
                       for c in ((n.get("effects") or {}).get("objectChanges") or {}).get("nodes") or []
                       if c.get("address") == curve_addr
                       and (c.get("outputState") or {}).get("version")})
        if len(vers) < 3:
            _vfit_cache[curve_addr] = (0.0, 0.0)
            return (0.0, 0.0)
        samples = []
        for v in (vers[0], vers[len(vers) // 2], vers[-1]):
            rr = ch.query('{ object(address: "%s", version: %d) { asMoveObject'
                          ' { contents { json } } } }' % (curve_addr, v))
            j = (((rr or {}).get("object") or {}).get("asMoveObject") or {}).get("contents", {}).get("json") or {}
            if isinstance(j, str):
                import json as _j; j = _j.loads(j)
            x, y = int(j.get("sui_reserve") or 0), int(j.get("token_reserve") or 0)
            if x > 0 and y > 0:
                samples.append((x, y))
        fit = solve_curve_fit(samples) if len(samples) >= 3 else None
        out = (fit["vx_mist"], fit["vy_atoms"]) if fit and fit.get("fit_err", 1) < 0.01 \
            else (0.0, 0.0)
    except Exception:
        out = (0.0, 0.0)
    _vfit_cache[curve_addr] = out
    return out


def expected_tokens_out(x_mist: int, y_atoms: int, spend_mist: int,
                        vx: float = 0.0, vy: float = 0.0) -> int:
    """Exact bonding-curve output for a buy of spend_mist under
    (x+vX)(y+vY)=k. Use for min-out math and 'expect ≈' lines — marginal
    price x/y is only an instantaneous quote, never a fill estimate."""
    k = (x_mist + vx) * (y_atoms + vy)
    y_new = k / (x_mist + spend_mist + vx) - vy
    return max(0, int(y_atoms - y_new))


# ---------------------------------------------------------------- curve fit (§3.0)
def solve_curve_fit(samples: list[tuple[int, int]]) -> dict | None:
    """Fit constant-product-with-virtual-reserves (x+vx)(y+vy)=C from 3+
    reserve snapshots [(sui_mist, token_atoms), …].

    Three points close-solve (vx, vy); extra points validate the fit. Returns
    None when data is insufficient/degenerate — callers then show '~more data',
    NEVER a fake-precise number (docs §3.0)."""
    pts = [(Fraction(int(x)), Fraction(int(y))) for x, y in samples if x > 0 and y > 0]
    if len(pts) < 3:
        return None
    (x1, y1), (x2, y2), (x3, y3) = pts[0], pts[1], pts[-1]
    # eq(i,j): vy*(x_i - x_j) = (x_j*y_j - x_i*y_i) + vx*(y_j - y_i)
    # eliminate vy between pairs (1,2) and (2,3) → vx:
    A12 = x2 * y2 - x1 * y1
    A23 = x3 * y3 - x2 * y2
    B12 = y2 - y1
    B23 = y3 - y2
    D12 = x1 - x2
    D23 = x2 - x3
    den = B12 * D23 - B23 * D12
    if den == 0:
        return None
    vx = (A23 * D12 - A12 * D23) / den
    if vx <= 0 or D12 == 0:
        return None
    vy = (A12 + vx * B12) / D12
    if vy < 0:
        return None
    C = (x1 + vx) * (y1 + vy)
    fvx, fvy, fC = float(vx), float(vy), float(C)
    errs = [abs((fC / (x + fvx) - fvy) - y) / y for x, y in
            ((float(a), float(b)) for a, b in pts) if y > 0]
    fit_err = sum(errs) / len(errs) if errs else 1.0
    return {"vx_mist": fvx, "vy_atoms": fvy, "C": fC, "fit_err": fit_err,
            "observations": len(pts)}


def project_grad_metrics(fit: dict, x_total_mist: int, decimals: int,
                         sui_price_usd: float, total_supply_atoms: int) -> tuple:
    """Price (SUI/atom) and mcap-USD at graduation point x = threshold."""
    x = float(x_total_mist)
    y = max(0.0, fit["C"] / (x + fit["vx_mist"]) - fit["vy_atoms"])
    price_atom_mist = (x + fit["vx_mist"]) / (y + fit["vy_atoms"])  # mist per atom
    price_whole = price_atom_mist * 1e-9 * (10 ** decimals)         # SUI per whole token
    circ = max(0, total_supply_atoms - int(y))
    mcap_sui = price_whole * circ / (10 ** decimals)
    mcap_usd = mcap_sui * sui_price_usd
    return price_whole, mcap_usd


# ---------------------------------------------------------------- main
def compute(ch: Chain, st: AssetState, reserves_samples: list | None = None,
            total_supply_atoms: int | None = None) -> Metrics:
    m = Metrics()
    dec = token_decimals(ch, st.token_type) if st.token_type else 6
    m.decimals = dec
    m.sui_usd = sui_usd_live() or sui_usd_price(ch) or DEFAULT_SUI_USD_FALLBACK
    x = st.sui_reserve_mist
    y = st.token_reserve
    m.sui_reserve_mist, m.token_reserve_atoms = x, y
    m.grad_threshold_mist = st.grad_threshold_mist
    if st.grad_threshold_mist > 0:
        m.progress_bps = min(10000, int(x * 10000 / st.grad_threshold_mist))
        m.grad_left_sui = max(0.0, (st.grad_threshold_mist - x) / 1e9)
    if st.kind in ("pool", "graduating"):
        # curve is dead/drained — threshold math on stale reserves is meaningless
        m.progress_bps = 10000 if st.kind == "pool" else m.progress_bps
        m.grad_left_sui = 0.0
    if st.kind == "curve" and x > 0 and y > 0:
        vx, vy = virtual_reserves(ch, st.curve_id)
        m.virtual = (vx, vy)
        price_mist_per_atom = (x + vx) / (y + vy)
        m.price_mist_per_atom = int(price_mist_per_atom)
        # SUI per whole token = (mist/atom) × 1e-9 × atoms/whole(10^dec)
        m.price_sui = price_mist_per_atom * 1e-9 * (10 ** dec)
        ts = (total_supply_atoms or token_total_supply(ch, st.token_type)
              or (y + _circ_guess(y)))
        m.total_supply_atoms = ts
        m.circulating_atoms = max(0, ts - y)
        m.mcap_sui = m.price_sui * m.circulating_atoms / (10 ** dec)
        m.mcap_usd = m.mcap_sui * m.sui_usd
        # curve tokens price nearly everything INSIDE the curve; FDV is the
        # number traders actually quote pre-graduation — surface both, and the
        # UI shows MCap (never a $0 when FDV > 0).
        m.fdv_usd = m.price_sui * ts / (10 ** dec) * m.sui_usd
        m.liq_sui = x / 1e9
        fit = solve_curve_fit(reserves_samples or [])
        if fit and fit["fit_err"] < 0.05:
            m.fit_err = round(fit["fit_err"], 4)
            _, gmc = project_grad_metrics(fit, st.grad_threshold_mist, dec, m.sui_usd, ts)
            m.grad_mcap_usd = gmc
    elif st.kind == "pool" and st.pool_id:
        try:
            p = ch.object(st.pool_id)
            pj = (p or {}).get("json") or {}
            a = int(pj.get("coin_a") or 0)
            b = int(pj.get("coin_b") or 0)
            # direction: prefer the type string (Pool<T, SUI> ⇒ A=token); size
            # heuristic only as fallback. sqrt-price squares B/A in RAW units.
            ptype = str((p or {}).get("type") or "")
            tok_is_a = True
            if "Pool<" in ptype:
                try:
                    inner = ptype[ptype.index("Pool<") + 5: ptype.rindex(">")]
                    first = inner.split(",")[0].strip().lower()
                    tok_is_a = not first.endswith("::sui")
                except Exception:
                    pass
            # A is the non-SUI side per the type string; coin_a/coin_b are its
            # raw amounts (SUI in mist, token in atoms).
            sui_mist = int((b if tok_is_a else a) or 0)
            tok_atoms = int((a if tok_is_a else b) or 0)
            m.sui_usd = m.sui_usd
            m.liq_sui = sui_mist / 1e9
            ts = (total_supply_atoms or token_total_supply(ch, st.token_type) or 0)
            # CLMM spot price = sqrt_price(Q64.64)^2 (mist-of-B per atom-of-A when
            # A=token). Reserve RATIO is only a fallback — under concentrated
            # liquidity the two diverge whenever price sits off a range edge.
            spot = 0.0
            try:
                sq = int(pj.get("current_sqrt_price") or 0)
                if sq > 0 and tok_is_a:
                    spot = (sq / 2 ** 64) ** 2            # SUI-mist per token atom
            except Exception:
                spot = 0.0
            if tok_atoms > 0:
                mist_per_atom = spot or (sui_mist / tok_atoms)   # float! tiny values
                m.price_mist_per_atom = int(mist_per_atom * 10 ** 6)  # micro-mist
                m.price_sui = mist_per_atom * 1e-9 * (10 ** dec)
                m.total_supply_atoms = ts
                m.circulating_atoms = ts
                m.mcap_sui = m.price_sui * ts / (10 ** dec)
                m.mcap_usd = m.mcap_sui * m.sui_usd
                m.fdv_usd = m.mcap_usd
        except Exception:
            pass
    return m


def _circ_guess(y: int) -> int:
    # mainnet cohort: curve starts at 800M whole (1e15 atoms at 6 dec); tokens in
    # circulation ≈ total - curve-held; refine with observed per-curve totals later.
    return 200_000_000 * 10 ** 6


def eta_hours(m: Metrics, trade_rate_mist_per_min: float) -> tuple | None:
    """Left/threshold over a 30-min EMA of SUI inflow; None if no data."""
    if not m.grad_threshold_mist or m.grad_left_sui <= 0:
        return None
    if trade_rate_mist_per_min <= 0:
        return None
    hrs = (m.grad_left_sui * 1e9) / trade_rate_mist_per_min / 60.0
    return (round(hrs * 0.6, 1), round(hrs * 1.8, 1))
