// Neko terminal: a live working screen for this week's calls.
// The core works out positions, advice and recommendations every few seconds
// (so the rules live in one tested place); a direct stream from the exchange
// (prices, best bid and offer, trades, order book) keeps the numbers and the
// chart moving in between.

import {
  getAdvanced, getBook, getChart, getLivePrices, getTerminal, type Candle, type Position, type TerminalMarket,
} from "../core/api";
import { h, replace, whileVisible } from "./dom";
import { buildAdvanced, paintAdvanced, paintBook, paintLivePanels, paintSessions } from "./terminal_adv";
import {
  FAST_MS, PANEL_MS, SILENT_MS, changeFrom, decimalsOf, directPrices, exact, feedState, isStale, liveBreadth,
  liveFunding, liveMarkets, liveMovers, livePerformance, liveRisk, nextDirectWait, livePosition as repriced,
  updatesPerSecond,
} from "./terminal_live";
import {
  COLORS, compact, cue, heat, pct, px, tone, usd, type Elements, type Helpers, type Interval, type TerminalState,
} from "./terminal_util";

const app = document.getElementById("app") as HTMLElement;
const POLL_MS = 5000;
const CHART_MS = 60000;
const STREAM_URL = "wss://api.hyperliquid.xyz/ws";
const INTERVALS: Array<[Interval, string]> = [["1s", "1S"], ["1h", "1H"], ["4h", "4H"], ["1d", "1D"]];
const TICK_BARS = 300;                // the one-second chart keeps the last five minutes
const CLOCKS = [["UTC", "UTC"], ["New York", "America/New_York"], ["London", "Europe/London"],
                ["Tokyo", "Asia/Tokyo"]];
const ADV_MS = 20000;
const ADV_LOADING_MS = 4000;          // while worksheet markets are still downloading, look again sooner
const BOOK_MS = 3000;

const state: TerminalState = {
  snap: null, chart: null, selected: "ETH", interval: "4h", stake: loadStake(),
  prices: {}, stream: "connecting", lastPoll: 0, error: null,
  log: [], seen: new Set(), hover: null, frame: null,
  mode: loadMode(), adv: null, book: null, tape: [], bookSymbol: null, tradeSymbol: null,
  tradeBacklog: false,
  watch: new Set(["ETH"]),
  quotes: {}, context: {}, prev: {}, places: {}, priceAt: {},
  feedAt: 0, openedAt: Date.now(), outageAt: 0, streamAt: 0, notLiveSince: Date.now(), directAt: 0,
  messages: 0, rates: [], rate: 0,
  dirty: new Set(),
};

// Basic or advanced screen: a per-browser preference.
function loadMode(): "basic" | "advanced" {
  try {
    return localStorage.getItem("neko-terminal-mode") === "advanced" ? "advanced" : "basic";
  } catch {
    return "basic";
  }
}

function setMode(mode: "basic" | "advanced"): void {
  if (state.mode === mode) return;
  state.mode = mode;
  try {
    localStorage.setItem("neko-terminal-mode", mode);
  } catch { /* storage unavailable */ }
  frame();
  paint();
  loadChart();
  restartTimers();
  watchBook();
  if (mode === "advanced") pollAdvanced();
}

// Weekly amount: a per-browser convenience shared with the weekly page.
function loadStake(): number {
  try {
    const saved = JSON.parse(localStorage.getItem("neko-week-plan") || "null");
    if (saved && saved.stake > 0) return saved.stake;
  } catch { /* storage unavailable */ }
  return 18;
}

function saveStake(): void {
  try {
    const saved = JSON.parse(localStorage.getItem("neko-week-plan") || "null") || { leverage: 0 };
    localStorage.setItem("neko-week-plan", JSON.stringify({ ...saved, stake: state.stake }));
  } catch { /* storage unavailable */ }
}

// ---- helpers -----------------------------------------------------------------

function live(symbol: string, fallback: number | null | undefined): number | null | undefined {
  const v = state.prices[symbol];
  return v === undefined ? fallback : v;
}

const message = (err: unknown) => (err instanceof Error ? err.message : String(err));

// ---- one-second candles ------------------------------------------------------

// The exchange has no one-second history, so these are built here from the live
// stream: every price update and every trade of a watched market lands in the
// candle of the second it arrived in. They start when the page opens.
const ticks = new Map<string, Candle[]>();          // symbol -> candles, oldest first

// A price has arrived for a market, from the stream or from asking directly.
function gotPrice(symbol: string, price: number, places: number): void {
  state.prices[symbol] = price;
  state.priceAt[symbol] = state.feedAt = Date.now();
  if (places > (state.places[symbol] || 0)) state.places[symbol] = places;
  state.dirty.add(symbol);
  if (state.watch.has(symbol)) tickPrice(symbol, price);
}

function tickPrice(symbol: string, price: number, size = 0): void {
  let candles = ticks.get(symbol);
  if (!candles) ticks.set(symbol, candles = []);
  const t = Math.floor(Date.now() / 1000) * 1000;
  let last = candles[candles.length - 1];
  if (!last || t > last.t) {
    // If prices stopped arriving after this market's last candle, the chart picks up
    // from the new price: the seconds in between are not filled in as if they were data.
    const resumed = last && last.t < state.outageAt;
    const open = last && !resumed ? last.c : price;
    // quiet seconds get flat candles, so the time axis stays even
    for (let s = last && !resumed ? Math.max(last.t + 1000, t - (TICK_BARS - 1) * 1000) : t; s < t; s += 1000) {
      candles.push({ t: s, o: open, h: open, l: open, c: open, v: 0 });
    }
    last = { t, o: open, h: open, l: open, c: open, v: 0 };
    candles.push(last);
    if (candles.length > TICK_BARS) candles.splice(0, candles.length - TICK_BARS);
  }
  last.h = Math.max(last.h, price);
  last.l = Math.min(last.l, price);
  last.c = price;
  last.v += size;
}

// Markets worth keeping one-second candles for: the ones a screen can select.
function watchMarkets(): void {
  const watch = new Set([state.selected]);
  if (state.snap) for (const m of state.snap.markets) watch.add(m.symbol);
  if (state.adv) for (const m of state.adv.worksheet) watch.add(m.symbol);
  state.watch = watch;
  watchQuotes();
}

// Two feeds for every watched market. Best bid and offer arrive several times
// a second and keep prices, profit and the one-second chart moving. The
// market's own figures (funding, open interest, yesterday's price, the mid)
// arrive about once a second, even when nothing trades. The all-markets price
// feed is far too slow to rely on.
const quoted = new Set<string>();
const FEEDS = ["bbo", "activeAssetCtx"];

function watchQuotes(): void {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  for (const symbol of [...quoted]) {
    if (state.watch.has(symbol)) continue;
    for (const type of FEEDS) send({ method: "unsubscribe", subscription: { type, coin: symbol } });
    quoted.delete(symbol);
  }
  for (const symbol of state.watch) {
    if (quoted.has(symbol)) continue;
    for (const type of FEEDS) send({ method: "subscribe", subscription: { type, coin: symbol } });
    quoted.add(symbol);
  }
}

// What the chart draws: candles from the exchange, or the one-second candles built here.
function chartNow(): { candles: Candle[]; emas: { "10d"?: Array<number | null>; "40d"?: Array<number | null> } } | null {
  if (state.interval !== "1s") return state.chart;
  return { candles: ticks.get(state.selected) || [], emas: {} };
}

// True when no price has arrived for the selected market for too long to call it live.
function noLivePrices(): boolean {
  return isStale(Math.max(state.priceAt[state.selected] || 0, state.openedAt), Date.now());
}

// Once a second: measure the stream, keep prices coming, start the next candle
// even if the market is quiet, and redraw.
function everySecond(): void {
  const now = Date.now();
  state.rates.push(state.messages);
  state.messages = 0;
  if (state.rates.length > 3) state.rates.shift();
  state.rate = updatesPerSecond(state.rates);
  if (isStale(Math.max(state.feedAt, state.openedAt), now)) state.outageAt = now;
  // a stream that has gone silent without closing is closed here, so that it reconnects
  if (state.stream === "live" && now - state.streamAt > SILENT_MS && socket) socket.close();
  askDirectly();
  paintBar();
  if (state.interval !== "1s") return;
  if (noLivePrices()) {
    // nothing is arriving: say so, and do not draw seconds that were never observed
    if (state.hover === null) paintReadout();
    return;
  }
  const candles = ticks.get(state.selected);
  const price = candles && candles.length ? candles[candles.length - 1].c : state.prices[state.selected];
  if (price === undefined) return;
  tickPrice(state.selected, price);
  if (state.hover === null) paintReadout();
  draw();
}

// ---- prices without the stream --------------------------------------------------

// When the stream is not delivering (a firewall or proxy can block it), prices are
// asked for once a second straight from the exchange, until the stream is back. On
// the advanced screen the same question keeps the markets the stream is not
// followed for up to date, so the movers and breadth can be counted live. The
// questions go through the core's request budget like every other.
const direct = { busy: false, wait: FAST_MS, next: 0 };

async function askDirectly(): Promise<void> {
  const started = Date.now();
  if (direct.busy || started < direct.next) return;
  const dexes = directPrices(state.stream, paused || document.hidden, started - state.notLiveSince, state.mode);
  if (!dexes.length) return;
  direct.busy = true;
  let ok = false;
  try {
    const answers = await Promise.all(dexes.map(getLivePrices));
    for (const answer of answers) {
      for (const [symbol, text] of Object.entries(answer)) {
        const price = Number(text);
        if (!(price > 0)) continue;
        // with the stream up, it is the better source for the markets it is followed for
        if (state.stream === "live" && quoted.has(symbol)) continue;
        gotPrice(symbol, price, decimalsOf(text));
      }
    }
    if (state.stream !== "live") state.directAt = Date.now();
    ok = true;
    schedule();
  } catch { /* no answer: wait longer before asking again */ }
  direct.wait = nextDirectWait(direct.wait, ok);
  direct.next = started + direct.wait - 200;
  direct.busy = false;
}

// ---- data --------------------------------------------------------------------

async function poll(): Promise<void> {
  try {
    state.snap = await getTerminal(state.stake);
    state.lastPoll = Date.now();
    state.error = null;
    const now = Date.now();
    for (const m of state.snap.markets) {
      // the snapshot's price stands in until a fresher one has arrived directly
      if (m.price && isStale(state.priceAt[m.symbol] || 0, now)) {
        state.prices[m.symbol] = m.price;
        state.dirty.add(m.symbol);
      }
      if (m.prev_day_price && !state.context[m.symbol]) state.prev[m.symbol] = m.prev_day_price;
    }
    noteAdvice();
    watchMarkets();
    paint();
  } catch (err) {
    state.error = message(err);
    paintBar();
  }
}

let forexLooks = 0;

async function pollAdvanced(): Promise<void> {
  if (state.mode !== "advanced") return;
  if (timers.advSoon) clearTimeout(timers.advSoon);
  timers.advSoon = null;
  try {
    state.adv = await getAdvanced(state.stake);
    for (const m of state.adv.worksheet) {
      if (m.prev_day_price && !state.context[m.symbol]) state.prev[m.symbol] = m.prev_day_price;
    }
    watchMarkets();
    paintAdvanced(el, state, helpers);
    paintTicker();
    paintLive();
    paintPanels();
    // Worksheet rows appear as each market's history arrives: look again soon while some are loading.
    // The forex board may also take a moment longer than the rest the first time.
    const forexDue = !state.adv.forex.length && forexLooks < 3;
    if (forexDue) forexLooks += 1;
    if ((state.adv.pending || forexDue) && state.mode === "advanced" && !paused) {
      timers.advSoon = setTimeout(pollAdvanced, ADV_LOADING_MS);
    }
  } catch (err) {
    state.error = message(err);
  }
}

// Order book without the stream: ask the exchange every few seconds.
async function pollBook(): Promise<void> {
  if (state.mode !== "advanced" || state.stream === "live") return;
  try {
    const book = await getBook(state.selected);
    if (book.symbol === state.selected) {
      state.book = book;
      paintBook(el, state);
    }
  } catch { /* the next poll will try again */ }
}

function send(payload: unknown): void {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
}

// Follow the selected market on the stream: its trades always (they feed the
// one-second chart and the tape), its order book only on the advanced screen.
function watchBook(): void {
  if (state.tradeSymbol !== state.selected) {
    if (state.tradeSymbol) {
      send({ method: "unsubscribe", subscription: { type: "trades", coin: state.tradeSymbol } });
    }
    send({ method: "subscribe", subscription: { type: "trades", coin: state.selected } });
    state.tradeSymbol = state.selected;
    state.tradeBacklog = true;
    state.tape = [];
  }
  const want = state.mode === "advanced" ? state.selected : null;
  if (state.bookSymbol && state.bookSymbol !== want) {
    send({ method: "unsubscribe", subscription: { type: "l2Book", coin: state.bookSymbol } });
  }
  if (want !== state.bookSymbol) {
    state.book = null;
    if (want) send({ method: "subscribe", subscription: { type: "l2Book", coin: want } });
    state.bookSymbol = want;
    paintBook(el, state);
    pollBook();
  }
}

async function loadChart(): Promise<void> {
  if (state.interval === "1s") {                  // built in the browser; nothing to fetch
    paintChartHead();
    draw();
    return;
  }
  const symbol = state.selected;
  const interval = state.interval;
  let chart: TerminalState["chart"] = null;
  let failed: string | null = null;
  try {
    chart = await getChart(symbol, interval);
  } catch (err) {
    failed = message(err);
  }
  // The market or interval may have been changed while this was on its way.
  if (symbol !== state.selected || interval !== state.interval) return;
  state.chart = chart;
  if (failed) state.error = failed;
  paintChartHead();
  draw();
}

function noteAdvice(): void {
  if (!state.snap) return;
  const time = new Date().toISOString().slice(11, 19);
  for (const a of state.snap.advice) {
    if (state.seen.has(a.key)) continue;
    state.seen.add(a.key);
    if (a.level === "info") continue;                 // the log records changes that matter
    state.log.unshift({ time, title: a.title });
  }
  state.log = state.log.slice(0, 30);
}

// Direct price stream. If it cannot connect, the 5-second poll still keeps prices fresh.
let socket: WebSocket | null = null;
let retry = 1000;
let reconnect: ReturnType<typeof setTimeout> | null = null;

interface StreamLevel { px: string; sz: string }
interface StreamTrade { coin: string; time: number; px: string; sz: string; side: string }
interface StreamContext {
  funding?: string;
  openInterest?: string;
  prevDayPx?: string;
  dayNtlVlm?: string;
  markPx?: string;
  midPx?: string | null;
}
interface StreamMessage {
  channel?: string;
  data?: {
    coin?: string;
    levels?: StreamLevel[][];
    bbo?: Array<StreamLevel | null>;
    mids?: Record<string, string>;
    ctx?: StreamContext;
  } | StreamTrade[];
}

function connect(): void {
  reconnect = null;
  let opened: WebSocket;
  try {
    opened = new WebSocket(STREAM_URL);
  } catch {
    state.stream = "poll";
    return;
  }
  socket = opened;
  opened.addEventListener("open", () => {
    retry = 1000;
    opened.send(JSON.stringify({ method: "subscribe", subscription: { type: "allMids" } }));
    opened.send(JSON.stringify({ method: "subscribe", subscription: { type: "allMids", dex: "xyz" } }));
    state.bookSymbol = null;                      // subscriptions do not survive a reconnect
    state.tradeSymbol = null;
    quoted.clear();
    watchBook();
    watchQuotes();
  });
  opened.addEventListener("message", (event) => {
    let msg: StreamMessage;
    try {
      msg = JSON.parse(String(event.data));
    } catch {
      return;
    }
    state.messages += 1;
    state.streamAt = Date.now();
    const data = msg.data;
    if (msg.channel === "trades" && Array.isArray(data)) {
      // The first batch after subscribing is a backlog of earlier trades. It
      // belongs on the tape, but not in the candle of the current second.
      const backlog = state.tradeBacklog && data.some((t) => t.coin === state.tradeSymbol);
      for (const t of data) {
        if (t.coin !== state.tradeSymbol) continue;
        // the price and size are kept as the exchange wrote them, beside their numbers
        state.tape.unshift({ time: t.time, px: Number(t.px), sz: Number(t.sz), buy: t.side === "B",
                             text: t.px, size: t.sz });
        if (!backlog) {
          tickPrice(t.coin, Number(t.px), Number(t.sz));
          state.priceAt[t.coin] = state.feedAt = Date.now();
          state.dirty.add(t.coin);
        }
      }
      if (backlog) state.tradeBacklog = false;
      state.tape = state.tape.slice(0, 16);
      scheduleBook();
      schedule();
      return;
    }
    if (!data || Array.isArray(data)) return;
    if (msg.channel === "l2Book" && data.coin === state.bookSymbol && data.coin && data.levels) {
      const side = (rows: StreamLevel[]) => rows.slice(0, 12).map((r) => ({ px: Number(r.px), sz: Number(r.sz) }));
      state.book = { symbol: data.coin, bids: side(data.levels[0]), asks: side(data.levels[1]) };
      scheduleBook();
      return;
    }
    if (msg.channel === "bbo" && Array.isArray(data.bbo) && data.coin) {
      const [bid, ask] = data.bbo;
      const price = bid && ask ? (Number(bid.px) + Number(ask.px)) / 2 : Number((bid || ask || { px: NaN }).px);
      if (!(price > 0)) return;
      nowLive();
      state.quotes[data.coin] = { bid: bid ? { px: bid.px, sz: bid.sz } : null,
                                  ask: ask ? { px: ask.px, sz: ask.sz } : null };
      // the mid can sit half a tick between the two, so it gets one more decimal than they have
      const tick = Math.max(bid ? decimalsOf(bid.px) : 0, ask ? decimalsOf(ask.px) : 0);
      gotPrice(data.coin, price, tick + 1);
      schedule();
      return;
    }
    if (msg.channel === "activeAssetCtx" && data.ctx && data.coin) {
      const ctx = data.ctx;
      nowLive();
      state.context[data.coin] = { funding: Number(ctx.funding), openInterest: Number(ctx.openInterest),
                                   dayVolume: Number(ctx.dayNtlVlm) };
      if (Number(ctx.prevDayPx) > 0) state.prev[data.coin] = Number(ctx.prevDayPx);
      const mid = ctx.midPx || ctx.markPx;
      if (mid && Number(mid) > 0) gotPrice(data.coin, Number(mid), decimalsOf(mid));
      else state.dirty.add(data.coin);
      schedule();
      return;
    }
    if (msg.channel !== "allMids" || !data.mids) return;
    nowLive();
    for (const [symbol, value] of Object.entries(data.mids)) {
      const price = Number(value);
      if (price > 0) gotPrice(symbol, price, decimalsOf(value));
    }
    schedule();
  });
  opened.addEventListener("close", () => {
    if (socket !== opened) return;
    if (state.stream === "live") state.notLiveSince = Date.now();
    state.stream = "poll";
    state.quotes = {};                            // a quote is only true while the stream is up
    paintBar();
    schedule();
    // While the tab is hidden nothing is retried; showing the tab again reconnects.
    if (!paused) reconnect = setTimeout(connect, retry = Math.min(retry * 2, 30000));
  });
  opened.addEventListener("error", () => opened.close());
}

function nowLive(): void {
  if (state.stream === "live") return;
  state.stream = "live";
  paintBar();
}

let bookFrame: number | null = null;

function scheduleBook(): void {
  if (bookFrame) return;
  bookFrame = requestAnimationFrame(() => {
    bookFrame = null;
    paintBook(el, state);
    paintTape();
  });
}

// Panels that are rebuilt or re-ranked (movers, breadth, risk, funding, the thirty-day
// lines) follow the live figures a few times a second, and not at all in a hidden tab.
let panelTimer: ReturnType<typeof setTimeout> | null = null;

function panelsSoon(): void {
  if (panelTimer || state.mode !== "advanced" || paused || document.hidden) return;
  panelTimer = setTimeout(() => {
    panelTimer = null;
    paintPanels();
  }, PANEL_MS);
}

function paintPanels(): void {
  if (state.mode !== "advanced" || !state.adv || !state.snap || !el.mov) return;
  const priceOf = (symbol: string) => state.prices[symbol];
  const markets = liveMarkets(state.adv.universe || [], priceOf, (symbol) => state.prev[symbol]);
  paintLivePanels(el, {
    movers: markets.length ? liveMovers(markets) : state.adv.movers,
    breadth: markets.length ? liveBreadth(markets) : state.adv.breadth,
    risk: liveRisk(state.adv.risk, state.snap.positions.map(livePosition)),
    exits: state.adv.exits,
    funding: liveFunding(state.adv.funding, (symbol) => state.context[symbol], priceOf),
    performance: livePerformance(state.adv.performance, state.adv.performance_start, priceOf),
  });
}

// Price ticks arrive many times a second; repaint the live numbers at most once per frame.
function schedule(): void {
  if (state.frame) return;
  state.frame = requestAnimationFrame(() => {
    state.frame = null;
    // the chart is redrawn when its own market moved, not for every tick of every market
    const moved = state.dirty.has(state.selected);
    paintLive();
    if (moved) draw();
  });
}

// ---- frame -------------------------------------------------------------------

const el: Elements = {};

function stakeForm(): HTMLElement {
  const stake = h("input", { type: "text", inputmode: "decimal", value: String(state.stake),
                             "aria-label": "Weekly amount in USD" });
  return h("form", { class: "t-inline", novalidate: true,
    onsubmit: (event) => {
      event.preventDefault();
      const value = Number(stake.value);
      if (!(value > 0)) {
        stake.value = String(state.stake);
        return;
      }
      state.stake = value;
      saveStake();
      stake.blur();
      poll();
      pollAdvanced();
    } }, h("span", { class: "dim" }, "Weekly $"), stake, h("button", { class: "button quiet", type: "submit" }, "Set"));
}

function chartPanel(): HTMLElement {
  el.chartHead = h("div", { class: "t-chart-head" });
  el.quote = h("div", { class: "t-readout t-quote", role: "group", "aria-label": "Live quote" });
  el.tape = h("div", { class: "t-tape", role: "group", "aria-label": "Latest trades" });
  el.readout = h("div", { class: "t-readout" });
  const canvas = h("canvas", { role: "img", "aria-label": "Price chart" });
  el.canvas = canvas;
  el.canvasBox = h("div", { class: "t-canvas" }, canvas);
  canvas.addEventListener("pointermove", onHover);
  canvas.addEventListener("pointerleave", () => { state.hover = null; paintReadout(); draw(); });
  new ResizeObserver(draw).observe(el.canvasBox);
  return h("section", { class: "t-panel t-chart" },
    el.chartHead, el.quote, el.readout, el.canvasBox, el.tape,
    h("div", { class: "t-legend" },
      h("span", null, h("i", { style: `background:${COLORS.amber}` }), "10-day average"),
      h("span", null, h("i", { style: `background:${COLORS.violet}` }), "40-day average"),
      h("span", null, h("i", { style: `background:${COLORS.cyan}` }), "Entry"),
      h("span", null, h("i", { style: `background:${COLORS.down}` }), "Stop loss"),
      h("span", null, h("i", { style: `background:${COLORS.up}` }), "Take profit")));
}

const FOOT = "Positions are model positions: what the weekly amount would be doing had this week's calls been followed from Monday's open. " +
  "Calls come from six years of past prices and have been right a little over half the time. Prices: Hyperliquid.";

function frame(): void {
  for (const key of Object.keys(el)) delete el[key];
  el.bar = h("header", { class: "t-panel t-bar" });
  el.ticker = h("div", { class: "t-panel t-ticker", role: "group", "aria-label": "Watched markets, live" });
  el.top = h("div", { class: "t-top" }, el.bar, el.ticker);
  el.feed = h("div", { class: "t-body" });
  if (state.mode === "advanced") {
    replace(app, buildAdvanced(el, { chartPanel, stakeForm }), h("p", { class: "t-foot" }, FOOT));
    return;
  }
  el.pos = h("div", { class: "t-body t-stack" });
  el.mkt = h("div", { class: "t-body t-stack" });
  el.rec = h("div", { class: "t-body t-stack t-gauge" });
  replace(app, h("div", { class: "t-frame" },
    el.top,
    h("section", { class: "t-panel t-pos" },
      h("div", { class: "t-title" }, "Position tracker", stakeForm()), el.pos),
    chartPanel(),
    h("section", { class: "t-panel t-mkt" },
      h("div", { class: "t-title" }, "Market dashboard"), el.mkt),
    h("section", { class: "t-panel t-rec" },
      h("div", { class: "t-title" }, "Recommendation"), el.rec),
    h("section", { class: "t-panel t-feed" },
      h("div", { class: "t-title" }, "Desk feed", h("span", { class: "dim" }, "what to do now")), el.feed)),
    h("p", { class: "t-foot" }, FOOT));
}

function paint(): void {
  if (!state.snap) return;
  paintBar();
  if (el.pos) paintPositions();
  if (el.mkt) paintMarkets();
  if (el.rec) paintRecommendation();
  paintFeed();
  paintChartHead();
  paintTicker();
  paintTape();
  draw();
  if (state.mode === "advanced") paintAdvanced(el, state, helpers);
  paintLive();
}

// Every figure that hangs off a live price, brought up to date in place. A node says
// what it shows: data-live (a price), data-change (a percentage from a reference price:
// yesterday's, or the one in data-from), data-fund and data-oi (funding and open
// interest from the stream). Only markets whose figures moved are touched.
function paintLive(): void {
  const dirty = state.dirty;
  const fresh = (node: HTMLElement, symbol: string) => dirty.has(symbol) || node.dataset.value === undefined;
  for (const node of app.querySelectorAll<HTMLElement>("[data-live], [data-change], [data-fund], [data-oi]")) {
    const data = node.dataset;
    if (data.live !== undefined) {
      const price = state.prices[data.live];
      if (price !== undefined && fresh(node, data.live)) cue(node, px(price), price);
      continue;
    }
    if (data.change !== undefined) paintChange(node);
    else if (data.fund !== undefined) paintFunding(node);
    else if (data.oi !== undefined) paintInterest(node);
  }
  paintHead();
  paintQuote();
  paintSparks();
  if (el.pos) paintPnl();
  dirty.clear();
  panelsSoon();
}

function isFresh(node: HTMLElement, symbol: string): boolean {
  return state.dirty.has(symbol) || node.dataset.value === undefined;
}

// A percentage from a reference price: yesterday's, or the one the node carries.
function paintChange(node: HTMLElement): void {
  const symbol = node.dataset.change as string;
  if (isFresh(node, symbol)) {
    const from = node.dataset.from ? Number(node.dataset.from) : state.prev[symbol];
    const change = changeFrom(state.prices[symbol], from);
    if (change === null) return;
    if (!cue(node, `${pct(change, Number(node.dataset.digits || 2))}${node.dataset.suffix || ""}`, change)) return;
    if (node.dataset.heat && node.parentElement) {
      node.parentElement.style.cssText = heat(change, Number(node.dataset.heat));
    } else {
      node.classList.toggle("up", change > 0);
      node.classList.toggle("down", change < 0);
    }
  }
}

// Funding, as a yearly rate, from the stream's hourly one.
function paintFunding(node: HTMLElement): void {
  const symbol = node.dataset.fund as string;
  const ctx = state.context[symbol];
  if (!ctx || !isFresh(node, symbol)) return;
  const apr = ctx.funding * 24 * 365 * 100;
  if (cue(node, pct(apr, 1), apr)) {
    node.classList.toggle("up", apr > 0);
    node.classList.toggle("down", apr < 0);
  }
}

// Open interest in dollars: the stream's count of units at the live price.
function paintInterest(node: HTMLElement): void {
  const symbol = node.dataset.oi as string;
  const ctx = state.context[symbol];
  const price = state.prices[symbol];
  if (ctx && price > 0 && isFresh(node, symbol)) cue(node, compact(ctx.openInterest * price), ctx.openInterest * price);
}

// The selected market's price in the chart header, with the decimals the exchange quotes.
function headPrice(): string | null {
  const price = state.prices[state.selected];
  if (price === undefined) return null;
  const places = state.places[state.selected];
  return places === undefined ? px(price) : exact(price, places);
}

function paintHead(): void {
  const text = el.last ? headPrice() : null;
  if (el.last && text && el.last.textContent !== text) el.last.textContent = text;
}

// The live quote under the chart header: best bid and ask with their sizes, the spread and
// the last trade, exactly as the exchange sends them. Without the stream there is no order
// book to quote from, so only the mid price is shown.
function paintQuote(): void {
  const line = el.quote;
  if (!line) return;
  const symbol = state.selected;
  const q = state.stream === "live" ? state.quotes[symbol] : null;
  const kind = q && q.bid && q.ask ? "book" : state.prices[symbol] !== undefined ? "mid" : "none";
  if (line.dataset.kind !== `${kind} ${symbol}`) {
    line.dataset.kind = `${kind} ${symbol}`;
    const figure = (key: string) => h("b", { "data-q": key });
    const size = (key: string) => h("span", { "data-q": key, "data-flash": "off" });
    replace(line, kind === "book" ? [
      h("span", null, "Bid ", figure("bid"), " × ", size("bidSize")),
      h("span", null, "Ask ", figure("ask"), " × ", size("askSize")),
      h("span", null, "Spread ", h("b", { "data-q": "spread", "data-flash": "off" })),
      h("span", null, "Last trade ", figure("trade"), " × ", size("tradeSize")),
    ] : kind === "mid" ? [
      h("span", null, "Mid ", figure("mid")),
      h("span", { class: "dim" }, "Bid, ask and trades need the live stream."),
    ] : h("span", null, "Waiting for a live quote…"));
  }
  const put = (key: string, text: string, value: number) => {
    const node = line.querySelector<HTMLElement>(`[data-q="${key}"]`);
    cue(node, text, value);
    return node;
  };
  if (kind === "mid") put("mid", headPrice() || "–", state.prices[symbol]);
  if (kind !== "book" || !q || !q.bid || !q.ask) return;
  const bid = Number(q.bid.px);
  const ask = Number(q.ask.px);
  const places = Math.max(decimalsOf(q.bid.px), decimalsOf(q.ask.px));
  put("bid", exact(bid, decimalsOf(q.bid.px)), bid);
  put("bidSize", q.bid.sz, Number(q.bid.sz));
  put("ask", exact(ask, decimalsOf(q.ask.px)), ask);
  put("askSize", q.ask.sz, Number(q.ask.sz));
  put("spread", `${exact(ask - bid, places)} (${((ask - bid) / bid * 10000).toFixed(1)} bps)`, ask - bid);
  const trade = state.tape[0];
  const node = put("trade", trade ? exact(trade.px, decimalsOf(trade.text)) : "–", trade ? trade.px : 0);
  if (node) {
    node.classList.toggle("up", Boolean(trade && trade.buy));
    node.classList.toggle("down", Boolean(trade && !trade.buy));
  }
  put("tradeSize", trade ? trade.size : "–", trade ? trade.sz : 0);
}

// The basic screen's trade tape under the chart: the last eight trades of the selected
// market. (The advanced screen has its own beside the order book.)
function paintTape(): void {
  if (!el.tape) return;
  el.tape.hidden = state.mode !== "basic";
  if (state.mode !== "basic") return;
  const rows = state.tape.slice(0, 8);
  replace(el.tape, rows.length
    ? rows.map((t) => h("span", { class: "t-trade" },
        h("span", { class: "dim" }, new Date(t.time).toISOString().slice(11, 19)),
        h("b", { class: t.buy ? "up" : "down" }, exact(t.px, decimalsOf(t.text))),
        h("span", null, `$${compact(t.sz * t.px)}`)))
    : h("span", { class: "dim" }, state.stream === "live" ? "Trades appear here as they happen."
                                                         : "The trade tape needs the live stream."));
}

// A strip of every watched market: label, live price, today's change. Built once per set
// of markets; the figures in it are kept up to date by paintLive.
function paintTicker(): void {
  if (!el.ticker) return;
  const symbols: string[] = [];
  if (state.snap) for (const m of state.snap.markets) symbols.push(m.symbol);
  if (state.mode === "advanced" && state.adv) {
    for (const m of state.adv.worksheet) if (!symbols.includes(m.symbol)) symbols.push(m.symbol);
  }
  const key = symbols.join(" ");
  if (el.ticker.dataset.key === key) return;
  el.ticker.dataset.key = key;
  replace(el.ticker, symbols.map((symbol) => {
    const m = marketInfo(symbol);
    return h("button", { class: "t-tick", type: "button", onclick: () => select(symbol) },
      h("b", null, m ? m.label : symbol.split(":").pop()),
      h("span", { "data-live": symbol }, px(live(symbol, m && m.price))),
      h("span", { class: tone(m && m.day_change_pct), "data-change": symbol, "data-flash": "off" },
        pct(m ? m.day_change_pct : null)));
  }));
}

// ---- top bar -----------------------------------------------------------------

function countdown(target: string): string {
  const ms = new Date(`${target}T00:00:00Z`).getTime() - Date.now();
  if (ms <= 0) return "00:00:00";
  const s = Math.floor(ms / 1000);
  const days = Math.floor(s / 86400);
  const clock = [Math.floor(s % 86400 / 3600), Math.floor(s % 3600 / 60), s % 60]
    .map((n) => String(n).padStart(2, "0")).join(":");
  return days ? `${days}d ${clock}` : clock;
}

function paintBar(): void {
  if (!el.bar) return;
  if (el.clk) paintSessions(el);
  const snap = state.snap;
  const now = new Date();
  const clocks = CLOCKS.map(([label, zone]) => h("div", { class: "t-clock" },
    h("b", null, now.toLocaleTimeString("en-GB", { timeZone: zone, hour12: false })),
    h("span", null, label)));
  // "Live" comes with a true measurement: stream messages received in the last second or so.
  const mode = feedState(state.stream, state.directAt, state.lastPoll, Date.now(), POLL_MS);
  const words = { live: `Live · ${state.rate} updates/s`, fast: "Every 1s", poll: "Every 5s", down: "No data" };
  const mood = snap && (snap.mood.today || snap.mood.week);
  replace(el.bar,
    h("span", { class: "t-brand" }, "Neko terminal"),
    h("span", { class: "t-tabs", role: "group", "aria-label": "Screen" },
      ([["basic", "Basic"], ["advanced", "Advanced"]] as const).map(([value, text]) => h("button", { type: "button",
        "aria-pressed": String(state.mode === value), onclick: () => setMode(value) }, text))),
    ...(state.mode === "advanced" ? [] : clocks),
    mood ? h("div", { class: "t-stat" },
      h("b", { class: mood.value >= 50 ? "up" : "down" }, `${mood.value} ${mood.label}`),
      h("span", null, "Market mood")) : null,
    snap ? h("div", { class: "t-stat" },
      h("b", { class: tone(weekPnl()), id: "week-pnl", "data-value": weekPnl() }, usd(weekPnl())),
      h("span", null, "This week, model")) : null,
    snap ? h("div", { class: "t-stat" },
      h("b", null, countdown(snap.next_call)), h("span", null, "To Monday's close")) : null,
    h("span", { class: "t-spacer" }),
    state.error ? h("span", { class: "down small" }, state.error) : null,
    h("span", { class: "t-live", "data-state": mode }, h("i"), words[mode]),
    h("nav", { class: "t-nav", "aria-label": "Pages" },
      h("a", { href: "/", "aria-current": "page" }, "Terminal"),
      h("a", { href: "/week" }, "This week"),
      h("a", { href: "/forex" }, "Forex"),
      h("a", { href: "/guide" }, "Guide")));
}

// ---- positions ---------------------------------------------------------------

// Re-price a model position from the live stream between polls.
function livePosition(p: Position): Position {
  return repriced(p, live(p.coin, p.last));
}

function weekPnl(): number {
  if (!state.snap) return 0;
  return state.snap.positions.reduce((sum, p) => sum + livePosition(p).pnl_usd, 0);
}

const CLOSED_WORDS: Record<string, string> = { stopped: "Stopped", liquidated: "Liquidated", target: "Profit taken" };

function paintPositions(): void {
  if (!state.snap || !el.pos) return;
  const snap = state.snap;
  const cards = snap.positions.map((raw) => {
    const p = livePosition(raw);
    const closed = p.status !== "open";
    const room = p.stop_room_left;
    return h("button", { class: "t-card", type: "button", "aria-pressed": String(state.selected === p.coin),
      onclick: () => select(p.coin) },
      h("div", { class: "t-card-head" },
        h("strong", null, `${p.coin}/USD`),
        h("span", { class: "t-badge", "data-side": closed ? p.status : p.side },
          closed ? CLOSED_WORDS[p.status] || p.status : `${p.side} ${p.leverage}x`)),
      h("div", { class: "t-kv" },
        h("span", null, "Size"), h("b", null, `$${p.position_usd.toFixed(2)}`),
        h("span", null, "Entry"), h("b", null, px(p.entry)),
        h("span", null, "Last"), h("b", { "data-live": p.coin }, px(p.last)),
        h("span", null, "Stop"), h("b", { class: "down" }, px(p.stop)),
        h("span", null, "Target"), p.target ? h("b", { class: "up" }, px(p.target)) : h("b", { class: "dim" }, "none"),
        h("span", null, "Liq."), h("b", null, px(p.liquidation))),
      h("div", { class: `t-pnl ${tone(p.pnl_usd)}`, "data-pnl": p.coin, "data-value": p.pnl_pct },
        `${usd(p.pnl_usd)} (${pct(p.pnl_pct, 2)})`),
      !closed && room !== undefined && room !== null ? h("div", null,
        h("div", { class: "t-room", "data-near": String(room <= 0.25), "data-room": p.coin },
          h("i", { style: `width:${Math.min(100, room / 1.5 * 100)}%` })),
        h("div", { class: "dim", style: "font-size:var(--t-micro);margin-top:2px", "data-stopdist": p.coin },
          `${(p.stop_distance_pct as number).toFixed(2)}% from the stop`)) : null);
  });
  const out = snap.stay_out.map((c) => h("button", { class: "t-card", type: "button",
    "aria-pressed": String(state.selected === c.coin), onclick: () => select(c.coin) },
    h("div", { class: "t-card-head" },
      h("strong", null, `${c.coin}/USD`), h("span", { class: "t-badge" }, "Stay out")),
    h("div", { class: "dim", style: "font-size:var(--t-small)" },
      c.skipped ? `Signals lean ${c.signal}; that side has not held up here.`
                : "Mood and the last four weeks disagree.")));
  const small = (snap.too_small || []).map((c) => h("button", { class: "t-card", type: "button",
    "aria-pressed": String(state.selected === c.coin), onclick: () => select(c.coin) },
    h("div", { class: "t-card-head" },
      h("strong", null, `${c.coin}/USD`), h("span", { class: "t-badge", "data-side": "small" }, "Too small")),
    h("div", { class: "dim", style: "font-size:var(--t-small)" },
      `Called ${c.side} at ${c.leverage}x, but that is a $${c.position_usd.toFixed(2)} position and the ` +
      `exchange's smallest order is $10. Needs a weekly amount of $${Math.ceil(c.min_weekly_amount)}.`)));
  replace(el.pos, ...cards, ...small, ...out,
    !cards.length && !small.length && !out.length ? h("p", { class: "dim" }, "No calls this week.") : null);
}

function paintPnl(): void {
  if (!state.snap || !el.pos) return;
  for (const raw of state.snap.positions) {
    const p = livePosition(raw);
    const node = el.pos.querySelector<HTMLElement>(`[data-pnl="${p.coin}"]`);
    if (node && cue(node, `${usd(p.pnl_usd)} (${pct(p.pnl_pct, 2)})`, p.pnl_pct)) {
      node.classList.toggle("up", p.pnl_usd > 0);
      node.classList.toggle("down", p.pnl_usd < 0);
    }
    const dist = el.pos.querySelector(`[data-stopdist="${p.coin}"]`);
    if (dist && p.stop_distance_pct !== undefined) {
      dist.textContent = `${p.stop_distance_pct.toFixed(2)}% from the stop`;
    }
    const room = el.pos.querySelector<HTMLElement>(`[data-room="${p.coin}"]`);
    if (room && p.stop_room_left !== undefined && p.stop_room_left !== null) {
      room.dataset.near = String(p.stop_room_left <= 0.25);
      (room.firstChild as HTMLElement).style.width = `${Math.min(100, p.stop_room_left / 1.5 * 100)}%`;
    }
  }
  const total = document.getElementById("week-pnl");
  if (total && cue(total, usd(weekPnl()), weekPnl())) {
    total.classList.toggle("up", weekPnl() > 0);
    total.classList.toggle("down", weekPnl() < 0);
  }
}

// ---- markets -----------------------------------------------------------------

function spark(values: number[], up: boolean): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 200 30");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  if (values.length > 1) {
    const min = Math.min(...values);
    const span = (Math.max(...values) - min) || 1;
    const line = document.createElementNS(ns, "polyline");
    line.setAttribute("points", values.map((v, i) =>
      `${(i / (values.length - 1) * 200).toFixed(1)},${(28 - (v - min) / span * 26).toFixed(1)}`).join(" "));
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", up ? COLORS.up : COLORS.down);
    line.setAttribute("stroke-width", "1.5");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(line);
  }
  return svg;
}

// A dashboard card's line: the closed four-hour bars, then the live price as its last point.
function liveSpark(m: TerminalMarket): SVGElement {
  const price = state.prices[m.symbol];
  const values = price > 0 && m.spark.length ? [...m.spark, price] : m.spark;
  const up = values.length > 1 ? values[values.length - 1] >= values[0] : (m.day_change_pct || 0) >= 0;
  const svg = spark(values, up);
  svg.dataset.spark = m.symbol;
  return svg;
}

function paintSparks(): void {
  if (!el.mkt || !state.snap) return;
  for (const m of state.snap.markets) {
    if (!state.dirty.has(m.symbol)) continue;
    for (const old of el.mkt.querySelectorAll<SVGElement>("svg[data-spark]")) {
      if (old.dataset.spark === m.symbol) old.replaceWith(liveSpark(m));
    }
  }
}

function paintMarkets(): void {
  if (!state.snap || !el.mkt) return;
  replace(el.mkt, ...state.snap.markets.map((m) => {
    return h("button", { class: "t-card t-market", type: "button",
      "aria-pressed": String(state.selected === m.symbol), onclick: () => select(m.symbol) },
      h("strong", null, m.label),
      h("span", { class: "t-price", "data-live": m.symbol }, px(live(m.symbol, m.price))),
      h("span", { class: "t-badge" }, m.recommendation.verdict),
      h("span", { class: `t-chg ${tone(m.day_change_pct)}`, "data-change": m.symbol, "data-suffix": " today",
                  "data-flash": "off" }, `${pct(m.day_change_pct)} today`),
      liveSpark(m),
      h("p", null, m.note));
  }));
}

// ---- recommendation ------------------------------------------------------------

function gaugeSvg(lean: number): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 240 136");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label",
    `Signals lean ${lean < -0.2 ? "bearish" : lean > 0.2 ? "bullish" : "neutral"}`);
  const arc = (from: number, to: number, color: string) => {
    const point = (deg: number) => {
      const rad = Math.PI * (1 - deg / 180);
      return `${(120 + 96 * Math.cos(rad)).toFixed(1)} ${(120 - 96 * Math.sin(rad)).toFixed(1)}`;
    };
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", `M ${point(from)} A 96 96 0 0 1 ${point(to)}`);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", "12");
    svg.append(path);
  };
  arc(2, 58, COLORS.down);
  arc(62, 118, COLORS.amber);
  arc(122, 178, COLORS.up);
  const angle = Math.PI * (1 - (90 + lean * 80) / 180);
  const needle = document.createElementNS(ns, "line");
  needle.setAttribute("x1", "120");
  needle.setAttribute("y1", "120");
  needle.setAttribute("x2", (120 + 78 * Math.cos(angle)).toFixed(1));
  needle.setAttribute("y2", (120 - 78 * Math.sin(angle)).toFixed(1));
  needle.setAttribute("stroke", COLORS.ink);
  needle.setAttribute("stroke-width", "3");
  needle.setAttribute("stroke-linecap", "round");
  const hub = document.createElementNS(ns, "circle");
  hub.setAttribute("cx", "120");
  hub.setAttribute("cy", "120");
  hub.setAttribute("r", "6");
  hub.setAttribute("fill", COLORS.ink);
  for (const [x, label] of [[24, "Bearish"], [120, "Neutral"], [216, "Bullish"]] as Array<[number, string]>) {
    const text = document.createElementNS(ns, "text");
    text.setAttribute("x", String(x));
    text.setAttribute("y", label === "Neutral" ? "12" : "134");
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("font-size", "10");
    text.setAttribute("fill", COLORS.text);
    text.textContent = label;
    svg.append(text);
  }
  svg.append(needle, hub);
  return svg;
}

function paintRecommendation(): void {
  if (!state.snap || !el.rec) return;
  const market = state.snap.markets.find((m) => m.symbol === state.selected) || state.snap.markets[0];
  if (!market) return;
  const rec = market.recommendation;
  const position = state.snap.positions.find((p) => p.coin === market.symbol);
  const color = rec.verdict === "LONG" ? "up" : rec.verdict === "SHORT" ? "down" : "dim";
  replace(el.rec,
    h("div", { class: "dim", style: "text-align:center;font-size:var(--t-small)" }, market.label),
    gaugeSvg(rec.lean),
    h("div", { class: `t-verdict ${color}` }, rec.verdict),
    h("div", { class: "t-strength", role: "img",
      "aria-label": `${rec.strength} of ${rec.of} signals agree` },
      Array.from({ length: rec.of }, (_, i) => h("i", { "data-on": String(i < rec.strength) }))),
    h("p", { class: "dim", style: "text-align:center;font-size:var(--t-small);max-width:none" }, rec.detail),
    h("div", { class: "t-votes" }, rec.votes.map((v) => h("div", null,
      h("span", { class: "dim" }, v.name),
      h("span", { class: v.direction === "up" ? "up" : "down" }, v.direction === "up" ? "Up" : "Down")))),
    position ? h("div", { class: "t-votes" },
      h("div", null, h("span", { class: "dim" }, "Entry"), h("span", { class: "num" }, px(position.entry))),
      h("div", null, h("span", { class: "dim" }, "Stop loss"), h("span", { class: "num down" }, px(position.stop))),
      h("div", null, h("span", { class: "dim" }, "Take profit"),
        position.target ? h("span", { class: "num up" }, px(position.target))
                        : h("span", { class: "dim" }, "None: hold to Monday")),
      h("div", null, h("span", { class: "dim" }, "Liquidation"), h("span", { class: "num" }, px(position.liquidation))))
      : null);
}

// ---- feed --------------------------------------------------------------------

function paintFeed(): void {
  if (!state.snap || !el.feed) return;
  const advice = state.snap.advice;
  replace(el.feed,
    h("ul", { class: "t-advice" }, advice.map((a) => h("li", { "data-level": a.level },
      h("strong", null, a.title), h("span", null, a.text)))),
    state.log.length ? h("ul", { class: "t-log", "aria-label": "Changes since you opened the terminal" },
      state.log.map((entry) => h("li", null, h("b", null, `${entry.time} UTC`), `  ${entry.title}`))) : null);
}

// ---- chart -------------------------------------------------------------------

function select(symbol: string): void {
  if (state.selected === symbol) return;
  state.selected = symbol;
  state.chart = null;
  state.hover = null;
  watchMarkets();
  paint();
  loadChart();
  watchBook();
}

// A market's label and day change, wherever it is listed.
function marketInfo(symbol: string): { label: string; price: number | null; day_change_pct: number | null } | null {
  const basic = state.snap && state.snap.markets.find((m) => m.symbol === symbol);
  if (basic) return basic;
  const row = state.adv && state.adv.worksheet.find((m) => m.symbol === symbol);
  return row || null;
}

function paintChartHead(): void {
  if (!el.chartHead || !state.snap) return;
  const market = marketInfo(state.selected);
  const label = market ? market.label : state.selected.split(":").pop();
  const last = live(state.selected, market && market.price);
  replace(el.chartHead,
    h("span", { class: "t-symbol" }, `${label}/USD`),
    el.last = h("span", { class: `t-last ${market ? tone(market.day_change_pct) : ""}` }, headPrice() || px(last)),
    market ? h("span", { class: `num ${tone(market.day_change_pct)}`, "data-change": state.selected,
                         "data-suffix": " today", "data-flash": "off" }, `${pct(market.day_change_pct)} today`) : null,
    h("span", { class: "t-spacer" }),
    h("span", { class: "t-tabs", role: "group", "aria-label": "Chart interval" },
      INTERVALS.map(([value, text]) => h("button", { type: "button",
        "aria-pressed": String(state.interval === value),
        onclick: () => {
          state.interval = value;
          state.chart = null;
          state.hover = null;
          paintChartHead();
          draw();
          loadChart();
        } },
        text))));
  paintReadout();
}

function paintReadout(): void {
  if (!el.readout) return;
  const seconds = state.interval === "1s";
  const chart = chartNow();
  const candles = chart ? chart.candles : [];
  const c = state.hover !== null ? candles[state.hover] : candles[candles.length - 1];
  if (seconds && state.hover === null && noLivePrices()) {
    replace(el.readout, h("span", { class: "down" },
      "No live prices are arriving. Check the status at the top right."));
    return;
  }
  if (!c) {
    replace(el.readout, h("span", null,
      seconds ? "Waiting for the first live price…" : state.chart === null ? "Loading chart…" : ""));
    return;
  }
  const when = new Date(c.t).toISOString().replace("T", " ").slice(0, seconds ? 19 : 16);
  replace(el.readout,
    h("span", null, `${when} UTC`), h("span", null, `O ${px(c.o)}`), h("span", null, `H ${px(c.h)}`),
    h("span", null, `L ${px(c.l)}`), h("span", { class: c.c >= c.o ? "up" : "down" }, `C ${px(c.c)}`),
    seconds ? h("span", { class: "amber" }, state.stream === "live"
      ? `Live: ${candles.length} of ${TICK_BARS} seconds, built from live prices and trades since you opened the terminal`
      : `Every 1s: ${candles.length} of ${TICK_BARS} seconds, built from prices once a second; volume needs the live stream`) : null);
}

function levels(): Array<{ name: string; value: number; color: string }> {
  const p = state.snap && state.snap.positions.find((x) => x.coin === state.selected);
  if (!p) return [];
  return [
    { name: "ENTRY", value: p.entry, color: COLORS.cyan },
    { name: "STOP", value: p.stop, color: COLORS.down },
    { name: "TARGET", value: p.target, color: COLORS.up },
  ].filter((l): l is { name: string; value: number; color: string } => Boolean(l.value));
}

let geometry: { step: number; count: number; plotW: number } | null = null;

function draw(): void {
  const canvas = el.canvas;
  if (!canvas || !el.canvasBox) return;
  const box = el.canvasBox.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const W = Math.max(200, box.width);
  const H = Math.max(160, box.height);
  if (canvas.width !== Math.round(W * ratio) || canvas.height !== Math.round(H * ratio)) {
    canvas.width = Math.round(W * ratio);
    canvas.height = Math.round(H * ratio);
  }
  const g = canvas.getContext("2d");
  if (!g) return;
  g.setTransform(ratio, 0, 0, ratio, 0, 0);
  g.clearRect(0, 0, W, H);
  g.font = "11px 'IBM Plex Mono', ui-monospace, monospace";
  geometry = null;
  const chart = chartNow();
  const seconds = state.interval === "1s";
  if (!chart || !chart.candles.length) {
    g.fillStyle = COLORS.text;
    g.fillText(seconds ? "Waiting for the first live price…"
      : state.chart === null ? "Loading…" : "No chart data.", 12, 24);
    return;
  }
  const candles = chart.candles.map((c) => ({ ...c }));
  const tip = candles[candles.length - 1];
  const now = live(state.selected, tip.c) as number;
  tip.c = now;                                          // the forming candle follows the live price
  tip.h = Math.max(tip.h, now);
  tip.l = Math.min(tip.l, now);

  const gutter = 68;
  const priceH = H * 0.76;
  const volTop = priceH + 8;
  const volH = H - volTop - 18;
  const plotW = W - gutter;
  const step = plotW / candles.length;
  let lo = Math.min(...candles.map((c) => c.l));
  let hi = Math.max(...candles.map((c) => c.h));
  const pad = (hi - lo) * 0.06 || hi * 0.01;
  lo -= pad;
  hi += pad;
  const y = (v: number) => 6 + (1 - (v - lo) / (hi - lo)) * (priceH - 12);
  const x = (i: number) => i * step + step / 2;
  geometry = { step, count: candles.length, plotW };

  g.strokeStyle = COLORS.grid;
  g.fillStyle = COLORS.text;
  g.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const value = lo + (hi - lo) * i / 4;
    g.beginPath();
    g.moveTo(0, Math.round(y(value)) + 0.5);
    g.lineTo(plotW, Math.round(y(value)) + 0.5);
    g.stroke();
    g.fillText(px(value), plotW + 6, y(value) + 4);
  }
  const dates = Math.max(2, Math.min(5, Math.floor(plotW / 120)));   // fewer dates on a narrow chart
  for (let i = 0; i < dates; i += 1) {
    const index = Math.floor(candles.length * (i + 0.5) / dates);
    const date = new Date(candles[index].t);
    const text = seconds ? date.toISOString().slice(11, 19)
      : state.interval === "1d"
      ? date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "UTC" })
      : `${date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "UTC" })} ${String(date.getUTCHours()).padStart(2, "0")}:00`;
    g.fillText(text, Math.max(0, x(index) - 30), H - 4);
  }

  const maxVol = Math.max(...candles.map((c) => c.v)) || 1;
  const body = Math.max(1, step * 0.68);
  candles.forEach((c, i) => {
    const color = c.c >= c.o ? COLORS.up : COLORS.down;
    g.strokeStyle = color;
    g.fillStyle = color;
    g.beginPath();
    g.moveTo(Math.round(x(i)) + 0.5, y(c.h));
    g.lineTo(Math.round(x(i)) + 0.5, y(c.l));
    g.stroke();
    const top = y(Math.max(c.o, c.c));
    g.fillRect(x(i) - body / 2, top, body, Math.max(1, y(Math.min(c.o, c.c)) - top));
    g.globalAlpha = 0.45;
    const vh = c.v / maxVol * volH;
    g.fillRect(x(i) - body / 2, volTop + volH - vh, body, vh);
    g.globalAlpha = 1;
  });

  const line = (series: Array<number | null> | undefined, color: string) => {
    if (!series) return;
    g.strokeStyle = color;
    g.lineWidth = 1.5;
    g.beginPath();
    let started = false;
    series.forEach((v, i) => {
      if (v === null || v === undefined) return;
      const py = Math.max(6, Math.min(priceH - 6, y(v)));
      if (started) g.lineTo(x(i), py);
      else g.moveTo(x(i), py);
      started = true;
    });
    g.stroke();
    g.lineWidth = 1;
  };
  line(chart.emas["10d"], COLORS.amber);
  line(chart.emas["40d"], COLORS.violet);

  const tag = (text: string, color: string, at: number) => {
    g.fillStyle = color;
    g.fillRect(plotW + 1, at - 8, gutter - 2, 16);
    g.fillStyle = "#05080c";
    g.fillText(text, plotW + 5, at + 4);
  };
  let above = 0;
  let below = 0;
  for (const level of levels()) {
    const inside = level.value >= lo && level.value <= hi;
    // several levels off the same edge are listed one under the other
    const at = inside ? y(level.value)
      : level.value > hi ? 14 + 16 * above++ : priceH - 10 - 16 * below++;
    if (inside) {                                   // a level off the chart gets its label only
      g.strokeStyle = level.color;
      g.setLineDash([6, 4]);
      g.beginPath();
      g.moveTo(0, Math.round(at) + 0.5);
      g.lineTo(plotW, Math.round(at) + 0.5);
      g.stroke();
      g.setLineDash([]);
    }
    g.fillStyle = level.color;
    g.fillText(`${level.name} ${px(level.value)}${inside ? "" : level.value > hi ? "  (above)" : "  (below)"}`,
      8, at - 4);
    if (inside) tag(px(level.value), level.color, at);
  }
  tag(px(now), tip.c >= tip.o ? COLORS.up : COLORS.down, y(now));

  if (state.hover !== null && candles[state.hover]) {
    g.strokeStyle = COLORS.text;
    g.setLineDash([2, 3]);
    g.beginPath();
    g.moveTo(Math.round(x(state.hover)) + 0.5, 0);
    g.lineTo(Math.round(x(state.hover)) + 0.5, H - 18);
    g.stroke();
    g.setLineDash([]);
  }
}

function onHover(event: PointerEvent): void {
  if (!geometry || !el.canvas) return;
  const rect = el.canvas.getBoundingClientRect();
  const index = Math.floor((event.clientX - rect.left) / geometry.step);
  const next = index >= 0 && index < geometry.count ? index : null;
  if (next === state.hover) return;
  state.hover = next;
  paintReadout();
  draw();
}

// ---- start -------------------------------------------------------------------

type Timer = ReturnType<typeof setInterval> | null;
const timers: { poll: Timer; chart: Timer; clock: Timer; adv: Timer; book: Timer; advSoon: Timer } =
  { poll: null, chart: null, clock: null, adv: null, book: null, advSoon: null };
const helpers: Helpers = { select, live };
let paused = false;                   // true while the tab is hidden

function restartTimers(): void {
  for (const key of ["adv", "book"] as const) {
    if (timers[key]) clearInterval(timers[key]);
    timers[key] = null;
  }
  if (timers.advSoon) clearTimeout(timers.advSoon);
  timers.advSoon = null;
  if (state.mode === "advanced" && !paused) {
    timers.adv = setInterval(pollAdvanced, ADV_MS);
    timers.book = setInterval(pollBook, BOOK_MS);
  }
}

function startTimers(): void {
  timers.poll = setInterval(poll, POLL_MS);
  timers.chart = setInterval(loadChart, CHART_MS);
  timers.clock = setInterval(everySecond, 1000);
  restartTimers();
}

// A tab nobody is looking at asks for nothing: every refresh timer stops.
function pause(): void {
  paused = true;
  for (const key of ["poll", "chart", "clock", "adv", "book"] as const) {
    if (timers[key]) clearInterval(timers[key]);
    timers[key] = null;
  }
  if (timers.advSoon) clearTimeout(timers.advSoon);
  timers.advSoon = null;
  if (reconnect) clearTimeout(reconnect);
  reconnect = null;
}

// Back in view: refresh everything at once, then carry on as before.
function resume(): void {
  if (!paused) return;
  paused = false;
  if (!state.snap) {                  // the first load never finished: start over
    start();
    return;
  }
  startTimers();
  poll();
  loadChart();
  pollAdvanced();
  pollBook();
  everySecond();
  if (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING) connect();
}

async function start(): Promise<void> {
  frame();
  paintBar();
  await poll();
  if (!state.snap) {
    if (el.feed) replace(el.feed, h("p", { class: "down" }, state.error || "The terminal could not load."));
    return;
  }
  const called = state.snap.positions[0];
  if (called) state.selected = called.coin;
  watchMarkets();
  paint();
  loadChart();
  if (!paused) startTimers();
  if (!socket) connect();
  pollAdvanced();
}

whileVisible(pause, resume);
if (document.hidden) paused = true;
start();
