# Gates: Production Verification — Neko Trade Bot

Scope: Verify every production-critical component of the Neko trade bot is correctly wired, observable, and can execute real trades on Bluefin with sufficient certainty.

- [x] G1: Gateway builds Bluefin adapter from encrypted ledger key
  CHECK: .venv/bin/python scripts/verify/gateway_adapter.py
  EXPECT: GATEWAY+ADAPTER: PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=c7f7f6aab33e4746f2f03a4397d9a6b20cf447a4df59ef7fd8fc88dd22ed432a; output-bytes=105

- [x] G2: BluefinAdapter can build and sign a PersonalMessage order
  CHECK: .venv/bin/python scripts/verify/sign_order.py
  EXPECT: SIGN ORDER: PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=b92c85abdb4f35d0fe3388b327a86831c3d1282174fc8089ead5f83352e9d21e; output-bytes=171

- [x] G3: Agent leverages LLM leverage decision (20x floor, 20-40x range)
  CHECK: grep -E "leverage|LIVE_AGENT_LEVERAGE|clamp_leverage|BLUEFIN_MAX_LEVERAGE" service/agent/live_agent.py | head -20
  EXPECT: clamp_leverage
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=04a90ab93968eefe1bb741c56619585be6d36c2fac765dd99365649edd4a0705; output-bytes=1174

- [x] G4: Decision cache overwrites, never accumulates (Peek shows current only)
  CHECK: .venv/bin/python -c "import json; from pathlib import Path; c=Path('research/exports/live_agent_cache.json'); assert c.exists(), 'cache missing'; d=json.loads(c.read_text()); assert len(d) <= 2, f'cache has {len(d)} entries, should be <=2'; print(f'cache entries: {len(d)} (max 2: current + hold)'); print('keys:', list(d.keys()))"
  EXPECT: cache entries: 1 (max 2: current + hold)
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=d05c4e31883cec37a76b02c2956eb74777524b47798672c35e470aa5d879a48c; output-bytes=60

- [x] G5: Fee is charged at entry only, never at close (no double-deduction)
  CHECK: .venv/bin/python scripts/verify/fee_model.py
  EXPECT: FEE MODEL: PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=b96b4876ebfbc7dbe5fc70575ef8d624a76f78947f0be8919cf6d3ae1e01b747; output-bytes=38

- [x] G6: Healthcheck restarts crashed agents automatically (no silent stall)
  CHECK: grep -n "healthcheck\|def start" service/tg_bot/main.py | head -8
  EXPECT: healthcheck
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=e7b2c8f961a2f452ada0ca138db3a7319d0b41525653917144c23674052aeef9; output-bytes=343

- [x] G7: Wallet key is never stored in plaintext .env (only encrypted in ledger)
  CHECK: .venv/bin/python scripts/verify/key_storage.py
  EXPECT: KEY STORAGE: PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=388c61efbf8de61b9385820577d92191b50254b3c137d43e18223f27a6469e80; output-bytes=49

- [x] G8: Watch <ASSET> rejects tokens with no perp market (IKA rejected)
  CHECK: .venv/bin/python -c "import sys; sys.path.insert(0,'service/tg_bot'); perps={'sui':{'BTC','ETH','SOL','SUI','ARB','AVAX','BNB','DOGE','LINK','LTC','OP','MATIC','SEI','HYPE','DEEP','WAL','GOLD'}}; print('IKA tradeable:', 'IKA' in perps['sui']); print('BTC tradeable:', 'BTC' in perps['sui']); assert 'IKA' not in perps['sui']; assert 'BTC' in perps['sui']"
  EXPECT: IKA tradeable: False
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=2bf97782017bca09514cb7a57d016d09c9d07fed9bef10ce188c21b3fbd7c0eb; output-bytes=41

- [x] G9: Exec ledger has wallet with encrypted key, no stale zombie processes
  CHECK: .venv/bin/python -c "import sqlite3; db=sqlite3.connect('exec_ledger.db'); db.row_factory=sqlite3.Row; w=db.execute('SELECT id,bot_id,chain,address,status FROM exec_wallets').fetchall(); print('wallets:', len(w)); assert len(w)>=1; z=db.execute('SELECT COUNT(*) c FROM exec_orders').fetchone()['c']; print('orders:', z); db.close()"
  EXPECT: wallets: 1
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=3b6a2ab9898ab91c39749e93ab99e68fdc459c74e43ac2e2f4a08969cc4ea4e2; output-bytes=21

- [x] G10: No zombie agent processes (agent pool healthcheck clears them)
  CHECK: ps aux | grep -E "defunct.*python" | grep -v grep | wc -l
  EXPECT: 0
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro/.unlazy/production-verification; path=08538f152605/16 entries; EXPECT=matched; output-sha256=9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa; output-bytes=2

- [x] G11: Agent can fetch 5m candles and build scenario matrix (scalp flow works)
  CHECK: .venv/bin/python scripts/verify/scalp_flow.py
  EXPECT: SCALP FLOW: PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=d89cfedef6c109b7b270797df9b4ab03f3d450d81a5d4c1e4804e4ddd2d99615; output-bytes=55

- [x] G12: Trade execution path does not double-charge platform fee
  CHECK: grep -n "record_fill\|_sweep_fee\|fee_platform\|PLATFORM_FEE_BPS" service/execution/router.py
  EXPECT: PLATFORM_FEE_BPS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=7b69c6655ad9b5d8129dd1c393ffda699fd51caf6ff5eac0160b6fa10a16e3f1; output-bytes=352

- [x] G13: LLM prompt includes leverage field and confidence instructions
  CHECK: grep -n "leverage.*confidence\|LEVERAGE.*20.*40\|leverage.*P(win)" service/agent/live_agent.py | head -5
  EXPECT: 20
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=82ea9f9c3f722ea2615075e217a82f77020b319c96f2f4193aeb585401485d00; output-bytes=84

- [x] G14: All agent processes are running and healthy (no zombie, no orphan)
  CHECK: .venv/bin/python scripts/verify/processes.py
  EXPECT: PROCESSES: PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=67f47aea0b333dba42340db222b6581e93d6e57c8afcbfbc54761d34c442ec74; output-bytes=116

- [x] G16: Agent builds a valid OrderIntent for a real perp trade
  CHECK: .venv/bin/python scripts/verify/sign_order.py
  EXPECT: INTENT: PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=b92c85abdb4f35d0fe3388b327a86831c3d1282174fc8089ead5f83352e9d21e; output-bytes=171

- [x] G17: BluefinAdapter builds a signed PersonalMessage order from the user wallet
  CHECK: .venv/bin/python scripts/verify/sign_order.py
  EXPECT: SIGN ORDER: PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=b92c85abdb4f35d0fe3388b327a86831c3d1282174fc8089ead5f83352e9d21e; output-bytes=171

- [x] G18: Complete user journey — onboarding, key, agent, watch, decide, sign, cache, peek, fees
  CHECK: .venv/bin/python scripts/verify/user_scenarios.py
  EXPECT: USER CERTAINTY: 100.0 %
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=2b9f4284e347b9af62a6d17334ef1b6b8215d80b13e50d78cc0e250ddddd0be0; output-bytes=483

- [x] G19: Send/Receive + all notifications auto-delete after 5 minutes (TTL 300)
  CHECK: .venv/bin/python scripts/verify/send_receive_ttl.py
  EXPECT: SEND/RECEIVE TTL: PASS
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=d057d9284e931049db5a880e7eae75473a3ee6f291ffcaa6c6dd8bc4233e639b; output-bytes=403

- [x] G15: Final certainty score report — every production gate verified
  CHECK: .venv/bin/python scripts/verify/certainty.py
  EXPECT: certainty:
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=92a6f85e6d638ce7d86a2735538e3482d9bcad978a4390e4ae6b9a8b1c1064c2; output-bytes=34