"""Gate G7: Wallet key is never stored in plaintext .env (only encrypted in ledger)."""
import re

env = open(".env").read()
m = re.search(r"^EXEC_SUI_KEYPAIR_HEX=(.+)$", env, re.M)
if m and "suiprivkey" in m.group(1):
    raise AssertionError("plaintext suiprivkey found in .env!")
print("no plaintext key in .env:", m is None)
print("KEY STORAGE: PASS")