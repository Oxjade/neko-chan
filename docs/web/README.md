# Neko web app

The web app replaces Telegram as the interface. It has two sides:

| Page | Who it is for | What it does |
|---|---|---|
| `/` | Clients and the public | Shows the paper-trading record: win rate against the break-even it needs, results after fees, equity line, recent trades, the four go-live checks, and the engine replay. Collects emails to notify. |
| `/console` | You, the operator | Create, start and pause paper bots; see positions, decisions, closed trades and the go-live checks per bot; save the AI model key; choose which bots are public. |

It is paper trading only. The console cannot switch a bot to live trading and
`start_web.py` forces `LIVE_AGENT_EXECUTION=0`.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r service/requirements.txt
export WEB_OPERATOR_PASSWORD='a long random password'
export TG_VAULT_MASTER_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
python service/start_web.py          # http://localhost:8000
```

Keep `TG_VAULT_MASTER_KEY` somewhere safe: stored AI keys cannot be read without it.

Container: `docker build -f Dockerfile.web -t neko-web .` then run with the two
variables above and a volume on `/data`. Put it behind HTTPS before exposing it.

Do not run it next to the Telegram master bot unless `WEB_MANAGE_AGENTS=0`:
each has its own agent pool and two pools start two agents per bot.

## The go-live checks

A bot's record passes only when all four hold (`service/server/web_core.py`):

1. at least 100 closed trades;
2. the **low end** of the win rate's 95% interval is above the break-even win
   rate implied by its own average win and average loss;
3. the average trade is profitable after fees;
4. worst drawdown under 15%.

## Replay the engine with real costs

```bash
python research/scripts/replay_live_engine.py
python research/scripts/replay_live_engine.py --csv BTC=btc_5m.csv --csv SOL=sol_5m.csv
```

It walks the scenario engine the live agent actually runs over 5-minute bars,
resolves each entry against its stop and target, and reports the win rate with
its interval, the break-even win rate at three cost levels, and a
random-direction baseline. The public page shows the result.

## Files

- `web/` static pages, no build step (`assets/neko.css` holds the design tokens)
- `service/server/routes_web.py` routes, sign-in, bot control
- `service/server/web_core.py` trade pairing, statistics, go-live checks
- `service/server/tests/test_web.py` tests
