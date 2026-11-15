// Neko terminal: a live working screen for this week's calls.
// The server works out positions, advice and recommendations every few seconds
// (so the rules live in one tested place); a direct stream from the exchange
// (prices, best bid and offer, trades, order book) keeps the numbers and the
// chart moving in between.

import { api, h, replace, signInForm } from "/web/common.js";
import { COLORS, px, pct, usd, tone } from "/web/terminal_util.js";
import { buildAdvanced, paintAdvanced, paintSessions, paintBook } from "/web/terminal_adv.js";

const app = document.getElementById("app");
const POLL_MS = 5000;
const CHART_MS = 60000;
const STREAM_URL = "wss://api.hyperliquid.xyz/ws";
const INTERVALS = [["1s", "1S"], ["1h", "1H"], ["4h", "4H"], ["1d", "1D"]];
const TICK_BARS = 300;                // the one-second chart keeps the last five minutes
const CLOCKS = [["UTC", "UTC"], ["New York", "America/New_York"], ["London", "Europe/London"],
                ["Tokyo", "Asia/Tokyo"]];
const ADV_MS = 20000;
const BOOK_MS = 3000;

const state = {
  snap: null, chart: null, selected: "ETH", interval: "4h", stake: loadStake(),
  prices: {}, stream: "connecting", lastPoll: 0, error: null,
  log: [], seen: new Set(), hover: null, frame: null,
  mode: loadMode(), adv: null, book: null, tape: [], bookSymbol: null, tradeSymbol: null,
  tradeBacklog: false,
  watch: new Set(["ETH"]),
};

// Basic or advanced screen: a per-browser preference.
function loadMode() {
  try {
    return localStorage.getItem("neko-terminal-mode") === "advanced" ? "advanced" : "basic";
  } catch {
    return "basic";
  }
}

function setMode(mode) {
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
function loadStake() {
  try {
    const saved = JSON.parse(localStorage.getItem("neko-week-plan") || "null");
    if (saved && saved.stake > 0) return saved.stake;
  } catch { /* storage unavailable */ }
  return 18;
}

function saveStake() {
  try {
    const saved = JSON.parse(localStorage.getItem("neko-week-plan") || "null") || { leverage: 0 };
    localStorage.setItem("neko-week-plan", JSON.stringify({ ...saved, stake: state.stake }));
  } catch { /* storage unavailable */ }
}

// ---- helpers -----------------------------------------------------------------

function live(symbol, fallback) {
  const v = state.prices[symbol];
  return v === undefined ? fallback : v;
}

// ---- one-second candles ------------------------------------------------------

// The exchange has no one-second history, so these are built here from the live
// stream: every price update and every trade of a watched market lands in the
// candle of the second it arrived in. They start when the page opens.
const ticks = new Map();              // symbol -> candles, oldest first

function tickPrice(symbol, price, size = 0) {
  let candles = ticks.get(symbol);
  if (!candles) ticks.set(symbol, candles = []);
  const t = Math.floor(Date.now() / 1000) * 1000;
  let last = candles[candles.length - 1];
  if (!last || t > last.t) {
    const open = last ? last.c : price;
    // quiet seconds get flat candles, so the time axis stays even
    for (let s = last ? Math.max(last.t + 1000, t - (TICK_BARS - 1) * 1000) : t; s < t; s += 1000) {
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
function watchMarkets() {
  const watch = new Set([state.selected]);
  if (state.snap) for (const m of state.snap.markets) watch.add(m.symbol);
  if (state.adv) for (const m of state.adv.worksheet) watch.add(m.symbol);
  state.watch = watch;
  watchQuotes();
}

// Best bid and offer for every watched market. These arrive several times a
// second, where the all-markets price feed only ticks every few seconds, so
// they are what keeps prices, profit and the one-second chart moving.
const quoted = new Set();

function watchQuotes() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  for (const symbol of [...quoted]) {
    if (state.watch.has(symbol)) continue;
    send({ method: "unsubscribe", subscription: { type: "bbo", coin: symbol } });
    quoted.delete(symbol);
  }
  for (const symbol of state.watch) {
    if (quoted.has(symbol)) continue;
    send({ method: "subscribe", subscription: { type: "bbo", coin: symbol } });
    quoted.add(symbol);
  }
}

// What the chart draws: server candles, or the one-second candles built here.
function chartNow() {
  if (state.interval !== "1s") return state.chart;
  return { candles: ticks.get(state.selected) || [], emas: {} };
}

// Once a second: start the next candle even if the market is quiet, and redraw.
function everySecond() {
  paintBar();
  if (state.interval !== "1s") return;
  const candles = ticks.get(state.selected);
  const price = candles && candles.length ? candles[candles.length - 1].c : state.prices[state.selected];
  if (price === undefined) return;
  tickPrice(state.selected, price);
  if (state.hover === null) paintReadout();
  draw();
}

// ---- data --------------------------------------------------------------------

async function poll() {
  try {
    state.snap = await api(`/api/web/terminal?stake=${encodeURIComponent(state.stake)}`);
    state.lastPoll = Date.now();
    state.error = null;
    for (const m of state.snap.markets) {
      if (state.prices[m.symbol] === undefined && m.price) state.prices[m.symbol] = m.price;
    }
    if (state.stream !== "live") {
      for (const m of state.snap.markets) if (m.price) state.prices[m.symbol] = m.price;
    }
    noteAdvice();
    watchMarkets();
    paint();
  } catch (err) {
    if (err.status === 401) {
      stop();
      renderSignIn([]);
      return;
    }
    state.error = err.message;
    paintBar();
  }
}

async function pollAdvanced() {
  if (state.mode !== "advanced") return;
  try {
    state.adv = await api(`/api/web/terminal/advanced?stake=${encodeURIComponent(state.stake)}`);
    watchMarkets();
    paintAdvanced(el, state, helpers);
  } catch (err) {
    if (err.status === 401) return;
    state.error = err.message;
  }
}

// Order book without the stream: ask the server every few seconds.
async function pollBook() {
  if (state.mode !== "advanced" || state.stream === "live") return;
  try {
    const book = await api(`/api/web/terminal/book?symbol=${encodeURIComponent(state.selected)}`);
    if (book.symbol === state.selected) {
      state.book = book;
      paintBook(el, state, helpers);
    }
  } catch { /* the next poll will try again */ }
}

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

// Follow the selected market on the stream: its trades always (they feed the
// one-second chart and the tape), its order book only on the advanced screen.
function watchBook() {
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
    paintBook(el, state, helpers);
    pollBook();
  }
}

async function loadChart() {
  if (state.interval === "1s") {                  // built in the browser; nothing to fetch
    paintChartHead();
    draw();
    return;
  }
  try {
    state.chart = await api(
      `/api/web/terminal/chart?symbol=${encodeURIComponent(state.selected)}&interval=${state.interval}`);
  } catch (err) {
    state.chart = null;
    state.error = err.message;
  }
  paintChartHead();
  draw();
}

function noteAdvice() {
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
let socket = null;
let retry = 1000;

function connect() {
  try {
    socket = new WebSocket(STREAM_URL);
  } catch {
    state.stream = "poll";
    return;
  }
  socket.addEventListener("open", () => {
    retry = 1000;
    socket.send(JSON.stringify({ method: "subscribe", subscription: { type: "allMids" } }));
    socket.send(JSON.stringify({ method: "subscribe", subscription: { type: "allMids", dex: "xyz" } }));
    state.bookSymbol = null;                      // subscriptions do not survive a reconnect
    state.tradeSymbol = null;
    quoted.clear();
    watchBook();
    watchQuotes();
  });
  socket.addEventListener("message", (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (msg.channel === "l2Book" && msg.data && msg.data.coin === state.bookSymbol) {
      const side = (rows) => rows.slice(0, 12).map((r) => ({ px: Number(r.px), sz: Number(r.sz) }));
      state.book = { symbol: msg.data.coin, bids: side(msg.data.levels[0]), asks: side(msg.data.levels[1]) };
      scheduleBook();
      return;
    }
    if (msg.channel === "trades" && Array.isArray(msg.data)) {
      // The first batch after subscribing is a backlog of earlier trades. It
      // belongs on the tape, but not in the candle of the current second.
      const backlog = state.tradeBacklog && msg.data.some((t) => t.coin === state.tradeSymbol);
      for (const t of msg.data) {
        if (t.coin !== state.tradeSymbol) continue;
        state.tape.unshift({ time: t.time, px: Number(t.px), sz: Number(t.sz), buy: t.side === "B" });
        if (!backlog) tickPrice(t.coin, Number(t.px), Number(t.sz));
      }
      if (backlog) state.tradeBacklog = false;
      state.tape = state.tape.slice(0, 16);
      scheduleBook();
      if (state.interval === "1s") schedule();
      return;
    }
    if (msg.channel === "bbo" && msg.data && Array.isArray(msg.data.bbo)) {
      const [bid, ask] = msg.data.bbo;
      const price = bid && ask ? (Number(bid.px) + Number(ask.px)) / 2 : Number((bid || ask || {}).px);
      if (!(price > 0)) return;
      state.stream = "live";
      state.prices[msg.data.coin] = price;
      if (state.watch.has(msg.data.coin)) tickPrice(msg.data.coin, price);
      schedule();
      return;
    }
    if (msg.channel !== "allMids" || !msg.data || !msg.data.mids) return;
    state.stream = "live";
    for (const [symbol, value] of Object.entries(msg.data.mids)) {
      const price = Number(value);
      if (!(price > 0)) continue;
      state.prices[symbol] = price;
      if (state.watch.has(symbol)) tickPrice(symbol, price);
    }
    schedule();
  });
  socket.addEventListener("close", () => {
    state.stream = "poll";
    paintBar();
    if (timers.poll) setTimeout(connect, retry = Math.min(retry * 2, 30000));
  });
  socket.addEventListener("error", () => socket.close());
}

let bookFrame = null;

function scheduleBook() {
  if (bookFrame) return;
  bookFrame = requestAnimationFrame(() => {
    bookFrame = null;
    paintBook(el, state, helpers);
  });
}

// Price ticks arrive many times a second; repaint the live numbers at most once per frame.
function schedule() {
  if (state.frame) return;
  state.frame = requestAnimationFrame(() => {
    state.frame = null;
    paintLive();
    draw();
  });
}

// ---- frame -------------------------------------------------------------------

const el = {};

function stakeForm() {
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

function chartPanel() {
  el.chartHead = h("div", { class: "t-chart-head" });
  el.readout = h("div", { class: "t-readout" });
  el.canvas = h("canvas", { role: "img", "aria-label": "Price chart" });
  el.canvasBox = h("div", { class: "t-canvas" }, el.canvas);
  el.canvas.addEventListener("pointermove", onHover);
  el.canvas.addEventListener("pointerleave", () => { state.hover = null; paintReadout(); draw(); });
  new ResizeObserver(draw).observe(el.canvasBox);
  return h("section", { class: "t-panel t-chart" },
    el.chartHead, el.readout, el.canvasBox,
    h("div", { class: "t-legend" },
      h("span", null, h("i", { style: `background:${COLORS.amber}` }), "10-day average"),
      h("span", null, h("i", { style: `background:${COLORS.violet}` }), "40-day average"),
      h("span", null, h("i", { style: `background:${COLORS.cyan}` }), "Entry"),
      h("span", null, h("i", { style: `background:${COLORS.down}` }), "Stop loss"),
      h("span", null, h("i", { style: `background:${COLORS.up}` }), "Optional far target")));
}

const FOOT = "Positions are model positions: what the weekly amount would be doing had this week's calls been followed from Monday's open. " +
  "Calls come from six years of past prices and have been right a little over half the time. Prices: Hyperliquid.";

function frame() {
  for (const key of Object.keys(el)) delete el[key];
  el.bar = h("header", { class: "t-panel t-bar" });
  el.feed = h("div", { class: "t-body" });
  if (state.mode === "advanced") {
    replace(app, buildAdvanced(el, { chartPanel, stakeForm }), h("p", { class: "t-foot" }, FOOT));
    return;
  }
  el.pos = h("div", { class: "t-body t-stack" });
  el.mkt = h("div", { class: "t-body t-stack" });
  el.rec = h("div", { class: "t-body t-stack t-gauge" });
  replace(app, h("div", { class: "t-frame" },
    el.bar,
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

function paint() {
  if (!state.snap) return;
  paintBar();
  if (el.pos) paintPositions();
  if (el.mkt) paintMarkets();
  if (el.rec) paintRecommendation();
  paintFeed();
  paintChartHead();
  draw();
  if (state.mode === "advanced") paintAdvanced(el, state, helpers);
}

function paintLive() {
  for (const node of app.querySelectorAll("[data-live]")) {
    const price = state.prices[node.dataset.live];
    if (price === undefined) continue;
    const text = px(price);
    if (node.textContent === text) continue;
    const was = Number(node.dataset.value || price);
    node.textContent = text;
    node.dataset.value = String(price);
    if (node.dataset.flash !== "off" && price !== was) {
      node.classList.remove("t-flash-up", "t-flash-down");
      void node.offsetWidth;
      node.classList.add(price > was ? "t-flash-up" : "t-flash-down");
    }
  }
  if (el.pos) paintPnl();
}

// ---- top bar -----------------------------------------------------------------

function countdown(target) {
  const ms = new Date(`${target}T00:00:00Z`) - Date.now();
  if (ms <= 0) return "00:00:00";
  const s = Math.floor(ms / 1000);
  const days = Math.floor(s / 86400);
  const clock = [Math.floor(s % 86400 / 3600), Math.floor(s % 3600 / 60), s % 60]
    .map((n) => String(n).padStart(2, "0")).join(":");
  return days ? `${days}d ${clock}` : clock;
}

function paintBar() {
  if (!el.bar) return;
  if (el.clk) paintSessions(el);
  const snap = state.snap;
  const now = new Date();
  const clocks = CLOCKS.map(([label, zone]) => h("div", { class: "t-clock" },
    h("b", null, now.toLocaleTimeString("en-GB", { timeZone: zone, hour12: false })),
    h("span", null, label)));
  const fresh = Date.now() - state.lastPoll < POLL_MS * 3;
  const mode = state.stream === "live" ? "live" : fresh ? "poll" : "down";
  const words = { live: "Live", poll: "Every 5s", down: "No data" };
  const mood = snap && (snap.mood.today || snap.mood.week);
  replace(el.bar,
    h("span", { class: "t-brand" }, "Neko terminal"),
    h("span", { class: "t-tabs", role: "group", "aria-label": "Screen" },
      [["basic", "Basic"], ["advanced", "Advanced"]].map(([mode, text]) => h("button", { type: "button",
        "aria-pressed": String(state.mode === mode), onclick: () => setMode(mode) }, text))),
    ...(state.mode === "advanced" ? [] : clocks),
    mood ? h("div", { class: "t-stat" },
      h("b", { class: mood.value >= 50 ? "up" : "down" }, `${mood.value} ${mood.label}`),
      h("span", null, "Market mood")) : null,
    snap ? h("div", { class: "t-stat" },
      h("b", { class: tone(weekPnl()), id: "week-pnl" }, usd(weekPnl())),
      h("span", null, "This week, model")) : null,
    snap ? h("div", { class: "t-stat" },
      h("b", null, countdown(snap.next_call)), h("span", null, "To Monday's close")) : null,
    h("span", { class: "t-spacer" }),
    state.error ? h("span", { class: "down small" }, state.error) : null,
    h("span", { class: "t-live", "data-state": mode }, h("i"), words[mode]),
    h("nav", { class: "t-nav", "aria-label": "Pages" },
      h("a", { href: "/week" }, "This week"), h("a", { href: "/desk" }, "Desk"),
      h("a", { href: "/weekly" }, "Weekly plan"), h("a", { href: "/console" }, "Console"),
      h("button", { class: "button quiet", type: "button", style: "min-height:28px;padding:2px 8px",
        onclick: signOut }, "Sign out")));
}

// ---- positions ---------------------------------------------------------------

// Re-price a model position from the live stream between server polls.
function livePosition(p) {
  if (p.status !== "open") return p;
  const last = live(p.coin, p.last);
  const sign = p.side === "long" ? 1 : -1;
  const move = (last / p.entry - 1) * sign;
  const pnl = Math.max(p.share * p.leverage * (move - 0.0009), -p.share);
  const out = { ...p, last, move_pct: move * 100, pnl_usd: pnl, pnl_pct: pnl / p.share * 100 };
  if (p.stop) {
    const left = (p.stop - last) * -sign;
    out.stop_distance_pct = left / last * 100;
    out.stop_room_left = Math.max(0, Math.min(1.5, left / Math.abs(p.stop - p.entry)));
  }
  return out;
}

function weekPnl() {
  if (!state.snap) return 0;
  return state.snap.positions.reduce((sum, p) => sum + livePosition(p).pnl_usd, 0);
}

function paintPositions() {
  const snap = state.snap;
  const cards = snap.positions.map((raw) => {
    const p = livePosition(raw);
    const closed = p.status !== "open";
    return h("button", { class: "t-card", type: "button", "aria-pressed": String(state.selected === p.coin),
      onclick: () => select(p.coin) },
      h("div", { class: "t-card-head" },
        h("strong", null, `${p.coin}/USD`),
        h("span", { class: "t-badge", "data-side": closed ? p.status : p.side },
          closed ? p.status : `${p.side} ${p.leverage}x`)),
      h("div", { class: "t-kv" },
        h("span", null, "Size"), h("b", null, `$${p.position_usd.toFixed(2)}`),
        h("span", null, "Entry"), h("b", null, px(p.entry)),
        h("span", null, "Last"), h("b", { "data-live": p.coin }, px(p.last)),
        h("span", null, "Stop"), h("b", { class: "down" }, px(p.stop))),
      h("div", { class: `t-pnl ${tone(p.pnl_usd)}`, "data-pnl": p.coin },
        `${usd(p.pnl_usd)} (${pct(p.pnl_pct, 1)})`),
      !closed && p.stop_room_left !== undefined ? h("div", null,
        h("div", { class: "t-room", "data-near": String(p.stop_room_left <= 0.25), "data-room": p.coin },
          h("i", { style: `width:${Math.min(100, p.stop_room_left / 1.5 * 100)}%` })),
        h("div", { class: "dim", style: "font-size:var(--t-micro);margin-top:2px", "data-stopdist": p.coin },
          `${p.stop_distance_pct.toFixed(1)}% from the stop`)) : null);
  });
  const out = snap.stay_out.map((c) => h("button", { class: "t-card", type: "button",
    "aria-pressed": String(state.selected === c.coin), onclick: () => select(c.coin) },
    h("div", { class: "t-card-head" },
      h("strong", null, `${c.coin}/USD`), h("span", { class: "t-badge" }, "Stay out")),
    h("div", { class: "dim", style: "font-size:var(--t-small)" },
      c.skipped ? `Signals lean ${c.signal}; that side has not held up here.`
                : "Mood and the last four weeks disagree.")));
  replace(el.pos, ...cards, ...out,
    !cards.length && !out.length ? h("p", { class: "dim" }, "No calls this week.") : null);
}

function paintPnl() {
  if (!state.snap) return;
  for (const raw of state.snap.positions) {
    const p = livePosition(raw);
    const node = el.pos.querySelector(`[data-pnl="${p.coin}"]`);
    if (node) {
      node.textContent = `${usd(p.pnl_usd)} (${pct(p.pnl_pct, 1)})`;
      node.className = `t-pnl ${tone(p.pnl_usd)}`;
    }
    const dist = el.pos.querySelector(`[data-stopdist="${p.coin}"]`);
    if (dist && p.stop_distance_pct !== undefined) {
      dist.textContent = `${p.stop_distance_pct.toFixed(1)}% from the stop`;
    }
    const room = el.pos.querySelector(`[data-room="${p.coin}"]`);
    if (room && p.stop_room_left !== undefined) {
      room.dataset.near = String(p.stop_room_left <= 0.25);
      room.firstChild.style.width = `${Math.min(100, p.stop_room_left / 1.5 * 100)}%`;
    }
  }
  const total = document.getElementById("week-pnl");
  if (total) {
    total.textContent = usd(weekPnl());
    total.className = tone(weekPnl());
  }
}

// ---- markets -----------------------------------------------------------------

function spark(values, up) {
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

function paintMarkets() {
  replace(el.mkt, ...state.snap.markets.map((m) => {
    const week = m.spark.length > 1 ? m.spark[m.spark.length - 1] >= m.spark[0] : (m.day_change_pct || 0) >= 0;
    return h("button", { class: "t-card t-market", type: "button",
      "aria-pressed": String(state.selected === m.symbol), onclick: () => select(m.symbol) },
      h("strong", null, m.label),
      h("span", { class: "t-price", "data-live": m.symbol }, px(live(m.symbol, m.price))),
      h("span", { class: "t-badge" }, m.recommendation.verdict),
      h("span", { class: `t-chg ${tone(m.day_change_pct)}` }, `${pct(m.day_change_pct)} today`),
      spark(m.spark, week),
      h("p", null, m.note));
  }));
}

// ---- recommendation ------------------------------------------------------------

function gaugeSvg(lean) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 240 136");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label",
    `Signals lean ${lean < -0.2 ? "bearish" : lean > 0.2 ? "bullish" : "neutral"}`);
  const arc = (from, to, color) => {
    const point = (deg) => {
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
  for (const [x, label] of [[24, "Bearish"], [120, "Neutral"], [216, "Bullish"]]) {
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

function paintRecommendation() {
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
      h("div", null, h("span", { class: "dim" }, "Optional far target"),
        h("span", { class: "num up" }, px(position.target))),
      h("div", null, h("span", { class: "dim" }, "Liquidation"), h("span", { class: "num" }, px(position.liquidation))))
      : null);
}

// ---- feed --------------------------------------------------------------------

function paintFeed() {
  const advice = state.snap.advice;
  replace(el.feed,
    h("ul", { class: "t-advice" }, advice.map((a) => h("li", { "data-level": a.level },
      h("strong", null, a.title), h("span", null, a.text)))),
    state.log.length ? h("ul", { class: "t-log", "aria-label": "Changes since you opened the terminal" },
      state.log.map((entry) => h("li", null, h("b", null, `${entry.time} UTC`), `  ${entry.title}`))) : null);
}

// ---- chart -------------------------------------------------------------------

function select(symbol) {
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
function marketInfo(symbol) {
  const basic = state.snap && state.snap.markets.find((m) => m.symbol === symbol);
  if (basic) return basic;
  const row = state.adv && state.adv.worksheet.find((m) => m.symbol === symbol);
  return row || null;
}

function paintChartHead() {
  if (!el.chartHead || !state.snap) return;
  const market = marketInfo(state.selected);
  const label = market ? market.label : state.selected.split(":").pop();
  const last = live(state.selected, market && market.price);
  replace(el.chartHead,
    h("span", { class: "t-symbol" }, `${label}/USD`),
    h("span", { class: `t-last ${market ? tone(market.day_change_pct) : ""}`,
      "data-live": state.selected, "data-flash": "off" }, px(last)),
    market ? h("span", { class: `num ${tone(market.day_change_pct)}` }, `${pct(market.day_change_pct)} today`) : null,
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

function paintReadout() {
  if (!el.readout) return;
  const seconds = state.interval === "1s";
  const chart = chartNow();
  const candles = chart ? chart.candles : [];
  const c = state.hover !== null ? candles[state.hover] : candles[candles.length - 1];
  if (!c) {
    replace(el.readout, h("span", null,
      seconds ? "Waiting for the first live price…" : state.chart === null ? "Loading chart…" : ""));
    return;
  }
  const when = new Date(c.t).toISOString().replace("T", " ").slice(0, seconds ? 19 : 16);
  replace(el.readout,
    h("span", null, `${when} UTC`), h("span", null, `O ${px(c.o)}`), h("span", null, `H ${px(c.h)}`),
    h("span", null, `L ${px(c.l)}`), h("span", { class: c.c >= c.o ? "up" : "down" }, `C ${px(c.c)}`),
    seconds ? h("span", { class: "amber" },
      `Live: ${candles.length} of ${TICK_BARS} seconds, built from live prices and trades since you opened the terminal`) : null);
}

function levels() {
  const p = state.snap && state.snap.positions.find((x) => x.coin === state.selected);
  if (!p) return [];
  return [
    { name: "ENTRY", value: p.entry, color: COLORS.cyan },
    { name: "STOP", value: p.stop, color: COLORS.down },
    { name: "TARGET", value: p.target, color: COLORS.up },
  ].filter((l) => l.value);
}

let geometry = null;

function draw() {
  const canvas = el.canvas;
  if (!canvas) return;
  const box = el.canvasBox.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const W = Math.max(200, box.width);
  const H = Math.max(160, box.height);
  if (canvas.width !== Math.round(W * ratio) || canvas.height !== Math.round(H * ratio)) {
    canvas.width = Math.round(W * ratio);
    canvas.height = Math.round(H * ratio);
  }
  const g = canvas.getContext("2d");
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
  const now = live(state.selected, tip.c);
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
  const y = (v) => 6 + (1 - (v - lo) / (hi - lo)) * (priceH - 12);
  const x = (i) => i * step + step / 2;
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

  const line = (series, color) => {
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

  const tag = (value, text, color, at) => {
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
    if (inside) tag(level.value, px(level.value), level.color, at);
  }
  tag(now, px(now), tip.c >= tip.o ? COLORS.up : COLORS.down, y(now));

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

function onHover(event) {
  if (!geometry) return;
  const rect = el.canvas.getBoundingClientRect();
  const index = Math.floor((event.clientX - rect.left) / geometry.step);
  const next = index >= 0 && index < geometry.count ? index : null;
  if (next === state.hover) return;
  state.hover = next;
  paintReadout();
  draw();
}

// ---- start -------------------------------------------------------------------

const timers = { poll: null, chart: null, clock: null, adv: null, book: null };
const helpers = { select, live };

function restartTimers() {
  for (const key of ["adv", "book"]) {
    if (timers[key]) clearInterval(timers[key]);
    timers[key] = null;
  }
  if (state.mode === "advanced") {
    timers.adv = setInterval(pollAdvanced, ADV_MS);
    timers.book = setInterval(pollBook, BOOK_MS);
  }
}

function stop() {
  for (const key of Object.keys(timers)) {
    if (timers[key]) clearInterval(timers[key]);
    timers[key] = null;
  }
  if (socket) socket.close();
}

function renderSignIn(missing) {
  for (const key of Object.keys(el)) delete el[key];
  replace(app, signInForm(missing, start));
  app.firstElementChild.style.padding = "0 16px";
}

async function signOut() {
  stop();
  try {
    await api("/api/web/logout", {});
  } finally {
    renderSignIn([]);
  }
}

async function start() {
  let session;
  try {
    session = await api("/api/web/session");
  } catch (err) {
    replace(app, h("p", { class: "notice bad", style: "margin:24px" }, err.message));
    return;
  }
  if (!session.signed_in) {
    renderSignIn(session.setup_missing);
    return;
  }
  frame();
  paintBar();
  await poll();
  if (!state.snap) {
    replace(el.feed, h("p", { class: "down" }, state.error || "The terminal could not load."));
    return;
  }
  const called = state.snap.positions[0];
  if (called) state.selected = called.coin;
  paint();
  loadChart();
  timers.poll = setInterval(poll, POLL_MS);
  timers.chart = setInterval(loadChart, CHART_MS);
  timers.clock = setInterval(everySecond, 1000);
  restartTimers();
  connect();
  pollAdvanced();
}

start();
