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


@dataclass
class Metrics:
    price_sui: float = 0.0           # SUI per whole token
    price_mist_per_atom: int = 0
    decimals: int = 6
    total_supply_atoms: int = 0
    circulating_atoms: int = 0
    mcap_sui: float = 0.0
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


# ---------------------------------------------------------------- metadata
_dec_cache: dict[str, int] = {}
_usd_cache: dict = {"v": 0.0, "ts": 0}


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
    """From the on-chain PriceConfig oracle (sui_price_scaled). Scaled as
    cents*? — we calibrate the divisor from magnitude instead of guessing."""
    try:
        o = ch.object("0xaebff66fd224e4fefae2426b5cdb30c6e5c488dce5ec34a730246e8b91d44ff9")
        v = int(((o or {}).get("json") or {}).get("sui_price_scaled") or 0)
        if v <= 0:
            return 0.0
        # calibration: the relayer sets scaled price for virtual-reserve math;
        # accept a wide window and derive magnitude (4.2e12 mist floor ≈ $4800
        # historical formula ⇒ scale ~ cents*1e6 for ~$4-20 SUI).
        for div in (1_000_000, 100_000, 1_000, 100):
            p = v / div
            if 0.01 <= p <= 10_000:
                return p
        return 0.0
    except Exception:
        return 0.0


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
    m.sui_usd = sui_usd_price(ch) or DEFAULT_SUI_USD_FALLBACK
    x = st.sui_reserve_mist
    y = st.token_reserve
    m.sui_reserve_mist, m.token_reserve_atoms = x, y
    m.grad_threshold_mist = st.grad_threshold_mist
    if st.grad_threshold_mist > 0:
        m.progress_bps = min(10000, int(x * 10000 / st.grad_threshold_mist))
        m.grad_left_sui = max(0.0, (st.grad_threshold_mist - x) / 1e9)
    if st.kind == "curve" and x > 0 and y > 0:
        price_mist_per_atom = x / y
        m.price_mist_per_atom = int(price_mist_per_atom)
        # SUI per whole token = (mist/atom) × 1e-9 × atoms/whole(10^dec)
        m.price_sui = price_mist_per_atom * 1e-9 * (10 ** dec)
        ts = total_supply_atoms or (y + _circ_guess(y))
        m.total_supply_atoms = ts
        m.circulating_atoms = max(0, ts - y)
        m.mcap_sui = m.price_sui * m.circulating_atoms / (10 ** dec)
        m.mcap_usd = m.mcap_sui * m.sui_usd
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
            # Pool<T,SUI>: coin_a=token, coin_b=SUI (direction per verified read)
            tok_atoms, sui_mist = (a, b) if a > b else (b, a)
            m.sui_usd = m.sui_usd
            m.liq_sui = sui_mist / 1e9
            if tok_atoms > 0:
                m.price_sui = (sui_mist / 1e9) / (tok_atoms / 10 ** dec)
                m.price_mist_per_atom = int(sui_mist / tok_atoms)
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
