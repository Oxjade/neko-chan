# Neko Lite

The live terminal and the week's calls as a light, static web app. The pages
work everything out in the browser from public market data. There is no
Python, no database and no sign-in, and the only server code is one small
function.

- `/` the live terminal, with a Basic and an Advanced screen
- `/week` "This week: long, short or stay out"

## What it is, next to the Python version

The Python web app (`service/`, `web/`) computes every number on the server.
Neko Lite ports the two screens above and the calculations behind them to
TypeScript, so the site can be hosted as static files.

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

Not included (they stay in the Python version): sign-in, the desk and its
paper trading, the weekly forex planner, the console and its bots, the public
page, and the emails.

Different on purpose:

- A market's long price history (5000 four-hour bars) is downloaded once, in
  the background, a few markets at a time so the exchange's rate limit is
  respected. Until a market's history has arrived its dashboard note reads
  "Loading price history." and its worksheet row is missing; the worksheet
  title says how many are still to come. The history is kept in the browser
  (IndexedDB), so later visits download only the bars since the last one.
- Refresh timers stop while the tab is hidden and everything refreshes at
  once when it is shown again.
- The forex board comes from the app's one server function (below). If that
  function is missing or down, only the forex board is empty.
- Closed price bars are fetched when a bar closes (and once more a minute
  later, in case its last trades were still arriving) instead of on a fixed
  timer, and just after midnight on Monday the mood index is looked for every
  minute until it publishes the new day, so the new week's call appears as
  soon as its inputs exist.

## Where the data comes from

| Data | Source | Read by |
| --- | --- | --- |
| Prices, candles, order book, funding | Hyperliquid public API and stream | the browser |
| Market mood | alternative.me Fear & Greed index | the browser |
| Forex board | European Central Bank daily reference rates | `api/fx.ts` |
| Tested record of the rules | `src/core/data/*.json` | bundled with the app |

The ECB's file cannot be read from a browser on another site, so `api/fx.ts`
downloads it, works out the board (about 4 kB) and lets Vercel's CDN keep the
answer for an hour.

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
- tests of what is new here: the history store, the request limiter and the
  forex function;
- the parity check, when its recording is present (see below).

### Parity with the Python reference

`tests/parity/make_fixtures.py` freezes one moment, records every raw upstream
response the Python modules ask for, runs the Python functions from those
recorded responses only, and saves both. `tests/parity/parity.test.ts` replays
the same responses through the TypeScript core and compares every field:
numbers to 1e-9, everything else exactly.

```
../.venv/bin/python tests/parity/make_fixtures.py            # about four minutes, 15 MB, git-ignored
../.venv/bin/python tests/parity/make_fixtures.py monday     # a second moment, kept beside the first
npm test
```

Without a recording the parity test is skipped; with several, each is
checked. Record again and re-run it after any change to a rule, in Python or
here. A recording made on a Monday is worth having: the entry price then comes
from the live price instead of a closed candle.

## Deploy on Vercel

1. Import the repository in Vercel.
2. Set **Root Directory** to `neko-lite`.
3. Leave the rest as detected (framework Vite, build `npm run build`, output
   `dist`). No environment variables are needed.

Vercel serves `index.html` at `/`, `week.html` at `/week` (clean URLs are
switched on in `vercel.json`) and runs `api/fx.ts` as a function at `/api/fx`.

## How it is put together

```
index.html, week.html     the two pages
src/ui/                   the screens (DOM only; they import src/core/api.ts and nothing else from the core)
src/core/api.ts           getWeek, getScoreboard, getTerminal, getAdvanced, getChart, getBook
src/core/                 data loading and calculation; never touches the page, runs under test in Node
src/core/data/            extracts of the research tables
api/fx.ts                 the one server function
scripts/                  extract-tables.mjs
tests/                    unit tests, reference answers from Python, the parity check
```

Each function in `src/core/api.ts` returns what the matching Python function
returns (`weekly_calls.this_week`, `weekly_calls.scoreboard`,
`terminal.snapshot`, `terminal_adv.snapshot`, `terminal.chart`,
`market_data.order_book`), with one extra field: `pending` on the advanced
snapshot, the number of worksheet markets still downloading.

## Network use

Hyperliquid allows each IP address 1200 "weight" a minute. The core keeps a
running count and stays under it: what the screen is waiting for goes first,
long downloads take what is left (three at a time), and a "slow down" from the
exchange is waited out and retried.

Measured in Chromium against the live sources (November 2026). The counts
include the browser's own permission checks for Hyperliquid (one extra tiny
request for most calls) and the five files of the site itself:

| Load | Requests | Downloaded |
| --- | --- | --- |
| Week page, first visit | 17 | 0.35 MB |
| Basic terminal, first visit, until the screen is drawn (about 2 seconds) | 19 | 0.37 MB |
| ... then its chart and the dashboard's 8 price histories, in the background | 32 in all | 4.3 MB in all |
| Advanced terminal, first visit, until all 30 worksheet markets are in (about 3 minutes) | 107 | 14 MB, of which 12.9 MB is price history downloaded once |
| Advanced terminal, later visit | 13 | 0.2 MB |

The three minutes on a first Advanced visit are the exchange's rate limit, not
the connection: thirty long histories cost about 2200 weight, and the screen
is usable throughout.

While a terminal tab is open and visible it refreshes the market catalogue
every ten seconds, which is most of its steady traffic (about 0.8 MB a
minute; Hyperliquid sends that list uncompressed). The live stream adds 0.3 to
0.6 MB a minute. A hidden tab stops all of its refresh timers and keeps only
the live stream connected.
