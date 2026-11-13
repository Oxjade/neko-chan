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

## Trading styles

- **Slow trend** (default, no AI key needed): on 4-hour bars, long when the
  10-day average crosses above the 40-day average, short on the opposite
  cross, 4-ATR stop, no leverage. It is the only rule that was profitable
  after costs in both halves of `research/scripts/strategy_search.py`. It
  trades about once per coin every three to four weeks and wins roughly a
  third of its trades; the profit comes from a few large winners. The check
  period result is not statistically significant, so treat it as the best
  candidate, not a proven edge.
- **Scalp / intraday / both**: the original scenario engine with an AI model
  choosing the trade. In testing it won about 35% of trades against a 52%
  break-even (`research/exports/tables/llm_decision_test_*.json`).

## Goals and the daily email

When you create a bot you can give it a starting amount, a goal amount and a
number of days ("start with 10 USD, reach 40 USD in 14 days"). The console
then shows progress and the growth per day still needed.

A progress email goes out once a day when these are set (see `.env.example`):

```bash
export REPORT_EMAIL_TO='you@example.com'
export SMTP_HOST='smtp.gmail.com' SMTP_PORT=587
export SMTP_USER='you@gmail.com' SMTP_PASSWORD='an app password'
```

"Send report now" in the console sends one immediately, which is the way to
check the mail settings.

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

## Research scripts

```bash
python research/scripts/strategy_search.py        # rule search, choose/check split
python research/scripts/test_llm_decisions.py     # does the AI model add anything
python research/scripts/replay_live_engine.py     # the scenario engine with costs
```

## Files

- `web/` static pages, no build step (`assets/neko.css` holds the design tokens)
- `service/server/routes_web.py` routes, sign-in, bot control
- `service/server/web_core.py` trade pairing, statistics, go-live checks
- `service/server/web_email.py` goals and the daily email
- `service/agent/trend_agent.py` the slow trend agent
- `service/server/tests/test_web.py` tests
