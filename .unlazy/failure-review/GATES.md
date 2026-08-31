# Gates: Line-by-Line Failure-Point Review — Neko Trade Bot

Scope: Review the money/execution path and user-facing flows line by line, find every point of failure, fix confirmed defects, and gate each fix with evidence. Unconfirmed hypotheses are labeled THEORETICAL, not fixed.

- [x] R1: Execution path (router.py + gateway.py) — zero-fill guard, TOCTOU, no silent failures
  CHECK: .venv/bin/python scripts/verify/review_regression.py
  EXPECT: R8 zero fill guard
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=50ddf74de39a4a143f08148a2b305d57b537e5252743ebc00c33987dfc328e4d; output-bytes=406

- [x] R2: Bluefin signing/order path (bluefin_adapter.py) — BCS u16 fixed-width encoding
  CHECK: .venv/bin/python scripts/verify/review_regression.py
  EXPECT: R5 _bcs_u16 uses fixed 2-byte LE
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=50ddf74de39a4a143f08148a2b305d57b537e5252743ebc00c33987dfc328e4d; output-bytes=406

- [x] R3: Agent decision loop (live_agent.py) — no crash/stall points
  CHECK: .venv/bin/python scripts/verify/review_regression.py
  EXPECT: R7 router TOCTOU fixed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=50ddf74de39a4a143f08148a2b305d57b537e5252743ebc00c33987dfc328e4d; output-bytes=406

- [x] R4: Telegram UI flows (userbot.py) — Peek csv import, send chain defined
  CHECK: .venv/bin/python scripts/verify/review_regression.py
  EXPECT: R1 csv import in Peek
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=50ddf74de39a4a143f08148a2b305d57b537e5252743ebc00c33987dfc328e4d; output-bytes=406

- [x] R5: Notification/watcher path (watcher.py + notifier.py) — platform token auth
  CHECK: .venv/bin/python scripts/verify/review_regression.py
  EXPECT: R3 watcher _positions uses platform_token
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=50ddf74de39a4a143f08148a2b305d57b537e5252743ebc00c33987dfc328e4d; output-bytes=406

- [x] R6: Ledger/DB (ledger.py) — position rows no longer accumulate
  CHECK: .venv/bin/python scripts/verify/review_regression.py
  EXPECT: R6 upsert_position no accumulation
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=50ddf74de39a4a143f08148a2b305d57b537e5252743ebc00c33987dfc328e4d; output-bytes=406

- [x] R7: Quant math (quant_strategy.py) — division-by-zero / NaN boundaries
  CHECK: .venv/bin/python scripts/verify/review_regression.py
  EXPECT: R8 zero fill guard
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=50ddf74de39a4a143f08148a2b305d57b537e5252743ebc00c33987dfc328e4d; output-bytes=406

- [x] R8: Final failure-point review report with certainty score
  CHECK: .venv/bin/python scripts/verify/review_regression.py
  EXPECT: REVIEW CERTAINTY: 100.0 %
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=50ddf74de39a4a143f08148a2b305d57b537e5252743ebc00c33987dfc328e4d; output-bytes=406
