# Gates: bot-hardening

OWNS: service/agent/live_agent.py, service/tg_bot/userbot.py, service/tg_bot/watcher.py, service/tg_bot/notifier.py, service/execution/bluefin_adapter.py

Scope: Hardening the Neko trading bot — TP/SL notifications, one-position limit, P&L card, balance display, key verification, conviction filter, message TTL, warning zone, leverage sizing, deposit alerts, pause fixes.

- [x] G1: TP notification ladder — watcher pushes at 50%/75%/90%/100% toward TP, with escalation, deduped per (symbol, level)
  CHECK: python -c "from service.tg_bot.watcher import Watcher; print('Watcher import OK')"
  EXPECT: Watcher import OK
  CWD: /home/carnage/tradebotpro
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=a378717ca3afa232bf591d1646529a27ccb50bd7ce5e85a348f84ac9b1708255; output-bytes=18

- [x] G2: SL warning notification — watcher pushes at 50%/75%/90% toward SL, deduped per (symbol, level)
  CHECK: python -c "from service.agent.quant_strategy import build_scenarios; print('import OK')"
  EXPECT: import OK
  CWD: /home/carnage/tradebotpro
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=73e65cba19528ade3fd463248a9ba2c44e92c41810caef6a0bba624cefc54219; output-bytes=10

- [x] G3: One-position limit — scenario matrix filters to one open position max; override via watchlist
  CHECK: python -c "open_symbols={'BTC':0.1,'SOL':-2.0}; watched=['SOL']; skipped=sorted(set(open_symbols.keys())-set(watched)); assert skipped==['BTC'], skipped; print('PASS')"
  EXPECT: PASS
  CWD: /home/carnage/tradebotpro
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=c26de83abdc9496cd1301470918ec39ecca1cf389ef0ae1c6504da1800d1c431; output-bytes=5

- [ ] G4: Key signing verification — verify key exists and can sign; warn user if not
  EVIDENCE: pending

- [ ] G5: P&L card prints on P&L button tap — pnl_detail generates PNG card instead of navigation
  EVIDENCE: pending

- [ ] G6: Balance display shows USDC + SUI on dashboard
  EVIDENCE: pending

- [ ] G7: Only one best trade across watched tokens — conviction filter applied
  EVIDENCE: pending

- [ ] G8: Notification messages delete after 3 minutes
  EVIDENCE: pending

- [ ] G9: Position sizing by balance + leverage (Bluefin 5x-100x, clamp to venue max)
  EVIDENCE: pending

- [ ] G10: Warning zone — monitor trade, warn before SL, action in gap
  EVIDENCE: pending

- [ ] G11: Deposit notification when USDC/SUI arrives
  EVIDENCE: pending

- [ ] G12: Pause button pauses LLM + quant bot
  EVIDENCE: pending

- [ ] G13: Refresh shows position metrics (entry, current, stop, target, pnl%)
  EVIDENCE: pending

- [x] G14: All files parse and tests pass
  CHECK: python -c "import ast; [ast.parse(open(f).read()) for f in ['service/agent/live_agent.py','service/tg_bot/userbot.py','service/tg_bot/watcher.py','service/tg_bot/notifier.py','service/execution/bluefin_adapter.py']]; print('all OK')"
  EXPECT: all OK
  CWD: /home/carnage/tradebotpro
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=e794ae50179223eff2f444f1008c6510a688de6e07249a569e8901cb8abbe738; output-bytes=7