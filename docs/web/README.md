# Neko web app

The web app replaces Telegram as the interface. It has two sides:

| Page | Who it is for | What it does |
|---|---|---|
| `/` | Clients and the public | Shows the paper-trading record: win rate against the break-even it needs, results after fees, equity line, recent trades, the four go-live checks, and the engine replay. Collects emails to notify. |
| `/desk` | You, in a second tab | Signals for a watchlist you choose across crypto, stocks, indices, forex and commodities. Each market shows the rule's past record there; every unmuted signal is paper-traded automatically; sides that have lost money on a market are muted. Works out the position size for your account and risk. |
| `/weekly` | You, planning the week | For forex. You enter a pair, direction, stop and target; it shows what a plan of that shape has done in every week since 1999, the win rate it needs, the pair's normal weekly range, and the lot size for your risk. Includes a live TradingView chart. |
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

## The desk

One rule makes every signal: on 4-hour bars, long when the 10-day average
crosses above the 40-day average, short on the opposite cross, stop 4 ATR from
entry. Prices come from Hyperliquid (crypto, plus the stock, index, forex and
commodity perpetuals on its xyz venue), so the server needs
`api.hyperliquid.xyz` reachable.

- **Record.** The rule is replayed on each market's own history and shown
  beside the signal. `service/server/tests/test_desk.py` checks the replay
  against `research/scripts/strategy_search.py` trade for trade.
- **Muting.** A side (long or short) is muted on a market once it has five or
  more past trades there with a negative total after costs. The replay is
  redone as bars arrive, so a muted side comes back if it starts working.
- **Paper account.** A "Desk signals" paper account trades every unmuted
  fresh cross on the watchlist, equal weight, no leverage.

What the tests found: on crypto the rule made money over 2.3 years. On the
stock, index, forex and commodity markets it lost over the one year of history
available (268 trades, 18% won). A version with a target as close as the stop
wins about half the time and nets roughly zero. Treat signals outside crypto
as unproven.

## The weekly forex planner

`research/scripts/forex_weekly_search.py` tested 14 weekly rules (follow the
trend, fade the last move, breakouts, moving averages) on 14 pairs over 27
years of European Central Bank daily rates. None picked the week's direction
better than a coin flip: 48.6% to 51.6% right, all slightly negative after
costs, in both halves of the history. So the planner does not pick a
direction. It measures the trader's own plan:

- **Normal week.** How far the pair has been moving in a week (last 26
  weeks). The week closed inside that range in about 69% of past weeks.
- **Shape replay.** The stop and target are expressed in normal weeks and
  replayed on every week since 1999: how often the target came first, the
  stop came first, or the week ran out, with the win rate the shape needs to
  break even. A closer target wins more often and needs a higher win rate;
  across shapes the average result is close to zero, minus costs.
- **Size.** Lots and units for the chosen account risk.

Trading costs are left out by default; the form has an optional cost field for
a broker's spread and commission. Without costs the 14 rules average almost
exactly zero per week (`--no-costs`), so costs are not what hides an edge.

Sentiment was tested too (`research/scripts/forex_sentiment_search.py`): the
US regulator's weekly Commitments of Traders report, 1999 to now, seven
currencies, nine rules (trade with or against large speculators, fade or join
their extremes, follow their changes, fade small traders, follow hedgers).
None was right more than half the time in both halves of the history; each
worked in one half and reversed in the other. It is therefore not used.

Rates come from `www.ecb.europa.eu` (one fixing per business day), so stops
and targets are checked on daily fixings, not intraday prices. The chart is
TradingView's embeddable widget, loaded by the viewer's browser; no figures
come from it.

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
python research/scripts/forex_weekly_search.py    # weekly forex rules, 27 years
python research/scripts/forex_sentiment_search.py # positioning (sentiment) rules
python research/scripts/test_llm_decisions.py     # does the AI model add anything
python research/scripts/replay_live_engine.py     # the scenario engine with costs
```

## Files

- `web/` static pages, no build step (`assets/neko.css` holds the design tokens)
- `service/server/routes_web.py` routes, sign-in, bot control
- `service/server/web_core.py` trade pairing, statistics, go-live checks
- `service/server/web_email.py` goals and the daily email
- `service/agent/trend_agent.py` the slow trend agent
- `service/server/desk.py`, `market_data.py` desk signals, records, muting; market data
- `service/server/forex_weekly.py` weekly forex planner
- `service/server/tests/test_web.py` tests
