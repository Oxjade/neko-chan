#!/usr/bin/env python3
"""Hyperliquid testnet end-to-end check for the real-trading adapter.

Requires env vars:
  HL_TESTNET_MASTER_KEY - hex private key (0x-prefixed or bare) of the testnet master wallet
  HL_TESTNET_AGENT_KEY  - hex private key of the agent (API) wallet to approve

Optional:
  HL_TESTNET_PLACE_ORDER=1 - place a tiny market buy (BTC 0.001) and then clean up

Only testnet URLs are used; there is no mainnet code path in this script.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "service", "execution"))

from eth_account import Account

from hl_adapter import (
    HLApiWallet,
    HLAdapter,
    HL_TESTNET_INFO,
    HL_TESTNET_EXCHANGE,
)
from ledger import ExecLedger
from order_model import OrderIntent


def _die(message: str) -> None:
    print(f"FATAL: {message}", file=sys.stderr)
    sys.exit(2)


def _env_hex_key(name: str) -> str:
    raw = os.environ.get(name, "").strip()
    if not raw:
        _die(
            f"{name} is not set. Provide the hex private key of the wallet to use.\n"
            f"  export {name}=0x<64-hex-characters>"
        )
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 64:
        _die(f"{name} must be a 64-character hex private key (with or without 0x prefix); got {len(raw)} chars")
    try:
        bytes.fromhex(raw)
    except ValueError:
        _die(f"{name} is not valid hex: {raw[:8]}...")
    return "0x" + raw


def main() -> int:
    if "testnet" not in HL_TESTNET_INFO or "testnet" not in HL_TESTNET_EXCHANGE:
        _die("adapter is not pointing at testnet URLs - refusing to run")

    master_key = _env_hex_key("HL_TESTNET_MASTER_KEY")
    agent_key = _env_hex_key("HL_TESTNET_AGENT_KEY")
    master_address = Account.from_key(master_key).address

    wallet = HLApiWallet(agent_key, testnet=True)
    print(f"[1/5] testnet endpoints: {HL_TESTNET_INFO}")
    print(f"[1/5] master wallet:     {master_address}")
    print(f"[1/5] agent wallet:      {wallet.address}")

    print("[2/5] approving agent on testnet...")
    tx = wallet.approve_agent_tx(master_key, name="neko-testnet-check")
    sig = tx["signature"]
    print(f"[2/5] approval payload: type={tx['action']['type']} chain={tx['action']['hyperliquidChain']} "
          f"nonce={tx['nonce']}")
    print(f"[2/5] approval signature: v={sig['v']} r={sig['r']} s={sig['s']}")
    try:
        resp = wallet.submit_agent_approval(master_key, name="neko-testnet-check")
        print(f"[2/5] approval response: {resp}")
    except Exception as exc:
        print(f"[2/5] approval submit failed: {exc}", file=sys.stderr)
    try:
        approved = wallet.is_agent_approved(master_address)
        print(f"[2/5] agent approved: {approved}")
    except Exception as exc:
        approved = False
        print(f"[2/5] approval check failed: {exc}", file=sys.stderr)

    ledger = ExecLedger("/tmp/hl_testnet_check_ledger.db")
    adapter = HLAdapter(ledger, agent_key, master_address, testnet=True)

    print("[3/5] fetching account state...")
    state = adapter.get_account_state()
    if not state.get("ok"):
        print(f"FATAL: account state fetch failed: {state.get('error')}", file=sys.stderr)
        return 1
    print(f"[3/5] balances:  {state['balances']}")
    print(f"[3/5] positions: {state['positions'] or 'none'}")

    order_result = {"ok": False, "error": "HL_TESTNET_PLACE_ORDER != 1; order placement skipped"}
    if os.environ.get("HL_TESTNET_PLACE_ORDER", "0").strip() == "1":
        print("[4/5] placing tiny market buy: BTC 0.001 ...")
        intent = OrderIntent(
            chain="hyperliquid", venue="hl-perp", symbol="BTC", side="buy",
            qty=0.001, order_type="market", leverage=1,
            idempotency_key="hl-testnet-check-1",
        )
        order_result = adapter.place_order(intent, bot_id=1)
        print(f"[4/5] order result: {order_result}")
        if not order_result.get("ok"):
            print(f"WARNING: order placement failed: {order_result.get('error')}", file=sys.stderr)
    else:
        print("[4/5] HL_TESTNET_PLACE_ORDER=1 not set; skipping order placement")

    print("[5/5] cleanup: cancel all open orders + flat positions...")
    cancel = adapter.cancel_all(bot_id=1)
    flat = adapter.flat_and_cancel(bot_id=1)
    print(f"[5/5] cancel_all:      {cancel}")
    print(f"[5/5] flat_and_cancel: {flat}")
    fills = adapter.get_fills()
    print(f"[5/5] recent fills:    {fills[:3] or 'none'}")

    failures = []
    if not approved:
        failures.append("agent approval not confirmed via extraAgents")
    if not state.get("ok"):
        failures.append("account state")
    if order_result.get("error") and "skipped" not in order_result["error"]:
        failures.append(f"order placement: {order_result['error']}")
    if failures:
        print("RESULT: FAIL - " + "; ".join(failures))
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())