# GATES: Solana/chain=sol removal — only Sui remains

OWNS: frontend/static/config/lpp*.json, api/src/server.ts, api/src/sui.ts

Scope: Remove every Solana atom from the served tradebotpro product. Networks
in both lpp.json and lpp-test.json become Sui-only. Server drops the
token_security_sol route and tokenSecuritySol handlerholistic; OTHER_CHAINS no
longer blocks/mentions sol; served pages show zero chain=sol/hrefs/sol text.

- [ ] G1: lpp.json networks = Sui only, valid JSON, 69 rows, Sui[0]=suipump
  CHECK: node /home/carnage/tradebotpro/api/tools/check-config.mjs lpp.json
  EXPECT: lpp.json OK: only-Sui
  EVIDENCE: pending

- [ ] G2: lpp-test.json networks = Sui only, valid JSON, 69 rows, Sui[0]=suipump
  CHECK: node /home/carnage/tradebotpro/api/tools/check-config.mjs lpp-test.json
  EXPECT: lpp-test.json OK: only-Sui
  EVIDENCE: pending

- [ ] G3: no sol literal in server.ts OTHER_CHAINS regex; no token_security_sol route
  CHECK: node /home/carnage/tradebotpro/api/tools/check-server.mjs
  EXPECT: server.ts clean: no sol chain literal, no token_security_sol route
  EVIDENCE: pending

- [ ] G4: no tokenSecuritySol handler in api/src/sui.ts
  CHECK: node /home/carnage/tradebotpro/api/tools/check-sui.mjs
  EXPECT: sui.ts clean: no tokenSecuritySol handler
  EVIDENCE: pending

- [ ] G5: REST verify /sol/token/... 404, /sui/token/... 200, served lpp.json Sui-only
  CHECK: node /home/carnage/tradebotpro/api/tools/check-http.mjs
  EXPECT: HTTP OK: sol 404, sui 200, only-sui config served
  EVIDENCE: pending

- [ ] G6: browser DOM audit — no sol atom, no chain=sol href, no Solana text
  EVIDENCE: pending
