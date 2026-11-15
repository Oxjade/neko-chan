import {
  api, h, replace, fmt, tone, gaugeFromPerformance, table, figures, directionWord,
  setupNotice, signInForm, positionColumns, tradeColumns, marketLabel, tradingViewChart,
} from "/web/common.js";

const app = document.getElementById("app");
const signout = document.getElementById("signout");

const state = { data: null, markets: [], selected: null, flash: null, timer: null,
                sizing: loadSizing() };

const CLASS_WORDS = { crypto: "Crypto", stocks: "Stocks", indices: "Indices and funds",
                      forex: "Forex", commodities: "Commodities" };
const TRUST_WORDS = { trusted: "trusted", unproven: "unproven", muted: "muted" };

// The account size and risk are a per-browser convenience; nothing depends on them.
function loadSizing() {
  try {
    const saved = JSON.parse(localStorage.getItem("neko-desk-sizing") || "null");
    if (saved && saved.account > 0 && saved.risk > 0) return saved;
  } catch { /* storage unavailable */ }
  return { account: 100, risk: 1 };
}

function saveSizing() {
  try {
    localStorage.setItem("neko-desk-sizing", JSON.stringify(state.sizing));
  } catch { /* storage unavailable */ }
}

function day(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "–"
    : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function pct(v, digits = 1) {
  return v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

// TradingView names for the markets whose names differ; the chart lets the
// viewer change the symbol if a guess is wrong.
const CHART_NAMES = { SP500: "SPX", XYZ100: "NDX", JP225: "NI225", GOLD: "XAUUSD", SILVER: "XAGUSD",
                      CL: "USOIL", BRENTOIL: "UKOIL", EUR: "EURUSD", GBP: "GBPUSD", JPY: "USDJPY" };
const chart = { symbol: null, node: null };

function chartFor(row) {
  if (chart.symbol !== row.symbol || !chart.node) {
    const name = row.class === "crypto" ? `${row.label}USD` : (CHART_NAMES[row.label] || row.label);
    chart.symbol = row.symbol;
    chart.node = tradingViewChart(name, "240");
  }
  return chart.node;
}

// ---- data ------------------------------------------------------------------

async function refresh() {
  try {
    state.data = await api("/api/web/desk");
    const symbols = state.data.watchlist.map((r) => r.symbol);
    if (!symbols.includes(state.selected)) state.selected = symbols[0] || null;
    render();
  } catch (err) {
    if (err.status === 401) {
      stopPolling();
      renderSignIn([]);
      return;
    }
    state.flash = { bad: true, text: err.message };
    render();
  }
}

function stopPolling() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}

async function act(path, body, done) {
  state.flash = null;
  try {
    await api(path, body);
    if (done) state.flash = { bad: false, text: done };
  } catch (err) {
    state.flash = { bad: true, text: err.message };
  }
  await refresh();
}

// ---- wording ---------------------------------------------------------------

function signalWords(row) {
  const s = row.signal;
  if (s.state === "waiting") return "Waiting for a cross";
  if (s.state === "stopped") return `Stopped out ${day(s.since)}`;
  const side = directionWord(s.direction);
  if (s.muted) return `${side} (muted)`;
  return s.state === "new" ? `New ${side.toLowerCase()}` : `${side} since ${day(s.since)}`;
}

function recordWords(record) {
  if (!record.trades) return "No past trades";
  return `${record.wins} of ${record.trades} won`;
}

function standingWords(row) {
  const { long, short } = row.trust;
  if (long.state === "muted" && short.state === "muted") return "Muted";
  if (long.state === "muted") return "Longs muted";
  if (short.state === "muted") return "Shorts muted";
  return long.state === "trusted" && short.state === "trusted" ? "Trusted" : "Unproven";
}

// A replay's break-even is set by a few large winners, so describe what
// happened instead of claiming the win rate "beats" it.
function recordVerdict(record) {
  if (!record.trades) return null;
  const won = record.net > 0;
  return `${record.wins} of ${record.trades} past signals won, and the ` +
    `${won ? "wins outweighed the losses" : "losses outweighed the wins"}: ` +
    `${pct(record.net)} added up.`;
}

function verdict(row) {
  const s = row.signal;
  const name = row.label;
  if (s.state === "waiting") {
    return `No signal on ${name} yet. One appears when its 10-day and 40-day averages cross.`;
  }
  if (s.state === "stopped") {
    return `The last ${name} signal hit its stop on ${day(s.since)}. Waiting for the next cross.`;
  }
  const side = s.direction;
  const trust = row.trust[side];
  if (s.muted) {
    return `The rule says ${side} ${name}, but ${side}s have lost money here ` +
      `(${trust.wins} of ${trust.trades} won), so this signal is muted.`;
  }
  if (s.state === "new") {
    return trust.state === "trusted"
      ? `New ${side} signal on ${name}. ${directionWord(side)}s have made money on this market so far.`
      : `New ${side} signal on ${name}, but there are too few past trades here to judge it.`;
  }
  return `${directionWord(side)} ${name} since ${day(s.since)}, ${pct(s.change_pct)} so far. ` +
    `Joining late was not tested; the next entry is the next cross.`;
}

// ---- left column -----------------------------------------------------------

function addForm() {
  const watching = new Set(state.data.watchlist.map((r) => r.symbol));
  const error = h("p", { class: "loss small", role: "alert" });
  const classes = Object.keys(CLASS_WORDS).filter((c) => state.markets.some((m) => m.class === c));
  const kind = h("select", { name: "class" }, classes.map((c) =>
    h("option", { value: c }, CLASS_WORDS[c])));
  const market = h("select", { name: "market" });
  const fill = () => {
    const options = state.markets.filter((m) => m.class === kind.value && !watching.has(m.symbol));
    market.replaceChildren(...options.map((m) =>
      h("option", { value: m.symbol }, `${m.label}  ${fmt.price(m.price)}`)));
  };
  kind.addEventListener("change", fill);
  fill();
  const button = h("button", { class: "button", type: "submit" }, "Add to watchlist");
  return h("form", { class: "panel stack", novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      if (!market.value) {
        error.textContent = "Every market of this type is already on the watchlist.";
        return;
      }
      button.disabled = true;
      state.selected = market.value;
      await act("/api/web/desk/watchlist", { symbol: market.value },
        `${marketLabel(market.value)} added. Its signals are now paper-traded.`);
    } },
    h("h2", { style: "font-size:var(--t-h3)" }, "Add a market"),
    h("label", { class: "field" }, h("span", null, "Type"), kind),
    h("label", { class: "field" }, h("span", null, "Market"), market),
    error, button);
}

function sizingForm() {
  const account = h("input", { type: "text", name: "account", inputmode: "decimal",
                               value: String(state.sizing.account) });
  const risk = h("input", { type: "text", name: "risk", inputmode: "decimal",
                            value: String(state.sizing.risk) });
  const error = h("p", { class: "loss small", role: "alert" });
  return h("form", { class: "panel stack", novalidate: true,
    onsubmit: (event) => {
      event.preventDefault();
      error.textContent = "";
      const a = Number(account.value);
      const r = Number(risk.value);
      if (!(a > 0)) {
        error.textContent = "Enter your account size in USD.";
        account.focus();
        return;
      }
      if (!(r > 0 && r <= 100)) {
        error.textContent = "Enter the risk per trade as a percentage between 0 and 100.";
        risk.focus();
        return;
      }
      state.sizing = { account: a, risk: r };
      saveSizing();
      document.activeElement.blur();
      render();
    } },
    h("h2", { style: "font-size:var(--t-h3)" }, "Your trade size"),
    h("p", { class: "soft small" }, "Used to work out the position size for each signal."),
    h("label", { class: "field" }, h("span", null, "Account size (USD)"), account),
    h("label", { class: "field" }, h("span", null, "Risk per trade (%)"), risk),
    error, h("button", { class: "button quiet", type: "submit" }, "Save trade size"));
}

function lessonsPanel() {
  const lessons = state.data.lessons;
  return h("section", { class: "panel stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "What the record has taught"),
    lessons.length
      ? h("ul", { class: "gate" }, lessons.map((l) =>
          h("li", { "data-passed": "false" },
            h("span", { class: "tick", "aria-hidden": "true" }, ""),
            h("div", { class: "small" }, l.text))))
      : h("p", { class: "soft small" },
          "Nothing is muted. A side is muted on a market once it has five or more past trades there and they lost money overall."),
    h("p", { class: "soft small" },
      "The score is recounted as new prices arrive, so a muted side returns if it starts working."));
}

// ---- right column ----------------------------------------------------------

function newSignals() {
  const fresh = state.data.watchlist.filter((r) => r.signal && r.signal.state === "new");
  const live = fresh.filter((r) => !r.signal.muted);
  const muted = fresh.filter((r) => r.signal.muted);
  return h("section", { class: "panel stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "New signals"),
    live.length
      ? live.map((r) => h("div", { class: "stack" },
          h("p", { class: "verdict" },
            `${directionWord(r.signal.direction)} ${r.label} near ${fmt.price(r.signal.entry)}, ` +
            `stop ${fmt.price(r.signal.stop)}`),
          h("p", { class: "soft small" },
            `Crossed ${fmt.when(r.signal.since)}. On this market: ${recordWords(r.record)}, ` +
            `${pct(r.record.expectancy)} average; ` +
            `${TRUST_WORDS[r.trust[r.signal.direction].state]}.`)))
      : h("p", { class: "soft" },
          state.data.watchlist.length
            ? "No new signals in the last 24 hours. Each market signals about once every three to four weeks."
            : "Add markets to the watchlist to get signals."),
    muted.length
      ? h("p", { class: "soft small" },
          `Muted: ${muted.map((r) => `${r.signal.direction} ${r.label}`).join(", ")}.`)
      : null);
}

function watchTable() {
  const rows = state.data.watchlist;
  const columns = [
    { label: "Market", cell: (r) => h("button", {
        type: "button", class: "button quiet", "aria-pressed": String(r.symbol === state.selected),
        onclick: () => { state.selected = r.symbol; render(); } }, r.label) },
    { label: "Price", num: true, cell: (r) => fmt.price(r.price) },
    { label: "Today", num: true, cell: (r) => pct(r.day_change_pct), cls: (r) => tone(r.day_change_pct) },
    { label: "Trend", cell: (r) => (r.error ? "–" : `${r.trend === "up" ? "Up" : "Down"} ${r.trend_days.toFixed(0)}d`) },
    { label: "Signal", cell: (r) => (r.error ? r.error : signalWords(r)) },
    { label: "Since signal", num: true,
      cell: (r) => (r.error || !r.signal.direction ? "–" : pct(r.signal.change_pct)),
      cls: (r) => (r.error || !r.signal.direction ? "" : tone(r.signal.change_pct)) },
    { label: "Record here", cell: (r) => (r.error ? "–" : recordWords(r.record)) },
    { label: "Average trade", num: true,
      cell: (r) => (r.error || !r.record.trades ? "–" : pct(r.record.expectancy)),
      cls: (r) => (r.error || !r.record.trades ? "" : tone(r.record.expectancy)) },
    { label: "Standing", cell: (r) => (r.error ? "–" : standingWords(r)) },
    { label: "", cell: (r) => h("button", { type: "button", class: "button quiet",
        "aria-label": `Remove ${r.label} from the watchlist`,
        onclick: () => act("/api/web/desk/watchlist/remove", { symbol: r.symbol },
          `${r.label} removed.`) }, "Remove") },
  ];
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "Watchlist"),
    table(columns, rows, "The watchlist is empty. Add a market on the left."));
}

function sizeLine(row) {
  const s = row.signal;
  if (!s.direction || !s.stop_pct) return null;
  const { account, risk } = state.sizing;
  const cost = state.data.rule.cost_pct;
  const riskUsd = account * risk / 100;
  let position = riskUsd / ((s.stop_pct + cost) / 100);
  const cap = account * row.max_leverage;
  const capped = position > cap;
  if (capped) position = cap;
  const leverage = position / account;
  const loss = position * (s.stop_pct + cost) / 100;
  return h("p", { class: "notice" },
    `With ${fmt.usd(account)} and ${risk}% risk: position ${fmt.usd(position)} ` +
    `(${leverage.toFixed(1)}x), losing ${fmt.usd(loss)} if the stop is hit.` +
    (capped ? ` That is the most ${row.max_leverage}x leverage allows on this market.` : ""));
}

const pastColumns = [
  { label: "Opened", cell: (t) => day(t.opened_at) },
  { label: "Closed", cell: (t) => day(t.closed_at) },
  { label: "Side", cell: (t) => directionWord(t.direction) },
  { label: "Entry", num: true, cell: (t) => fmt.price(t.entry) },
  { label: "Exit", num: true, cell: (t) => fmt.price(t.exit) },
  { label: "Closed by", cell: (t) => (t.reason === "stop" ? "Stop" : "Opposite cross") },
  { label: "Result", num: true, cell: (t) => pct(t.gross_pct), cls: (t) => tone(t.gross_pct) },
];

function detail() {
  const row = state.data.watchlist.find((r) => r.symbol === state.selected);
  if (!row) return null;
  if (row.error) {
    return h("section", { class: "panel stack" },
      h("h2", { style: "font-size:var(--t-h3)" }, row.label),
      h("p", { class: "notice bad" }, row.error));
  }
  const s = row.signal;
  const record = row.record;
  const side = (d) => {
    const t = row.trust[d];
    return t.trades ? `${t.wins} of ${t.trades}, ${pct(t.total_net_pct)} total` : "No trades";
  };
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" },
      `${row.label}, ${(CLASS_WORDS[row.class] || row.class).toLowerCase()}`),
    h("div", { class: "panel stack" },
      h("p", { class: "verdict" }, verdict(row)),
      s.direction ? figures([
        [s.state === "new" ? "Entry near" : "Entered at", fmt.price(s.entry)],
        ["Stop", fmt.price(s.stop)],
        ["Stop distance", `${s.stop_pct.toFixed(1)}%`],
        ["Typical 4-hour move", row.atr_pct === null ? "–" : `${row.atr_pct.toFixed(2)}%`],
        ["Funding per day", pct(row.funding_daily_pct * (s.direction === "long" ? -1 : 1), 3)],
      ]) : null,
      s.muted ? null : sizeLine(row)),
    h("div", { class: "panel" }, gaugeFromPerformance(record, recordVerdict(record))),
    figures([
      ["Past trades here", String(record.trades)],
      ["Average trade", pct(record.expectancy), tone(record.expectancy)],
      ["All trades added up", pct(record.net), tone(record.net)],
      ["Longs", side("long")],
      ["Shorts", side("short")],
      ["History used", `${row.history_days.toFixed(0)} days`],
    ]),
    h("p", { class: "soft small" },
      `Results are a replay of the rule on this market's past prices, after ${state.data.rule.cost_pct}% ` +
      `costs per round trip. A replay is not a promise.`),
    chartFor(row),
    h("h3", null, "Past signals on this market"),
    table(pastColumns, row.recent_trades, "The rule has not closed a trade on this market yet."));
}

function paperSection() {
  const paper = state.data.paper;
  if (!paper) return null;
  const acct = paper.account;
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "Paper account"),
    h("p", { class: "soft small" },
      "Every unmuted signal on the watchlist is traded here with simulated money, split equally across the markets, no leverage. It opens only on a fresh cross, so it fills in slowly."),
    h("div", { class: "panel" }, gaugeFromPerformance(paper.performance)),
    figures([
      ["Equity", fmt.usd(acct.equity)],
      ["Cash", fmt.usd(acct.cash)],
      ["Unrealised", fmt.signedUsd(acct.unrealized), tone(acct.unrealized)],
      ["Closed trades", String(paper.performance.trades)],
      ["Result after fees", fmt.signedUsd(paper.performance.net), tone(paper.performance.net)],
    ]),
    h("h3", null, "Open paper positions"),
    table(positionColumns, paper.positions, "No open paper positions."),
    h("h3", null, "Closed paper trades"),
    table(tradeColumns, paper.trades, "No paper trades have closed yet."));
}

function render() {
  signout.hidden = false;
  const active = document.activeElement;
  if (active && app.contains(active) && ["INPUT", "SELECT"].includes(active.tagName)) {
    return; // do not rebuild the page under someone typing
  }
  if (!state.data) {
    replace(app, state.flash ? h("p", { class: "notice bad" }, state.flash.text) : null);
    return;
  }
  replace(app,
    setupNotice(state.data.setup_missing),
    state.flash
      ? h("p", { class: state.flash.bad ? "notice bad" : "notice", role: "status" }, state.flash.text)
      : null,
    h("div", { class: "console", style: "margin-top:var(--s5)" },
      h("aside", null, addForm(), sizingForm(), lessonsPanel()),
      h("main", null, h("div", { class: "stack" },
        newSignals(), watchTable(), detail(), paperSection()))));
}

// ---- start -----------------------------------------------------------------

function renderSignIn(missing) {
  signout.hidden = true;
  replace(app, signInForm(missing, start));
}

signout.addEventListener("click", async () => {
  stopPolling();
  try {
    await api("/api/web/logout", {});
  } finally {
    renderSignIn([]);
  }
});

async function start() {
  let session;
  try {
    session = await api("/api/web/session");
  } catch (err) {
    replace(app, h("p", { class: "notice bad" }, err.message));
    return;
  }
  if (!session.signed_in) {
    renderSignIn(session.setup_missing);
    return;
  }
  try {
    state.markets = (await api("/api/web/desk/markets")).markets;
  } catch (err) {
    state.flash = { bad: true, text: err.message };
  }
  await refresh();
  stopPolling();
  state.timer = setInterval(refresh, 60000);
}

start();
