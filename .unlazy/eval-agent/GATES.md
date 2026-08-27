# Gates: Neko agent evaluation

OWNS: research/scripts/evaluate_agent_accuracy.py, research/scripts/audit_pnl.py, research/scripts/evaluate_walk_forward.py, tests/research/**, research/exports/tables/agent_accuracy*.csv, research/exports/tables/agent_pnl_audit*.csv, research/exports/tables/agent_walkforward*.csv, research/exports/figures/agent_*.svg, research/agent_evaluation_report.md

Scope: code-aware quantitative audit of Neko's AI trading agent producing accuracy, PnL, walk-forward, look-ahead, and regime results plus a final report.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node /home/carnage/.config/opencode/skills/unlazy/scripts/gate-lint.mjs .unlazy/eval-agent/GATES.md
  CWD: /home/carnage/tradebotpro
  EXPECT: LINT OK
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=48630b7361dd44ee870917b12c3d19b9d7bdea738aaca16bb04d4cab83b772d2; output-bytes=8

- [x] G1: directional accuracy is computed from the recorded live-agent decisions and realized prices, with per-horizon and per-market results
  CHECK: .venv/bin/python research/scripts/evaluate_agent_accuracy.py --log research/exports/live_agent_log.csv --db service/server/data/clawtrader.db --out research/exports/tables/agent_accuracy.csv --summary research/exports/tables/agent_accuracy_summary.csv
  CWD: /home/carnage/tradebotpro
  EXPECT: accuracy evaluation passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=ee1218f6e4f0d83ded26182b8e80afe95abb8720fe6cd274b660c954a0f8e36a; output-bytes=2521

- [x] G2: accuracy metrics are reproduced independently from the raw source (live log + realized prices) inside the tool itself
  CHECK: .venv/bin/python research/scripts/evaluate_agent_accuracy.py --log research/exports/live_agent_log.csv --db service/server/data/clawtrader.db --out research/exports/tables/agent_accuracy.csv --summary research/exports/tables/agent_accuracy_summary.csv --self-check
  CWD: /home/carnage/tradebotpro
  EXPECT: self-check passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=fc0608d1f3f68298506e70570ff13415b5b7c0209f6f0c2a8042307c4b8f0eaf; output-bytes=2642

- [x] G3: PnL audit independently derives expected PnL, compares to platform cash accounting, checks the account equation, and prints PASS or FAIL with every discrepancy
  CHECK: .venv/bin/python research/scripts/audit_pnl.py --db service/server/data/clawtrader.db --log research/exports/live_agent_log.csv --out research/exports/tables/agent_pnl_audit.csv
  CWD: /home/carnage/tradebotpro
  EXPECT: pnl audit passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=c8bd1a22b99bfea3b55a327edf2f2646c0bc082986af7d90872b44625eaf1b34; output-bytes=2703

- [x] G4: the account equation (ending = starting + deposits + realized net PnL - withdrawals) reconciles for the evaluated live agent
  CHECK: .venv/bin/python research/scripts/audit_pnl.py --db service/server/data/clawtrader.db --log research/exports/live_agent_log.csv --out research/exports/tables/agent_pnl_audit.csv --account-equation
  CWD: /home/carnage/tradebotpro
  EXPECT: account equation passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=89f1c3f3c0e98c22d079b50d6e83003b8e71f091a36fc33dc467dfb53f894fb0; output-bytes=2881

- [x] G5: realistic execution scenarios (optimistic/baseline/adverse) are produced from the actual paper engine's fill prices, not fabricated assumptions
  CHECK: .venv/bin/python research/scripts/audit_pnl.py --db service/server/data/clawtrader.db --log research/exports/live_agent_log.csv --out research/exports/tables/agent_pnl_audit.csv --scenarios
  CWD: /home/carnage/tradebotpro
  EXPECT: scenario audit passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=d8b24ca4dd86b85029373116a9d7593d3d3c6539c8bb6e2c6cfab8d947dd1eff; output-bytes=2903

- [x] G6: walk-forward evaluation produces chronological OOS results for the live observational window and baselines, with the LLM replay limitation documented
  CHECK: .venv/bin/python research/scripts/evaluate_walk_forward.py --log research/exports/live_agent_log.csv --db service/server/data/clawtrader.db --out research/exports/tables/agent_walkforward.csv
  CWD: /home/carnage/tradebotpro
  EXPECT: walk-forward evaluation passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=44f3a00fd11a69319e7b8453d699def94149a136a867f383c6f507a0c70336c8; output-bytes=4845

- [x] G7: the explicit look-ahead test exists, passes, and a known-leaky positive control fails it
  CHECK: .venv/bin/python -m pytest tests/research/test_lookahead.py -q
  CWD: /home/carnage/tradebotpro
  EXPECT: 9 passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=2cc5b81ad387eb96a05e6abf2fdaef87bd922045d5ce1c8f32909f938f2fc867; output-bytes=98

- [x] G8: the final report contains the required sections (executive verdict, four separate questions, output block, market-by-market, regimes, failure analysis)
  CHECK: .venv/bin/python -c "import re,pathlib; p=pathlib.Path('research/agent_evaluation_report.md').read_text(); toks=['Executive Verdict','PROMISING BUT INSUFFICIENT EVIDENCE','Is Neko good at predicting direction?','Is Neko profitable after realistic costs?','Is the PnL implementation mathematically correct?','Does Neko outperform simple baselines out-of-sample?','PnL Accounting Audit','Look-Ahead Audit','CRYPTO','FOREX','STOCKS','PERPS']; missing=[t for t in toks if t not in p]; assert not missing, f'missing: {missing}'; print('report sections verified')"
  CWD: /home/carnage/tradebotpro
  EXPECT: report sections verified
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=89ff9d5283550b6baa9df5ab0ef40de479e0d69d8b2d4d74453836f32b543129; output-bytes=25

- [x] G9: regime evaluation results are computed with a documented classifier and written to a table
  CHECK: .venv/bin/python -c "import pathlib; p=pathlib.Path('research/exports/tables/agent_regimes.csv'); assert p.exists() and p.stat().st_size>0, 'agent_regimes.csv missing'; rows=p.read_text().splitlines(); assert rows[0].startswith('regime'), f'bad header {rows[0]}'; print('regime table verified')"
  CWD: /home/carnage/tradebotpro
  EXPECT: regime table verified
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=7f525e5d951a0e2aa7142eaeb035c2e73e1a07de44b66855f8514d9f9851a692; output-bytes=22

- [x] G10: production changes limited to the two authorized defect fixes (service/agent/live_agent.py D2 logging, service/server/tasks.py D8 forex refresh)
  CHECK: .venv/bin/python -c "import subprocess; out=subprocess.run(['git','status','--porcelain'],capture_output=True,text=True,cwd='.').stdout; prod=[l for l in out.splitlines() if l.strip() and ('service/agent' in l or 'service/server' in l or 'service/execution' in l or 'service/tg_bot' in l)]; others=[l for l in prod if not ('service/agent/live_agent.py' in l or 'service/server/tasks.py' in l)]; assert not others, f'unexpected production changes: {others}'; print('production isolation verified')"
  CWD: /home/carnage/tradebotpro
  EXPECT: production isolation verified
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=8f31dce947b2/15 entries; EXPECT=matched; output-sha256=322d4cd2efc8186297c75c2da0774a40859e2965f3258f5bd0fd9a0372031b49; output-bytes=30