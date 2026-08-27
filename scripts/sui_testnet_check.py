"""Sui testnet read-only check + optional dry-run spot order.

Env:
  SUI_TESTNET_KEYPAIR_HEX           required - hex of the 32-byte ed25519 seed
  SUI_TESTNET_USDC_COIN_TYPE        optional - testnet USDC coin type override
  SUI_TESTNET_DEEPBOOK_PACKAGE      optional - DeepBookV3 package address
  SUI_TESTNET_POOL_ID               optional - DeepBook pool object id
  SUI_TESTNET_BALANCE_MANAGER       optional - balance manager object id
  SUI_TESTNET_PLACE_ORDER=1         optional - dry-run a tiny spot order
  SUI_TESTNET_BROADCAST=1           ONLY with this set does the order broadcast

Fails loudly (exit 1) when required env is missing. Never broadcasts unless
SUI_TESTNET_BROADCAST=1.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service", "execution"))

from ledger import ExecLedger  # noqa: E402
from order_model import OrderIntent  # noqa: E402
from sui_adapter import SUIAdapter  # noqa: E402

DEFAULT_TESTNET_USDC = (
    "0x5d4b302506645c37ff133b98c4b50a5ae14841659738d6d733d59d0d217a93bf::coin::COIN"
)


def main() -> int:
    keypair_hex = os.getenv("SUI_TESTNET_KEYPAIR_HEX", "").strip()
    if not keypair_hex:
        print("FATAL: SUI_TESTNET_KEYPAIR_HEX is not set "
              "(hex of the 32-byte ed25519 seed of the trading wallet)")
        return 1

    kwargs = {
        "usdc_coin_type": os.getenv("SUI_TESTNET_USDC_COIN_TYPE", "").strip()
        or DEFAULT_TESTNET_USDC,
    }
    for env_name, attr in (
        ("SUI_TESTNET_DEEPBOOK_PACKAGE", "deepbook_package"),
        ("SUI_TESTNET_POOL_ID", "pool_id"),
        ("SUI_TESTNET_BALANCE_MANAGER", "balance_manager"),
    ):
        value = os.getenv(env_name, "").strip()
        if value:
            kwargs[attr] = value

    adapter = SUIAdapter(ExecLedger(":memory:"), keypair_hex, testnet=True, **kwargs)
    print(f"address:  {adapter.address}")
    print(f"rpc:      {adapter.rpc_url}")
    print(f"SUI  balance:  {adapter.get_balance('SUI'):.6f}")
    print(f"USDC balance:  {adapter.get_balance('USDC'):.6f}")

    if os.getenv("SUI_TESTNET_PLACE_ORDER") != "1":
        print("Set SUI_TESTNET_PLACE_ORDER=1 to dry-run a tiny spot order.")
        return 0

    missing = [name for name, v in (
        ("SUI_TESTNET_DEEPBOOK_PACKAGE", adapter.deepbook_package),
        ("SUI_TESTNET_POOL_ID", adapter.pool_id),
        ("SUI_TESTNET_BALANCE_MANAGER", adapter.balance_manager),
    ) if not v]
    if missing:
        print(f"FATAL: SUI_TESTNET_PLACE_ORDER=1 but missing env: {', '.join(missing)}")
        return 1

    intent = OrderIntent(
        chain="sui", venue="deepbook-spot", symbol="SUI/USDC", side="buy",
        qty=0.001, order_type="market", leverage=1.0,
        idempotency_key=f"testnet-check-{int(time.time())}",
    )
    built = adapter.build_spot_order_tx(intent, ref_price=1.0)
    if not built.get("ok"):
        print(f"FATAL: order build failed: {built.get('error')}")
        return 1
    print("built spot order transaction JSON:")
    print(json.dumps(built["transaction_json"], indent=2))

    try:
        gas = adapter._dry_run(built["transaction_json"])
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: dryRunTransactionBlock failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"dryRunTransactionBlock OK: gas_price={gas['gas_price']} budget={gas['budget']}")

    if os.getenv("SUI_TESTNET_BROADCAST") == "1":
        result = adapter.place_order(intent, ref_price=1.0)
        print("broadcast result:")
        print(json.dumps(result, indent=2))
        if not result.get("ok"):
            return 1
    else:
        print("NOT broadcasting (set SUI_TESTNET_BROADCAST=1 to execute this order)")
    return 0


if __name__ == "__main__":
    sys.exit(main())