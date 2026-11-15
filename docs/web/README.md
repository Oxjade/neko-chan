# Neko web app

The web app replaces Telegram as the interface. It has two sides:

| Page | Who it is for | What it does |
|---|---|---|
| `/` | Clients and the public | Shows the paper-trading record: win rate against the break-even it needs, results after fees, equity line, recent trades, the four go-live checks, and the engine replay. Collects emails to notify. |
| `/terminal` | You, in a second tab, all week | A live dark terminal: model positions for this week's calls with live profit and distance to the stop, a candlestick chart with the entry, stop and target drawn on it, a market dashboard, a recommendation gauge, and a desk feed that says what to do right now. |
| `/week` | Someone new to trading | The week's calls on perps: long, short or stay out for Bitcoin and Ether, each with a suggested leverage. Shows why, the steps to take with your amount, a week-by-week scoreboard, and how far to trust it. |
| `/desk` | You, in a second tab | Signals for a watchlist you choose across crypto, stocks, indices, forex and commodities. Each market shows the rule's past record there; every unmuted signal is paper-traded automatically; sides that have lost money on a market are muted. Works out the position size for your account and risk. |
| `/weekly` | You, planning the week | For forex. You enter a pair, direction, stop and target; it shows what a plan of that shape has done in every week since 1999, the win rate it needs, the pair's normal weekly range, and the lot size for your risk. Includes a live TradingView chart. |
| `/console` | You, the operator | Create, start and pause paper bots; see positions, decisions, closed trades and the go-live checks per bot; save the AI model key; choose which bots are public. |

It is paper trading only. The console cannot switch a bot to live trading and
`start_web.py` forces `LIVE_AGENT_EXECUTION=0`.

## Run it

Only a sign-in password is required:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r service/requirements.txt
NEKO_PASSWORD='a long random password' python service/start_web.py     # http://localhost:8000
```

The app keeps its database and a secret key in `service/data` (or
`NEKO_DATA_DIR`). The key is created on first start; back it up with the
database, because stored AI keys cannot be read without it.

Container: the repository's default `Dockerfile` is the web app. Build it,
set `NEKO_PASSWORD`, mount a volume at `/data`, expose port 8000 and put it
behind HTTPS. The server needs to reach `api.hyperliquid.xyz`,
`api.alternative.me` and `www.ecb.europa.eu`.

No Telegram is involved. A few internal settings still carry `TG_` names from
the older bot code; `start_web.py` fills them in. Do not run the old Telegram
master bot next to the web app unless `WEB_MANAGE_AGENTS=0`.

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

## The live terminal

`/terminal` keeps the week's calls on screen and live. The server recomputes
positions, advice and recommendations every five seconds
(`service/server/terminal.py`); a direct price stream from Hyperliquid moves
the numbers and the chart in between, falling back to the five-second poll if
the stream cannot connect.

- **Position tracker.** Model positions: what the weekly amount would be
  doing had the calls been followed from Monday's open. Live profit, and how
  far the price is from the stop. The terminal does not know what you hold.
- **Desk feed.** What to do now, most urgent first: stop hit (closed, do not
  re-enter), close to the stop (leave it), take profit hit (closed), a long in
  profit (hold to Monday), a call too small for the exchange, the weekly close
  coming up, do not enter late, and a preview of Monday's call.
- **Take profit and stop advice is tested.** Shorts take profit one normal
  week from the entry; longs have no take profit. Moving the stop to
  break-even or trailing it did not help, so the feed never says to tighten a
  stop. See "Stop loss, take profit, limit entry" below.
- **Too small to place.** The exchange refuses orders under 10 USD. A call
  whose share of the weekly amount, times its leverage, is below that is shown
  as too small with the weekly amount it needs, and is left out of the model
  positions and the scoreboard.
- **Recommendation.** For the selected market: which way the tested signals
  lean (mood, last four weeks, 10-day against 40-day average) and whether the
  platform makes the call. Stocks, forex and commodities show "no call".
- **Chart.** 1h, 4h or 1d candles with the two averages and this week's
  entry, stop and take profit. **1S** is a one-second chart: the exchange
  keeps no one-second history, so the browser builds those candles itself from
  the live stream (best bid and offer, plus trades for volume), starting when
  the terminal opens and keeping the last five minutes.
- **Live feed.** Every market on screen is followed on the exchange's best
  bid and offer feed, which updates several times a second, so prices, profit
  and the chart move continuously. The all-markets price feed alone only
  ticks about every ten seconds.

### Basic and Advanced

The switch in the top bar chooses the screen; the choice is remembered in the
browser. **Basic** is the five panels above. **Advanced** is a dense quant
terminal on one black sheet (`service/server/terminal_adv.py`,
`web/assets/terminal_adv.js`), refreshed every 20 seconds, with the same desk
feed and chart plus:

| Panel | What it shows | Source |
|---|---|---|
| Sessions | World clocks and whether New York, London, Tokyo and forex are open | the browser's clock (regular hours, no holidays) |
| Movers | Most up and most down today per asset class, markets under $250k daily volume left out | Hyperliquid, about 130 markets |
| Breadth | How many markets are up, down or flat today per class | same |
| Market mood | Today's Fear & Greed reading over 120 days | alternative.me |
| Market worksheet | 30 markets: price, day, 7-day and 30-day change as a heat map, trend, volatility, funding, open interest, the slow trend rule and its record. Selecting a row loads the chart and order book. | 4-hour candles |
| Order book and trades | Live depth and the trade tape for the selected market | Hyperliquid stream, REST fallback every 3 seconds |
| Signal matrix | Every coin and side: called or not, leverage, share of weeks right in each half of the history | `crypto_long_short.json` |
| Positions and risk | Exposure, distance to the stop and to liquidation, loss if every stop is hit, and the stop rule against holding with no stop | model positions, `crypto_exits.json` |
| Thirty days | Seven markets rebased to 100 | 4-hour candles |
| Correlation | How closely eight markets moved together over 30 days | 4-hour candles |
| Liquidation rate by leverage | Share of traded weeks wiped out, and the average week, from 1x to 30x; the suggested level is outlined | `crypto_long_short.json` |
| Week by week | The last twelve finished weeks and the running total | daily candles |
| Funding and open interest | Per coin | Hyperliquid |
| Forex board | Last week, a normal week's range and trend for 14 pairs | ECB reference rates |

Nothing is filled in for effect: there is no news or economic calendar because
the platform has no source for one. Each panel fails on its own, so a source
that is down empties its panel and leaves the rest of the screen working.

## This week: long, short or stay out

For someone who does not trade. Once a week, per coin (Bitcoin, Ether, Solana,
Sui), on perps:

- **long** when the Fear & Greed index is 50 or more and the coin rose over
  the last four weeks;
- **short** when the index is below 50 and the coin fell over the last four
  weeks;
- **stay out** otherwise.

`research/scripts/crypto_long_short.py` scores each side on its own, per coin
and per half of the history (Aug 2020 to now). A side is only called where it
was right more often than not and profitable in **both** halves:

| | Weeks right, to 2023 | Since 2023 | Called |
|---|---|---|---|
| Long Bitcoin | 65% | 54% | yes |
| Long Ether | 62% | 51% | yes |
| Short Ether | 56% | 57% | yes |
| Short Bitcoin | 52% | 48% | no: the platform says stay out |
| Long Solana | 60% | 52% | yes (2x) |
| Short Solana | 60% | 53% | yes (1x) |
| Long Sui | 56% | 33% | no |
| Short Sui | 33% | 54% | no |

The **suggested leverage** is the highest tested level at which the weekly
stake was liquidated in at most one week in fifty: 3x for Bitcoin and Ether,
2x for long Solana and 1x for short Solana. Each coin's halves are its own
history split in two, so Sui (listed in 2023) is judged on much less data.
From Saturday the page also previews next Monday's calls.
At 10x the stake was liquidated in 16 to 37% of weeks, at 30x in about 70%.

The long side carries most of the edge (average traded week about +1.5 to
+4.6% unleveraged against +0.5 to +1.7% for shorting Ether). Without leverage
or shorting, holding in "long" weeks only turned 1 USD into 24.63 against
5.85 for always holding, with a worst fall of 27% against 75%
(`research/scripts/crypto_in_or_out.py`). It is six years of one market, so
treat the size of the gains as luck-prone. The daily email starts with the
week's calls.

### Stop loss, take profit, limit entry

`research/scripts/crypto_exits.py` replays every traded week of the called
sides at 3x with each combination, measured in "normal weeks" (the coin's
typical weekly move over the previous half year):

- **Take profit, all sides together** at 1 to 2 normal weeks lowered the
  average result; the profit comes from the few large weeks. The only target
  that did no harm was 3 normal weeks away, and that is reached in about 3%
  of weeks, so it was no use as a target.
- **Take profit by side** (`research/scripts/crypto_take_profit.py`, 523
  traded weeks, every called side at its suggested leverage, 83 plans: full
  exits, part exits, stops that move up). Longs and shorts behave differently:
  - **Shorts: take profit one normal week from the entry.** Reached in 31% of
    short weeks. Shorts ended ahead in 55% of weeks with it against 52%
    without, and the average week was no lower in either half of the history
    (1.1 against 1.0 cents per dollar overall). Falls tend to bounce. 30 of
    the 83 plans were as good as the stop alone for shorts.
  - **Longs: no take profit.** None of the 83 plans was as good as the stop
    alone. A target one normal week away cut the average long week from 5.2
    to 2.5 cents per dollar. Rises tend to keep going.
  The gain on shorts is small and a week that takes profit can still leave
  money behind; what it buys is a target that is actually reached.
  Only plans that can be placed as resting orders on Monday are used: the
  exchange's 10 USD minimum rules out closing part of a small position.
- **Tight stops** (0.5 to 0.75 normal weeks) were hit by ordinary swings and
  cut the average by a third to a half.
- **A stop one normal week away** costs a little (3.1 against 3.7 cents per
  dollar per traded week) and caps the worst week at 57% of the weekly
  amount, against 100%. It is suggested as protection.
- **Limit entries** at a better price than Monday's open traded fewer weeks
  and made less in total: the missed weeks included too many winners.

The page shows an order ticket per call: position size, entry, the stop
price, the take profit where there is one, and what is made or lost at each. The scoreboard applies the same
stop.

### Week by week

The page also scores each finished week: what a fresh weekly amount would
have made following the calls, at the suggested leverage or any level from 1x
to 30x, with a running total and what to expect per call at that leverage.
Liquidation is checked against each day's low using the exchange's
maintenance margin; fees and a typical funding cost are included.

`research/scripts/leverage_on_rule.py` (Bitcoin, six years): about 62% of
weeks have no trade, 21% end ahead and 17% behind at up to 10x; a month ends
ahead 31% of the time and behind 24%; three months, 47% and 27%. At 30x the
weekly amount was lost in 70% of the weeks traded and the six-year total was
a loss. A version that trades every week (long or short by mood) ended ahead
in about half of weeks with an average gain of a few cents on 18 USD. No rule
tested made a profit every week.

## Crypto mood (weekly)

`research/scripts/crypto_weekly_search.py` ran the weekly test on crypto: ten
coins, Aug 2020 to now, 14 price rules plus the Fear & Greed index and funding
rates as sentiment, each judged on the first and second half of the history.

- Going **with** the mood (long when the index is 50 or more, short below)
  was right 55% of weeks in the first half and 50% in the second, averaging
  +2.8% and +1.0% per week, against +2.0% and +0.75% for simply holding.
- At extremes (under 25 or over 75) it was right 55% and 59% of weeks.
- Buying fear and selling greed was the worst rule tested (45% and 41%).
- Following the last four weeks' move was right 54% and 51%.
- Funding rates showed nothing dependable (three coins, history from 2023).

Two of 23 rules beat holding in both halves, both of them the mood rule. The
second half is not statistically solid, the ten coins move together, and the
index is partly built from price momentum, so it overlaps with the trend
rule. The desk shows this week's reading, the rule's direction and this
record; it needs `api.alternative.me` reachable.

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
python research/scripts/crypto_weekly_search.py   # weekly crypto rules incl. mood and funding
python research/scripts/crypto_in_or_out.py       # hold-or-cash rules against always holding
python research/scripts/leverage_on_rule.py       # the weekly rule on perps with leverage
python research/scripts/crypto_long_short.py      # long and short sides separately; calls and leverage
python research/scripts/crypto_exits.py           # stop loss, take profit and limit entry on the calls
python research/scripts/crypto_take_profit.py     # nearer take-profit plans, longs and shorts apart
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
- `service/server/crypto_mood.py` weekly crypto mood reading and its record
- `service/server/weekly_calls.py` the week's long, short or stay-out calls and the scoreboard
- `service/server/terminal.py` live model positions, advice, recommendations and chart data
- `service/server/terminal_adv.py` the Advanced screen's panels
- `web/assets/terminal.css`, `terminal.js` the terminal's dark theme and live page
- `web/assets/terminal_adv.js`, `terminal_util.js` the Advanced screen and shared formatting
- `service/server/tests/test_web.py` tests
