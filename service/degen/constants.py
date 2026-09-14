"""Degen mode: on-chain constants verified against Sui MAINNET (2026-09-13/14).

Every address/selector here was confirmed via graphql.mainnet.sui.io object
reads, package/module introspection and bytecode disassembly. Do NOT copy testnet
open-source assumptions (module names, graduation guards, reserve constants) into
this file — mainnet diverged (docs/degen-mode.md §0).
"""

# ---------------------------------------------------------------- launchpads
# Suipump bonding-curve package (current live version). Module is
# `bonding_curve` (NOT `trading` as in the old testnet repo).
SUIPUMP_PKG = "0xb205fea41ccedac051bc66498e6ca68cb802c4a6ea06da12e524bed09c80d9b0"
# Suipump legacy package — some live curves were created here; both must be
# subscribed / probed (docs §0.1).
SUIPUMP_PKG_LEGACY = "0x7b4163d17ce18b386ee50929ba48fa0a2ecb60304df4b07e26835aa18617cda2"
SUIPUMP_PACKAGES = (SUIPUMP_PKG, SUIPUMP_PKG_LEGACY)

SUIPUMP_MODULE = "bonding_curve"
# Curve object type prefix per package: "<pkg>::bonding_curve::Curve<token_type>"
SUIPUMP_TOKEN_TYPE_SUFFIX = "::suipump::SUIPUMP"

# Shared singletons (ownerKind: SHARED, verified).
SUIPUMP_PRICE_CONFIG = "0xaebff66fd224e4fefae2426b5cdb30c6e5c488dce5ec34a730246e8b91d44ff9"
PRICE_CONFIG_ISV = 974716582  # initialSharedVersion (live); janitor re-reads if it drifts
SUIPUMP_GRADUATION_REGISTRY = "0x36a82ab92b7b2b45a137674a5fc1fff89bfd9895653fa32ed7250d30379360a0"
SUIPUMP_LAUNCH_ISSUER_REGISTRY = "0xb622741bfcfd6ef13b40c2d5c2adc8d796f68b3b1254fabaa571bffa3a91e875"

# Cetus CLMM: graduated liquidity lands in pool objects of this package.
CETUS_PKG = "0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb"
CETUS_POOL_MODULE = "pool"

# Standard framework objects.
SUI_CLOCK = "0x6"
SUI_COIN_TYPE = "0x0000000000000000000000000000000000000000000000000000000000000002::sui::SUI"

MIST = 1_000_000_000  # 1 SUI

# ---------------------------------------------------------------- events
# Verified public event structs (module `bonding_curve`):
EV_CURVE_CREATED = f"{SUIPUMP_PKG}::{SUIPUMP_MODULE}::CurveCreated"
EV_GRADUATED = f"{SUIPUMP_PKG}::{SUIPUMP_MODULE}::Graduated"
EV_POOL_RECORDED = f"{SUIPUMP_PKG}::{SUIPUMP_MODULE}::PoolRecorded"  # relayer records pool
# Graduated event fields (from Graduated event json): curve_id, final_sui_reserve,
# creator_bonus, protocol_bonus, graduation_target.

# ---------------------------------------------------------------- buy/sell ABI
# bonding_curve::buy<T>(curve &mut, coin Coin<SUI>, min_tokens_out u64,
#                       referrer Option<address>, price_config &PriceConfig,
#                       clock &Clock, ctx) -> (Coin<T>, Coin<SUI>)
# bonding_curve::sell<T>(curve &mut, coin Coin<T>, min_sui_out u64,
#                       arg3 Option<address>, ctx) -> Coin<SUI>
# Both PUBLIC non-entry (MoveCall-able, verified). Option<address> pure BCS:
# b"" empty = None; b"\x01"+addr32 = Some(addr).
OPTION_NONE_BCS = b""
OPTION_ADDR_PREFIX = b"\x01"

# buy error observed live: abort code 4 when curve.graduated == true
ERR_GRADUATED_ABORT_CODE = "4"

# ---------------------------------------------------------------- fees (owner spec, docs §3.2a)
PLATFORM_FEE_BPS = 50          # 0.5% per fill (in-PTB split pre-grad / integratorFee post)
BUNDLE_FEE_MIST = 5 * MIST     # flat 5 SUI per spread-burst -> fee wallet (owner decision)
SNIPE_GAS_BUDGET_MIST = 1 * MIST   # per-tx gas CAP (not a fee; failed txs ≈ 0.002 SUI)

# ---------------------------------------------------------------- endpoints
# JSON-RPC is DEPRECATED on public fullnodes (error -32601) — GraphQL only.
SUI_GQL_MAINNET = "https://graphql.mainnet.sui.io/graphql"
SUI_GQL_TESTNET = "https://graphql.testnet.sui.io/graphql"
# Suipump indexer metadata (names/icons) — cross-check/UI only, never ground truth.
SUIPUMP_INDEXER = "https://suipump-main-web.onrender.com"
# Aftermath spot router (reused via service/spot adapter).
AFTERMATH_API = "https://aftermath.finance/api"

# ---------------------------------------------------------------- blast (locked)
BLAST_PKG = ""  # not verified — §7.1 §0-probe obligation before any wiring
BLAST_LIVE = False
