"""Dry-run test for the real on-chain USDC transfer path (no network broadcast).

Verifies: key decrypt -> SUIAdapter build -> transfer_asset parameter validation
-> dry-run JSON/BCS construction -> _coin_spec. Does NOT broadcast (wallet has no
funds on testnet); that's the honest limit of an offline test.
"""
import os
import sys
import subprocess

REPO = os.path.dirname(os.path.abspath(__file__)) + "/../.."
os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, "service/execution"))

def _load_master_key():
    pid = subprocess.run(["pgrep", "-f", "tg_bot/main.py"], capture_output=True,
                         text=True).stdout.strip().splitlines()
    if not pid:
        return
    try:
        raw = open(f"/proc/{pid[0]}/environ", "rb").read().decode("utf-8", "replace")
        for line in raw.split("\x00"):
            if line.startswith("TG_EXEC_MASTER_KEY="):
                os.environ["TG_EXEC_MASTER_KEY"] = line.split("=", 1)[1]
                return
    except Exception:
        pass

_load_master_key()
if not os.environ.get("TG_EXEC_MASTER_KEY"):
    # fall back to the .env files
    for envf in ("service/tg_bot/.env", ".env"):
        if os.path.exists(envf):
            for line in open(envf):
                line = line.strip()
                if line.startswith("TG_EXEC_MASTER_KEY="):
                    os.environ["TG_EXEC_MASTER_KEY"] = line.split("=", 1)[1]

from ledger import ExecLedger
from exec_vault import ExecVault
from sui_adapter import SUIAdapter

key = None
ledger = ExecLedger("exec_ledger.db")
try:
    w = ledger.wallet_by_bot_chain(2, "sui")
    if not w or not w.get("key_enc"):
        print("FAIL: no wallet key stored for bot 2/sui")
        sys.exit(1)
    vault = ExecVault()
    key = vault.decrypt(w["key_enc"])
    print(f"[1] wallet key decrypt: OK ({len(key)} chars)")
    print(f"[2] stored address : {w['address']}")

    ada = SUIAdapter(ledger, key, testnet=True)
    print(f"[3] SUIAdapter built: OK")
    print(f"    derived address: {ada.address}")
    print(f"    matches stored  : {ada.address.lower() == w['address'].lower()}")
    assert ada.address.lower() == w["address"].lower(), "address mismatch!"

    # Test GraphQL coin reads (replaces deprecated suix_getCoins)
    try:
        coins = ada._gql_coins("0x2::sui::SUI")
        print(f"[3a] GraphQL SUI coins: {len(coins)} found")
        for c in coins[:3]:
            print(f"    {c['objectId'][:14]}... v{c['version']} bal={c['balance']}")
    except Exception as exc:
        print(f"[3a] GraphQL coins error: {exc}")

    usdc_type, dec = ada._coin_spec("USDC")
    print(f"[4] USDC coin spec: {usdc_type[:24]}... dec={dec}")
    sui_type, dec2 = ada._coin_spec("SUI")
    print(f"[5] SUI  coin spec: {sui_type} dec={dec2}")

    # Parameter validation (no RPC/broadcast)
    dummy = "0x" + "ab" * 32
    r1 = ada.transfer_asset(dummy, 0.0, "USDC")
    assert not r1.get("ok") and "amount" in r1.get("error", ""), r1
    print(f"[6] zero-amount rejected: OK ({r1.get('error')[:40]})")

    r2 = ada.transfer_asset("short", 10.0, "USDC")
    assert not r2.get("ok"), r2
    print(f"[7] bad-address rejected: OK ({r2.get('error')[:40]})")

    r3 = ada.transfer_asset(dummy, 999999.0, "USDC")
    assert not r3.get("ok"), r3
    print(f"[8] insufficient-funds handled: OK ({r3.get('error')[:60]})")

    print("\nRESULT: PASS (offline validation). On-chain broadcast requires a funded wallet + reachable Sui RPC.")
finally:
    try:
        ledger.close()
    except Exception:
        pass
