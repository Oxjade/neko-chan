# Neko Lite

The live terminal, the week's calls and the forex planner as a light, static
web app. The pages work everything out in the browser from public market
data. There is no Python, no database and no sign-in, and the only server code
is one small function.

- `/` the live terminal, with a Basic and an Advanced screen
- `/week` "This week: long, short or stay out"
- `/forex` the forex planner: your own plan in pips, what that shape has done
  since 1999, and how big to trade it
- `/guide` "How to use Neko", a plain-language guide to all of the above

## What it is, next to the Python version

The Python web app (`service/`, `web/`) computes every number on the server.
Neko Lite ports the terminal, the week page and the forex planner, and the
calculations behind them, to TypeScript, so the site can be hosted as static
files. The guide exists only here.

Included, and unchanged in layout, wording and numbers:

- the weekly calls (long, short or stay out per coin, suggested leverage, the
  stop, the take profit on shorts, and a warning when a call is too small for
  the exchange to accept) and the week-by-week scoreboard
- the terminal: model positions, the "what to do now" feed, the market
  dashboard, the recommendation gauge, the candle chart with the 10-day and
  40-day averages, and the one-second live chart
- every Advanced panel: sessions, movers, breadth, mood, the 30-market
  worksheet, order book and trades, signal matrix, positions and risk,
  thirty-day performance, correlation, liquidation rate by leverage, week by
  week, funding and open interest, and the forex board
- live prices, order book and trades straight from Hyperliquid's stream
- the forex planner (`/weekly` in the Python version): pair, direction, stop
  and target in pips, the broker's spread, the odds since 1999, the lot size
  and what one pip is worth

Not included (they stay in the Python version): sign-in, the desk and its
paper trading, the console and its bots, the public page, and the emails.

Different on purpose:

- A market's long price history (5000 four-hour bars) is downloaded once, in
  the background, a few markets at a time so the exchange's rate limit is
  respected. Until a market's history has arrived its dashboard note reads
  "Loading price history." and its worksheet row is missing; the worksheet
  title says how many are still to come. The history is kept in the browser
  (IndexedDB), so later visits download only the bars since the last one.
- Refresh timers stop while the tab is hidden and everything refreshes at
  once when it is shown again.
- The forex figures come from the app's one server function (below). If that
  function is missing or down, only the forex board and the forex page are
  affected.
- On the forex page the pair's TradingView chart is loaded only when its
  button is pressed. It is another company's widget of a few megabytes; the
  Python version loads it with the page.
- Closed price bars are fetched when a bar closes (and once more a minute
  later, in case its last trades were still arriving) instead of on a fixed
  timer, and just after midnight on Monday the mood index is looked for every
  minute until it publishes the new day, so the new week's call appears as
  soon as its inputs exist.
- On Mondays the week's entry price (the open of Monday's daily candle) is
  read from the daily candles already fetched, which include the day still
  forming, instead of a request of its own. The value is the same.

## Where the data comes from

| Data | Source | Read by |
| --- | --- | --- |
| Prices, candles, order book, funding | Hyperliquid public API and stream | the browser |
| Market mood | alternative.me Fear & Greed index | the browser |
| Forex board and forex plans | European Central Bank daily reference rates | `api/fx.ts` |
| Tested record of the rules | `src/core/data/*.json` | bundled with the app |

The ECB's file cannot be read from a browser on another site, and its 27 years
of daily rates are too much to ship, so `api/fx.ts` downloads it and answers
two questions, letting Vercel's CDN keep each answer for an hour:

- `/api/fx` the board: every pair's last rate, the last finished week, this
  week so far and a normal week in pips, and its range (about 5 kB);
- `/api/fx?pair=EURUSD&direction=long&stop=50&target=100&spread=0` what a
  plan of that shape has done since 1999 (about 2 kB). A request it cannot
  check is answered with a 400 and a sentence that can be shown as it is.

Weeks run from one Friday's rate to the next, and everything measured per week
uses finished weeks only: from Monday the week being traded is reported apart
and is not counted as one of a plan's past weeks. The function decides this
by today's date (UTC).

A plan request carries no account size and no risk. Those stay in the
browser, where the position size and the pip value are worked out
(`src/core/forex.ts`), so one stored answer serves everyone who asks about
the same shape.

The files in `src/core/data/` are small extracts of the research tables in
`research/exports/tables/`. After the research scripts have rewritten those
tables, refresh the extracts and commit them:

```
node scripts/extract-tables.mjs
```

## Run it

Node 22 is needed.

```
npm install
npm run dev
```

Open the address Vite prints (usually http://localhost:5173). `/week` and
`/api/fx` work in dev and in `npm run preview` the same way they do on Vercel.

## Test it

```
npm run typecheck
npm test
npm run build
```

`npm test` runs:

- the Python unit tests for the terminal, the advanced panels, the weekly
  calls, the mood and the trend replay, ported with the same inputs and
  expected numbers (`tests/*.test.ts`);
- checks that the number handling matches Python and NumPy to the last digit,
  against answers saved by `tests/fixtures/make_reference.py`;
- the Python tests of the forex planner, on a made-up table of rates whose
  answers Python wrote down (`tests/fx.test.ts`, `tests/forexPlan.test.ts`);
- tests of what is new here: the history store, the request limiter and the
  forex function;
- the parity check, when its recording is present (see below).

### Parity with the Python reference

`tests/parity/make_fixtures.py` freezes one moment, records every raw upstream
response the Python modules ask for, runs the Python functions from those
recorded responses only, and saves both. `tests/parity/parity.test.ts` replays
the same responses through the TypeScript core and compares every field:
numbers to 1e-9, everything else exactly. It covers the week, the scoreboard,
both terminal screens, three charts, two order books, the forex overview and
four forex plans.

```
../.venv/bin/python tests/parity/make_fixtures.py            # about four minutes, 15 MB, git-ignored
../.venv/bin/python tests/parity/make_fixtures.py monday     # a second moment, kept beside the first
../.venv/bin/python tests/parity/make_fixtures.py --replay   # no fetching: today's Python over a recording already made
npm test
```

Without a recording the parity test is skipped; with several, each is
checked. Record again and re-run it after any change to a rule, in Python or
here. A recording made on a Monday is worth having: the entry price then comes
from the open of the daily candle that is still forming, not from a finished
one.

## Deploy on Vercel

1. Import the repository in Vercel.
2. Set **Root Directory** to `neko-lite`.
3. Leave the rest as detected (framework Vite, build `npm run build`, output
   `dist`). No environment variables are needed.

Vercel serves `index.html` at `/`, and `week.html`, `forex.html` and
`guide.html` at `/week`, `/forex` and `/guide` (clean URLs are switched on in
`vercel.json`), and runs `api/fx.ts` as a function at `/api/fx`.

## How it is put together

```
index.html, week.html,    the four pages (the guide is plain HTML with no script)
forex.html, guide.html
src/ui/                   the screens (DOM only; they import src/core/api.ts and nothing else from the core)
src/core/api.ts           getWeek, getScoreboard, getTerminal, getAdvanced, getChart, getBook,
                          getForexOverview, getForexPlan
src/styles/               neko.css and terminal.css from the Python app, unchanged, and lite.css for what is new here
src/core/                 data loading and calculation; never touches the page, runs under test in Node
src/core/data/            extracts of the research tables
api/fx.ts                 the one server function
scripts/                  extract-tables.mjs
tests/                    unit tests, reference answers from Python, the parity check
```

Each function in `src/core/api.ts` returns what the matching Python function
returns (`weekly_calls.this_week`, `weekly_calls.scoreboard`,
`terminal.snapshot`, `terminal_adv.snapshot`, `terminal.chart`,
`market_data.order_book`, `forex_weekly.overview`, `forex_weekly.plan`), with
one extra field: `pending` on the advanced snapshot, the number of worksheet
markets still downloading.

## Network use

Hyperliquid allows each IP address 1200 "weight" a minute. The core keeps a
running count and stays under it: what the screen is waiting for goes first,
long downloads take what is left (three at a time), and a "slow down" from the
exchange is waited out and retried.

Measured in Chromium against the live sources (November 2026). The counts
include the browser's own permission checks for Hyperliquid (one extra tiny
request for most calls) and the handful of files of the site itself:

| Load | Requests | Downloaded |
| --- | --- | --- |
| Week page, first visit | 18 | 0.35 MB |
| Basic terminal, first visit, until the screen is drawn (about 2 seconds) | 19 | 0.37 MB |
| ... then its chart and the dashboard's 8 price histories, in the background | 32 in all | 4.3 MB in all |
| Advanced terminal, first visit, until all 30 worksheet markets are in (2 to 3 minutes) | 80 to 110 | 14 MB, of which 12.9 MB is price history downloaded once |
| Advanced terminal, later visit | 12 | 0.14 MB |
| Forex page, first visit, with two plans checked | 8 | 0.04 MB |
| Guide | 3 | 0.02 MB |

The two to three minutes on a first Advanced visit are the exchange's rate
limit, not the connection: thirty long histories cost about 2200 weight, and
the screen is usable throughout.

While a terminal tab is open and visible it refreshes the market catalogue
every ten seconds, which is most of its steady traffic (about 0.8 MB a
minute; Hyperliquid sends that list uncompressed). The live stream adds 0.3 to
0.6 MB a minute. A hidden tab stops all of its refresh timers and keeps only
the live stream connected.
