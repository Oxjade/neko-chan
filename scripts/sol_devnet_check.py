#!/usr/bin/env python3
"""Devnet-only SOL adapter smoke check.

Reads SOL_DEVNET_KEYPAIR_HEX (solders-format secret hex, e.g. from
ExecVault.generate_key_material('solana')[1]) and prints SOL/USDC balance plus
open Jupiter perp positions on Solana devnet. If SOL_DEVNET_PLACE_ORDER=1 it
additionally quotes a tiny SOL->USDC swap, builds, signs and broadcasts it, and
prints the tx hash. Fails loudly if the env var is missing or a mainnet RPC
would be used. NO mainnet usage.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "service", "execution"))

from ledger import ExecLedger  # noqa: E402
from sol_adapter import (  # noqa: E402
    DEVNET_RPC,
    SOLAdapter,
    SOL_MINT,
    SWAP_QUOTE_URL,
    SWAP_SWAP_URL,
    USDC_MINT,
)


def main() -> int:
    keypair_hex = os.environ.get("SOL_DEVNET_KEYPAIR_HEX", "")
    if not keypair_hex:
        raise SystemExit(
            "SOL_DEVNET_KEYPAIR_HEX is required (solders-format secret hex). "
            "Generate with: ExecVault.generate_key_material('solana')[1]"
        )
    ledger = ExecLedger(":memory:")
    adapter = SOLAdapter(ledger, keypair_hex, testnet=True)
    if adapter.rpc_url != DEVNET_RPC:
        raise SystemExit(f"Refusing mainnet: rpc_url={adapter.rpc_url} != {DEVNET_RPC}")
    print(f"wallet {adapter.pubkey}")
    print(f"rpc    {adapter.rpc_url}")
    print(f"SOL  balance: {adapter.get_balance('SOL'):.9f}")
    print(f"USDC balance: {adapter.get_balance('USDC'):.6f}")
    positions = adapter.get_positions()
    print(f"perp positions: {len(positions)}")
    for pos in positions:
        print(f"  {pos['symbol']} {pos['side']} {pos['qty']} @ {pos['entry']} "
              f"(leverage {pos['leverage']})")
    if os.environ.get("SOL_DEVNET_PLACE_ORDER") != "1":
        print("SOL_DEVNET_PLACE_ORDER != 1; skipping swap order")
        return 0
    amount = 100_000  # 0.0001 SOL - tiny test amount
    quote = adapter._request("GET", SWAP_QUOTE_URL, params={
        "inputMint": SOL_MINT,
        "outputMint": USDC_MINT,
        "amount": amount,
        "slippageBps": 50,
    })
    print(f"quote: {amount} lamports SOL -> {quote.get('outAmount')} USDC")
    build = adapter._request("POST", SWAP_SWAP_URL, json={
        "quoteResponse": quote,
        "userPublicKey": adapter.pubkey,
        "wrapAndUnwrapSol": True,
    })
    tx = build.get("swapTransaction")
    if not tx:
        raise SystemExit(f"swap build returned no transaction: {build}")
    sig = adapter._sign_and_broadcast(tx)
    print(f"broadcast ok: tx {sig}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())