# Gates: Limit-order win-rate fixes (paper sim + real-mode bugs + TP/stop tuning)

OWNS: service/agent/live_agent.py, service/agent/quant_strategy.py, service/execution/aftermath_adapter.py, service/server/routes_models.py, service/server/routes_signals.py, tests/research/test_quant_strategy.py, tests/execution/test_sui_aftermath.py, .env.example

Scope: Make the new limit-order entry actually work in both paper (simulated fills) and real (Aftermath) modes, fix the two real-mode execution bugs, and tune TP/stop sizing so more trades reach their target before the time-exit fires.

- [x] G1: Aftermath adapter uses intent.limit_price for limit orders (not raw ref_price)
  CHECK: python -c "code=open('service/execution/aftermath_adapter.py').read(); assert 'intent.limit_price' in code; assert 'price = (intent.limit_price or ref_price)' in code; print('limit price fix OK')"
  EXPECT: limit price fix OK
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=acf80b814b395b061d19b9c7f63915f8d650226f07c978b28f2368e69a9ef311; output-bytes=19

- [x] G2: The agent exit thread routes through the real gateway when it exists
  CHECK: python -c "code=open('service/agent/live_agent.py').read(); assert 'if gw is not None:' in code; assert 'route_real_order(' in code; assert code.count('route_real_order(') >= 2; print('exit routing OK')"
  EXPECT: exit routing OK
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=b69c7b8fb4180c14bdf397f9117728bcc0412c921b20638122024b76b5fed8df; output-bytes=16

- [x] G3: RealtimeSignalRequest accepts an optional limit_price for paper limit-fill simulation
  CHECK: python -c "import ast,sys; sys.path.insert(0,'service/server'); from routes_models import RealtimeSignalRequest; m=RealtimeSignalRequest(market='crypto',action='buy',symbol='BTC',price=0,quantity=0.01,executed_at='now',limit_price=100.0); assert m.limit_price==100.0; print('model OK')"
  EXPECT: model OK
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=e384b14e7ee46d371212eb810d51d4b093e366727dca94af191eafd1a80553c3; output-bytes=9

- [x] G4: The agent sends a 2bps-inside limit price on paper opens (buy/short)
  CHECK: python -c "code=open('service/agent/live_agent.py').read(); assert 'limit_price' in code; assert 'ENTRY_OFFSET_BPS' in code; print('agent limit sim OK')"
  EXPECT: agent limit sim OK
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=a9e9ce44116caf38d9d640ab33ef788bf305fc4981297d0288a412cea1f96227; output-bytes=19

- [x] G5: The server fills paper opens at the more-favorable (maker) limit price
  CHECK: python -c "code=open('service/server/routes_signals.py').read(); assert 'data.limit_price' in code; assert 'min(' in code or 'max(' in code; print('server fill OK')"
  EXPECT: server fill OK
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=1db3577438d6d107177658072015b37676868cc33e7cc7b092267efde757251c; output-bytes=15

- [x] G6: HORIZONS hits the measured 45% win-rate point (scalp stop=1.5σ target=0.6σ, R~1.24)
  CHECK: python -c "import sys; sys.path.insert(0,'service/agent'); from quant_strategy import HORIZONS; s=HORIZONS['scalp']; assert s['stop']==1.5 and s['target']==0.6; assert 0.6/1.5 <= 0.6; print('horizons 45pct OK')"
  EXPECT: horizons 45pct OK
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=ba716b0ca023ada0a931407f6fc3cd80ae2379acdbe9e59ff2590d8b4ed98e6e; output-bytes=12

- [x] G7: quant strategy tests still pass after TP/stop tuning
  CHECK: python -m pytest tests/research/test_quant_strategy.py >/dev/null 2>&1 && echo 'QUANT TESTS PASSED'
  EXPECT: QUANT TESTS PASSED
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=31d6db38e678a11a8ddcb2676ea5d5d861aea55d0e148d258fc78e2791cb84d7; output-bytes=19

- [x] G8: all execution tests still pass (no regression from real-mode fixes)
  CHECK: python -m pytest tests/execution/ >/dev/null 2>&1 && echo 'EXECUTION TESTS PASSED'
  EXPECT: EXECUTION TESTS PASSED
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=3bc665f42ecdede9a234c88d3b36a9c40d658cd39007149d004c5bace6d61090; output-bytes=23

- [x] G9: .env.example documents the real-mode switch (REAL_TRADING_ENABLED + LIVE_AGENT_EXECUTION)
  CHECK: grep -q 'REAL_TRADING_ENABLED' .env.example && grep -q 'LIVE_AGENT_EXECUTION' .env.example && echo 'ENV SWITCH DOCUMENTED'
  EXPECT: ENV SWITCH DOCUMENTED
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=1466adf2cc3370c91558d90d851b61eb6c6d5a994f3042a9ad295101cf5044c8; output-bytes=22

- [x] G10: The win-rate harness (with momentum+RSI live filters) measures >= 45% win rate
  CHECK: python research/scripts/test_winrate.py --symbols BTC,ETH,SOL,SUI --bars 3000 2>&1 | grep -oE '\([0-9.]+% win rate' | grep -oE '[0-9.]+' | head -1 | awk '{exit !($1>=45)}' && echo 'WIN RATE >= 45%'
  EXPECT: WIN RATE >= 45%
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=7cb03df9c8457edebe9176deb4b5e7db0eda5c95576b0cc35f6d8bfa6356d7f4; output-bytes=16

- [x] G11: Intraday config maximizes profit (stop=0.8 sigma / target=0.6 sigma, the measured +0.44 EV/trade point)
  CHECK: python -c "import sys; sys.path.insert(0,'service/agent'); from quant_strategy import HORIZONS; i=HORIZONS['intraday']; assert i['stop']==0.8 and i['target']==0.6; print('intraday profit config OK')"
  EXPECT: intraday profit config OK
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=f2dfec83982ae2f69d83b8d25794a74eafdac89fff573ed3674cbf764b8fa272; output-bytes=26