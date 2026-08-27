# Plan: Telegram Master Bot — Depth Tree & Build Order

Ledger: `docs/telegram-bot/GATES.md` · Design: `docs/telegram-bot/system-design.md`

## Deliverable tree

```
telegram-master-bot (G1, G6, G10)
├── leaf-A  key infrastructure              → G2, G3
│   ├── key_vault.py        Fernet vault + masked repr
│   └── provider.py         presets, key validation, chat-completion helper
├── leaf-B  registry + platform glue        → G4, G7
│   ├── store.py            users/api_keys/bots/events, hash dedupe, tg_id isolation
│   └── platform_client.py  register agent, positions, leaderboard, trade, price
├── leaf-C  Telegram UX                     → G5, G6, G9, G12
│   ├── main.py             polling app + /start + /menu + /admin
│   ├── handlers/start.py   onboarding + How It Works pages
│   ├── handlers/wizard.py  init conversation (7 states, cancel, idempotent)
│   ├── handlers/menu.py    dashboard, bot controls, settings, leaderboard, signals
│   └── messages.py         all copy, tone guide, masking rules
├── leaf-D  agent pool                      → G8
│   ├── agent_pool.py       spawn/stop/health/restart-limit per bot
│   └── runner_adapter.py   live_agent.py reuse: user key/provider/config via env/file
└── leaf-E  ops                             → G11
    ├── handlers/admin.py   /admin list, ban, force-stop
    └── notifier.py         fill/stop/error/daily-summary pushes, dedup
```

## Dependencies
- leaf-A ← (nothing)
- leaf-B ← leaf-A (stores encrypted keys, uses provider validation in wizard only)
- leaf-C ← leaf-A, leaf-B
- leaf-D ← leaf-A (runner needs user key), leaf-B (platform calls)
- leaf-E ← leaf-B, leaf-C, leaf-D

## Waves
- Wave 1: leaf-A, leaf-B (parallel — disjoint files: service/tg_bot/key_vault.py, provider.py, store.py, platform_client.py + their tests)
- Wave 2: leaf-C (needs A+B verified)
- Wave 3: leaf-D (needs A+B)
- Wave 4: leaf-E (needs C+D)
- Integration gates: G7 (e2e), G10 (regression), G12 (manual review)

## Verification rules
- Every leaf: unit tests under tests/tg_bot/ mirroring the leaf's module names.
- G7 e2e runs against the LIVE local platform (localhost:8000) with a throwaway test key; the test user is cleaned up afterward (agent kept for leaderboard realism or marked disabled).
- G10 reruns the untouched platform suite to prove zero regression.
- Manual gates (G12) reviewed by the operator before release.