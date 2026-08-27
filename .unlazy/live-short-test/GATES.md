# Gates: Live short-leg test on current market

OWNS: .unlazy/live-short-test/**

Scope: Place and measure a live paper short position on SOL in the current live
market over a finite window, and report realized P&L (after fees) against an
honest pass/fail criterion. This tests whether the short leg is profitable in
the CURRENT regime, not a backtest.

- [x] G1: a live short position is actually open in the paper ledger at test start
  CHECK: .venv/bin/python -c "import sqlite3; c=sqlite3.connect('service/server/data/clawtrader.db'); rows=[r for r in c.execute('SELECT symbol,side,quantity,entry_price FROM positions WHERE agent_id=8 AND quantity<0')]; assert len(rows)>=1, 'no open short'; print('open short present:', rows)"
  CWD: /home/carnage/tradebotpro
  EXPECT: open short present
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=87590bb9485b0663c7e8895f1cd6f1f6ca9147b0d0a958aaa991cd3077de806e; output-bytes=53

- [x] G2: the test's starting mark and entry are recorded so P&L is measured consistently
  CHECK: .venv/bin/python -c "import sqlite3,json; c=sqlite3.connect('service/server/data/clawtrader.db'); c.row_factory=sqlite3.Row; rows=[dict(r) for r in c.execute('SELECT symbol,entry_price,quantity FROM positions WHERE agent_id=8 AND quantity<0')]; assert rows, 'no short'; open('/tmp/opencode/short_test_start.json','w').write(json.dumps(rows)); print('start snapshot written:', rows)"
  CWD: /home/carnage/tradebotpro
  EXPECT: start snapshot written
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=4787d42587de42649ec0a845d5c9e9a7912f5df9cc6fa09041500a4373287ec9; output-bytes=85

- [ ] G3: short realized P&L (per open short) is positive after the test window, i.e. the short leg made money in the current market
  CHECK: .venv/bin/python -c "import sqlite3,urllib.request,json; c=sqlite3.connect('service/server/data/clawtrader.db'); c.row_factory=sqlite3.Row; shorts=[dict(r) for r in c.execute('SELECT symbol,entry_price,quantity FROM positions WHERE agent_id=8 AND quantity<0')]; req=urllib.request.Request('https://api.hyperliquid.xyz/info', data=json.dumps({'type':'allMids'}).encode(), headers={'Content-Type':'application/json'}); mids=json.load(urllib.request.urlopen(req,timeout=10)); [print(s['symbol']+' short @'+str(s['entry_price'])+' mark '+str(mids.get(s['symbol']))+' PnL '+str(round((s['entry_price']-float(mids.get(s['symbol'],s['entry_price'])))*abs(s['quantity']),2))) or __import__('sys').exit(1) if not (s['entry_price']-float(mids.get(s['symbol'],s['entry_price'])))*abs(s['quantity'])>0 else None for s in shorts]; print('ALL SHORTS PROFITABLE')"
  CWD: /home/carnage/tradebotpro
  EXPECT: ALL SHORTS PROFITABLE
ABANDON: G3 HONEST FAIL - live short leg is NOT profitable in the current regime. Measured 2026-08-27 ~20:35-20:40 UTC: SOL short entry 107.49, closed 108.85, realized PnL -$0.15 (-1.27% on notional). Matches full-universe analysis: shorts only profit in confirmed bears; current market is up-drift so the short is underwater. This is the experimentally-confirmed answer to "is the short profitable now": NO. Short-sized and bear-gated logic is correct; no live short should be sized as if profitable in this regime. Closing the short also exposed and fixed the services.py cover bug (side-blind snapshot returned the long row, so any cover of a same-symbol short failed).
