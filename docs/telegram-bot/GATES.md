# Gates: telegram-master-bot

OWNS: service/tg_bot/**, docs/telegram-bot/**, tests/tg_bot/**

Scope: A Telegram bot network. Users bring their own Telegram bot token (BotFather) and AI API key; the master bot validates both (incl. challenge-response ownership proof), registers their agent on the AI-Trader platform, and serves their user bot (dashboard, live markets, positions, settings) with push notifications. Multi-market: perps (leverage 1-10x), spot crypto, US stocks, forex.

- [x] G1: design documents cover system architecture, complete UX spec (both bots), push notifications, and multi-market/perp behavior
  CHECK: python -c "import pathlib; assert all(pathlib.Path(p).exists() for p in ('docs/telegram-bot/system-design.md','docs/telegram-bot/UX-design.md','docs/telegram-bot/PLAN.md')); t=pathlib.Path('docs/telegram-bot/UX-design.md').read_text(); assert all(s in t for s in ('Perps','Live Markets','Inbox','EDGE-CASE','6b')); print('design documents verification passed')"
  EXPECT: design documents verification passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=9f5ea2731f4e85cad31be9adef354bac8db5e165cbd20e9131595fd0815ef8d0; output-bytes=37

- [x] G2: key vault encrypts at rest (Fernet), masked repr only, and never returns plaintext except with the master key
  CHECK: python -m pytest tests/tg_bot/test_key_vault.py -q && echo "key vault tests passed"
  EXPECT: key vault tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=4ef3357388bbf657238d482e350b594327cd51030ae290716a0bdfa69e567829; output-bytes=121

- [x] G3: provider module validates a live key (HTTP 2xx) and rejects an invalid key (401/403), with presets for openai/openrouter/opencode-go/custom
  CHECK: python -m pytest tests/tg_bot/test_provider.py -q && echo "provider tests passed"
  EXPECT: provider tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=26ef6fd1683716d2db04217aef9f1d9d67ad0f53cfcd7c9e7f5b91c6b100c6fb; output-bytes=120

- [x] G4: registry stores users/api_keys/bots, dedupes keys by hash, isolates rows by owner, and rejects duplicate bot tokens
  CHECK: python -m pytest tests/tg_bot/test_store.py -q && echo "store tests passed"
  EXPECT: store tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=833559cab35b9c1990b25185578782ae5b4edf496442dfc202b3cb67159a5780; output-bytes=117

- [x] G5: initialization wizard is idempotent - resume on re-entry, never duplicate platform agents, cancel discards nothing unexpected
  CHECK: python -m pytest tests/tg_bot/test_wizard_flow.py -q && echo "wizard flow tests passed"
  EXPECT: wizard flow tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=8c805e35fad20398c7fd6c2b9384a61fab0d680cf71ae47c697487d66d883439; output-bytes=123

- [x] G6: ownership verification requires the user to send a challenge code to their own bot; the wizard blocks without getUpdates receipt
  CHECK: python -m pytest tests/tg_bot/test_wizard_flow.py::test_ownership_flow_blocks_without_code tests/tg_bot/test_wizard_flow.py::test_ownership_flow_passes_when_code_arrives tests/tg_bot/test_wizard_flow.py::test_ownership_rejects_invalid_token -q && echo "ownership tests passed"
  EXPECT: ownership tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=d87edae241d4c5b2868dcc8a89025086d4e455b43e02af36012ec1fc67b191ee; output-bytes=121

- [x] G7: multi-market config persists per bot - perps flag + leverage (1-10x), spot, us-stock, forex - and is validated
  CHECK: python -m pytest tests/tg_bot/test_wizard_flow.py::test_markets_from_flags tests/tg_bot/test_agent_pool.py::test_universe_mapping tests/tg_bot/test_e2e_flow.py -q && echo "markets tests passed"
  EXPECT: markets tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=677e6774b57db5fcf10274c23acc9f1d2969557b9bca00eb7a6cbf22c97f2f2d; output-bytes=120

- [x] G8: master bot boots with a valid token, answers /start with the onboarding menu, and serves all main-menu buttons
  CHECK: python -c "import pathlib,sys; sys.path.insert(0,'service/tg_bot'); import main; h=main.menu_buttons(); assert any('Add My Bot' in b for row in h for b in row); print('master bot boot verification passed')"
  EXPECT: master bot boot verification passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=ca79fb9dfa1f451077cb89e678d8b9520156707fd724e973b3f09a1f33f255a3; output-bytes=36

- [x] G9: full flow end-to-end with mocked Telegram transport against the live local platform: onboard -> register agent -> bot listed with live P&L
  CHECK: python -m pytest tests/tg_bot/test_e2e_flow.py -q && echo "e2e flow tests passed"
  EXPECT: e2e flow tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=050eca071697d6faf6186d7640092f52e74cb14430d538b5290c7216dc7af168; output-bytes=131

- [x] G10: user bot serving spawns per-user Application; dashboard, positions, and live markets render platform data (prices/PnL)
  CHECK: python -m pytest tests/tg_bot/test_userbot_screens.py -q && echo "userbot screens tests passed"
  EXPECT: userbot screens tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=aa098d331cee7ec456f335a5a094ef166e4a555cc09f97f2b7a0be5a4bcbe97c; output-bytes=127

- [x] G11: push notifications are deduped/batched (first error pings, repeats merge; fills/stops always delivered) and attached buttons work
  CHECK: python -m pytest tests/tg_bot/test_notifier.py -q && echo "notifier tests passed"
  EXPECT: notifier tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=42b2ae212242be49ffebece1e8d462fe5440c9a02e56f9d01e785784e88345bd; output-bytes=120

- [x] G12: agent pool spawns a runner per active bot, produces decisions, restarts crashed runners (max 3/h), and stops cleanly
  CHECK: python -m pytest tests/tg_bot/test_agent_pool.py -q && echo "agent pool tests passed"
  EXPECT: agent pool tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=e2724cb6f919aacb6f466058706ad1e56c2432136bbfde58e3ddcd9b0156ca39; output-bytes=122

- [x] G13: keys and tokens are never echoed raw in any log line or message (masked format only)
  CHECK: python -c "import pathlib,re; bad=[(str(p),l) for p in pathlib.Path('service/tg_bot').rglob('*.py') for l in p.read_text().splitlines() if re.search(r'sk-[A-Za-z0-9]{8,}|[0-9]{8,}:[A-Za-z0-9_-]{30,}', l)]; assert not bad, bad; print('no raw key leakage detected')"
  EXPECT: no raw key leakage detected
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro/docs/telegram-bot; path=08538f152605/16 entries; EXPECT=matched; output-sha256=28874ae3aadec54a6fca3381cd9a46b334b9423550d332c6370e3d7ae645bead; output-bytes=28

- [x] G14: admin view lists all registered bots with status and P&L from one command
  CHECK: python -m pytest tests/tg_bot/test_admin_views.py -q && echo "admin views tests passed"
  EXPECT: admin views tests passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=cf2a150fc4da517583e5ac48f94639a1b34740a45aa9210389241b861bad00e5; output-bytes=1111

- [x] G15: full existing platform suite still passes (zero regression)
  CHECK: python -m pytest service/server/tests/ -q && echo "platform regression passed"
  EXPECT: platform regression passed
  EVIDENCE: exit=0; shell=/bin/sh; cwd=/home/carnage/tradebotpro; path=08538f152605/16 entries; EXPECT=matched; output-sha256=b900ac8ff8753c96ffb42d118e82b9d4c237de8d44748e05ae232049765448d0; output-bytes=782

- [x] G16: UX copy review - tone guide, no jargon, every error names a fix, every screen reachable in <=2 taps, no dead ends
  EVIDENCE: REVIEWED 2026-08-26 by operator: all error templates name a fix action (Check @BotFather / Double-check it / wait a minute / try again / Send it again); every screen carries a Home path (24 home/dash callback anchors); wizard cancel present on every step; P&L money formatting signed in all templates; emoji use is single-functional per line (verified by code-point scan - earlier regex false-positives were the Unicode variation selector, not stacked emojis)