// Advanced screen of the Neko terminal: many dense panels, all from live or tested data.

import { h, replace } from "/web/common.js";
import { COLORS, SERIES, px, pct, usd, compact, tone, heat } from "/web/terminal_util.js";

const CLASS_WORDS = { crypto: "Crypto", stocks: "Stocks", indices: "Indices", forex: "Forex",
                      commodities: "Commodities" };
const SESSIONS = [
  { city: "Crypto", zone: "UTC", always: true },
  { city: "New York", zone: "America/New_York", open: 9.5, close: 16 },
  { city: "London", zone: "Europe/London", open: 8, close: 16.5 },
  { city: "Tokyo", zone: "Asia/Tokyo", open: 9, close: 15 },
];
const NS = "http://www.w3.org/2000/svg";

function panel(area, title, body, extra) {
  return h("section", { class: `t-panel a-${area}` },
    h("div", { class: "t-title" }, title, extra || null), body);
}

// A dense table. columns: { label, cell(row), num, heat(row) -> css, cls(row), amber }
function dense(columns, rows, options = {}) {
  if (!rows.length) return h("p", { class: "dim t-pad" }, options.empty || "No data.");
  const body = [];
  let group = null;
  for (const row of rows) {
    if (options.group) {
      const name = options.group(row);
      if (name !== group) {
        group = name;
        body.push(h("tr", { class: "t-group" }, h("td", { colspan: String(columns.length) }, name)));
      }
    }
    body.push(h("tr", {
      "aria-current": options.selected && options.selected(row) ? "true" : null,
      class: options.onRow ? "t-click" : "",
      onclick: options.onRow ? () => options.onRow(row) : null,
    }, columns.map((c) => h("td", {
      class: [c.num ? "n" : "", c.amber ? "amber" : "", c.cls ? c.cls(row) : ""].join(" ").trim(),
      style: c.heat ? c.heat(row) : null,
    }, c.cell(row)))));
  }
  return h("div", { class: "t-scroll" }, h("table", { class: "t-dense" },
    h("thead", null, h("tr", null, columns.map((c) =>
      h("th", { scope: "col", class: c.num ? "n" : "" }, c.label)))),
    h("tbody", null, body)));
}

export function buildAdvanced(el, parts) {
  for (const key of ["clk", "mov", "brd", "mood", "wks", "book", "sig", "risk", "perf", "corr", "lev",
                     "score", "fund", "fx"]) {
    el[key] = h("div", { class: "t-fill" });
  }
  const chart = parts.chartPanel();
  chart.classList.add("a-cht");
  return h("div", { class: "t-frame", "data-mode": "advanced" },
    el.bar,
    panel("clk", "Sessions", el.clk),
    panel("mov", "Movers today, by asset class", el.mov),
    panel("brd", "Breadth", el.brd, h("span", { class: "dim" }, "up / down today")),
    panel("mood", "Market mood", el.mood),
    panel("wks", "Market worksheet", el.wks, h("span", { class: "dim" }, "select a row")),
    chart,
    panel("bok", "Order book and trades", el.book),
    h("section", { class: "t-panel a-feed" },
      h("div", { class: "t-title" }, "Desk feed", h("span", { class: "dim" }, "what to do now")), el.feed),
    panel("sig", "Signal matrix", el.sig, h("span", { class: "dim" }, "weeks right, each half of history")),
    panel("risk", "Positions and risk", el.risk, parts.stakeForm()),
    panel("perf", "Thirty days, rebased to 100", el.perf),
    panel("corr", "Correlation, 30 days", el.corr),
    panel("lev", "Liquidation rate by leverage", el.lev, h("span", { class: "dim" }, "share of traded weeks")),
    panel("score", "Week by week", el.score),
    panel("fund", "Funding and open interest", el.fund),
    panel("fx", "Forex board", el.fx, h("span", { class: "dim" }, "central-bank rates")));
}

// ---- sessions ----------------------------------------------------------------

function localParts(zone, now) {
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone: zone, weekday: "short", hour: "2-digit",
    minute: "2-digit", second: "2-digit", hour12: false }).formatToParts(now);
  const get = (type) => parts.find((p) => p.type === type).value;
  return { day: get("weekday"), hour: Number(get("hour")) + Number(get("minute")) / 60,
           text: `${get("hour")}:${get("minute")}:${get("second")}` };
}

export function paintSessions(el) {
  if (!el.clk) return;
  const now = new Date();
  const cells = SESSIONS.map((s) => {
    const t = localParts(s.zone, now);
    const weekday = !["Sat", "Sun"].includes(t.day);
    const open = s.always || (weekday && t.hour >= s.open && t.hour < s.close);
    return h("div", { class: "t-clockcell" },
      h("b", { class: "num" }, t.text), h("span", { class: "amber" }, s.always ? "UTC · Crypto" : s.city),
      h("em", { class: open ? "up" : "dim" }, s.always ? "24/7" : open ? "Open" : "Closed"));
  });
  const ny = localParts("America/New_York", now);
  const fxOpen = !(ny.day === "Sat" || (ny.day === "Fri" && ny.hour >= 17) || (ny.day === "Sun" && ny.hour < 17));
  replace(el.clk, h("div", { class: "t-clocks" }, cells),
    h("p", { class: "t-pad t-micro" }, h("span", { class: "amber" }, "Forex "),
      h("span", { class: fxOpen ? "up" : "dim" }, fxOpen ? "open" : "closed for the weekend"),
      h("span", { class: "dim" }, " · regular hours, holidays not counted")));
}

// ---- panels ------------------------------------------------------------------

function paintMovers(el, adv) {
  const classes = Object.keys(CLASS_WORDS).filter((c) => adv.movers[c]);
  const tile = (m) => h("div", { class: "t-tile", style: heat(m.day_change_pct, 6) },
    h("b", null, m.label), h("span", { class: "num" }, pct(m.day_change_pct)));
  replace(el.mov, h("div", { class: "t-movers" }, classes.map((c) => {
    const group = adv.movers[c];
    return h("div", null,
      h("div", { class: "t-colhead" }, CLASS_WORDS[c]),
      h("div", { class: "t-sub up" }, "Most up"),
      group.up.length ? group.up.map(tile) : h("div", { class: "dim t-micro" }, "None up"),
      h("div", { class: "t-sub down" }, "Most down"),
      group.down.length ? group.down.map(tile) : h("div", { class: "dim t-micro" }, "None down"));
  })));
}

function donut(up, down, flat) {
  const total = up + down + flat || 1;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 42 42");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${up} up, ${down} down, ${flat} flat`);
  let offset = 25;                                   // start at twelve o'clock
  for (const [count, color] of [[up, COLORS.up], [down, COLORS.down], [flat, COLORS.grid]]) {
    const share = count / total * 100;
    const ring = document.createElementNS(NS, "circle");
    ring.setAttribute("cx", "21");
    ring.setAttribute("cy", "21");
    ring.setAttribute("r", "15.915");
    ring.setAttribute("fill", "none");
    ring.setAttribute("stroke", color);
    ring.setAttribute("stroke-width", "6");
    ring.setAttribute("stroke-dasharray", `${share} ${100 - share}`);
    ring.setAttribute("stroke-dashoffset", String(offset));
    svg.append(ring);
    offset -= share;
  }
  return svg;
}

function paintBreadth(el, adv) {
  replace(el.brd, h("div", { class: "t-donuts" }, Object.keys(CLASS_WORDS).filter((c) => adv.breadth[c])
    .map((c) => {
      const b = adv.breadth[c];
      return h("div", null, donut(b.up, b.down, b.flat),
        h("div", { class: "t-micro amber" }, CLASS_WORDS[c]),
        h("div", { class: "num t-micro" }, h("span", { class: "up" }, String(b.up)), " / ",
          h("span", { class: "down" }, String(b.down))));
    })));
}

function line(values, color, width, height, lo, hi) {
  const path = document.createElementNS(NS, "polyline");
  const span = (hi - lo) || 1;
  path.setAttribute("points", values.map((v, i) =>
    `${(i / (values.length - 1) * width).toFixed(1)},${(height - 2 - (v - lo) / span * (height - 4)).toFixed(1)}`).join(" "));
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", color);
  path.setAttribute("stroke-width", "1.5");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  return path;
}

function rule(y, width, color) {
  const node = document.createElementNS(NS, "line");
  node.setAttribute("x1", "0");
  node.setAttribute("x2", String(width));
  node.setAttribute("y1", String(y));
  node.setAttribute("y2", String(y));
  node.setAttribute("stroke", color);
  node.setAttribute("stroke-dasharray", "3 3");
  node.setAttribute("vector-effect", "non-scaling-stroke");
  return node;
}

function paintMood(el, state) {
  const history = state.adv.mood_history;
  const mood = state.snap ? state.snap.mood : null;
  const today = mood && (mood.today || mood.week);
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 200 54");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Fear and Greed index, last 120 days");
  svg.append(rule(27, 200, COLORS.text));
  if (history.length > 1) svg.append(line(history.map((d) => d.value), COLORS.amber, 200, 54, 0, 100));
  replace(el.mood, h("div", { class: "t-pad" },
    today ? h("div", { class: `t-big ${today.value >= 50 ? "up" : "down"}` }, `${today.value}`) : null,
    today ? h("div", { class: "t-micro" }, today.label) : null,
    svg,
    h("div", { class: "dim t-micro" },
      mood && mood.week ? `Week's reading ${mood.week.value}. Dashed line is 50: above is greedy, below is fearful.`
                        : "Dashed line is 50.")));
}

function paintWorksheet(el, state, helpers) {
  const rows = [...state.adv.worksheet].sort((a, b) =>
    Object.keys(CLASS_WORDS).indexOf(a.class) - Object.keys(CLASS_WORDS).indexOf(b.class));
  replace(el.wks, dense([
    { label: "Market", amber: true, cell: (r) => h("button", { type: "button", class: "t-rowbtn" }, r.label) },
    { label: "Last", num: true,
      cell: (r) => h("span", { "data-live": r.symbol, "data-flash": "off" }, px(helpers.live(r.symbol, r.price))) },
    { label: "Day", num: true, cell: (r) => pct(r.day_change_pct), heat: (r) => heat(r.day_change_pct, 6) },
    { label: "7d", num: true, cell: (r) => pct(r.week_pct, 1), heat: (r) => heat(r.week_pct, 15) },
    { label: "30d", num: true, cell: (r) => pct(r.month_pct, 1), heat: (r) => heat(r.month_pct, 30) },
    { label: "Trend", cell: (r) => `${r.trend === "up" ? "▲" : "▼"} ${r.trend_days.toFixed(0)}d`,
      cls: (r) => (r.trend === "up" ? "up" : "down") },
    { label: "Vol 30d", num: true, cell: (r) => (r.vol_30d === null ? "–" : `${r.vol_30d.toFixed(0)}%`) },
    { label: "Fund/yr", num: true, cell: (r) => pct(r.funding_apr, 1), cls: (r) => tone(r.funding_apr) },
    { label: "Open int.", num: true, cell: (r) => compact(r.open_interest_usd) },
    { label: "Trend rule", cell: (r) => (r.signal ? r.signal.toUpperCase() : "—"),
      cls: (r) => (r.signal === "long" ? "up" : r.signal === "short" ? "down" : "dim") },
    { label: "Won", num: true, cell: (r) => `${r.record_wins}/${r.record_trades}` },
  ], rows, {
    group: (r) => CLASS_WORDS[r.class] || r.class,
    onRow: (r) => helpers.select(r.symbol),
    selected: (r) => r.symbol === state.selected,
  }));
}

export function paintBook(el, state) {
  if (!el.book) return;
  const book = state.book;
  const label = state.selected.split(":").pop();
  if (!book || !book.bids.length || !book.asks.length) {
    replace(el.book, h("p", { class: "dim t-pad" }, `Waiting for the ${label} order book…`));
    return;
  }
  const withTotals = (rows) => {
    let sum = 0;
    return rows.map((r) => ({ ...r, total: (sum += r.sz) }));
  };
  const asks = withTotals(book.asks.slice(0, 9));
  const bids = withTotals(book.bids.slice(0, 9));
  const deepest = Math.max(asks[asks.length - 1].total, bids[bids.length - 1].total) || 1;
  const row = (r, side) => h("tr", {
    style: `background:linear-gradient(to left, ${side === "ask" ? "rgba(255,77,94,.18)" : "rgba(34,224,122,.18)"} ` +
           `${(r.total / deepest * 100).toFixed(1)}%, transparent 0)` },
    h("td", { class: `n ${side === "ask" ? "down" : "up"}` }, px(r.px)),
    h("td", { class: "n" }, compact(r.sz * r.px)), h("td", { class: "n dim" }, compact(r.total * r.px)));
  const spread = asks[0].px - bids[0].px;
  const table = h("table", { class: "t-dense" },
    h("thead", null, h("tr", null, h("th", { class: "n" }, "Price"), h("th", { class: "n" }, "Size $"),
      h("th", { class: "n" }, "Total $"))),
    h("tbody", null, [...asks].reverse().map((r) => row(r, "ask")),
      h("tr", { class: "t-group" }, h("td", { colspan: "3" },
        `${label} spread ${px(spread)} (${(spread / bids[0].px * 10000).toFixed(1)} bps)`)),
      bids.map((r) => row(r, "bid"))));
  const tape = state.tape.length ? h("table", { class: "t-dense" },
    h("thead", null, h("tr", null, h("th", null, "Time"), h("th", { class: "n" }, "Price"),
      h("th", { class: "n" }, "Size $"))),
    h("tbody", null, state.tape.slice(0, 10).map((t) => h("tr", null,
      h("td", { class: "dim" }, new Date(t.time).toISOString().slice(11, 19)),
      h("td", { class: `n ${t.buy ? "up" : "down"}` }, px(t.px)),
      h("td", { class: "n" }, compact(t.sz * t.px))))))
    : h("p", { class: "dim t-pad t-micro" },
        state.stream === "live" ? "Trades appear here as they happen." : "The trade tape needs the live stream.");
  replace(el.book, table, tape);
}

function paintSignals(el, adv) {
  replace(el.sig, dense([
    { label: "Coin", amber: true, cell: (r) => r.coin },
    { label: "Side", cell: (r) => r.side, cls: (r) => (r.side === "long" ? "up" : "down") },
    { label: "Called", cell: (r) => (r.allowed ? "YES" : "no"), cls: (r) => (r.allowed ? "" : "dim") },
    { label: "Lev", num: true, cell: (r) => (r.allowed ? `${r.leverage}x` : "–") },
    { label: "1st half", num: true, cell: (r) => (r.right_first === null ? "–" : `${(r.right_first * 100).toFixed(0)}%`),
      heat: (r) => (r.right_first === null ? "" : heat((r.right_first - 0.5) * 100, 15)) },
    { label: "2nd half", num: true, cell: (r) => (r.right_second === null ? "–" : `${(r.right_second * 100).toFixed(0)}%`),
      heat: (r) => (r.right_second === null ? "" : heat((r.right_second - 0.5) * 100, 15)) },
    { label: "Avg 1st", num: true, cell: (r) => pct(r.avg_first, 1), cls: (r) => tone(r.avg_first) },
    { label: "Avg 2nd", num: true, cell: (r) => pct(r.avg_second, 1), cls: (r) => tone(r.avg_second) },
    { label: "Now", cell: (r) => (r.active ? "ACTIVE" : r.pointed ? "skipped" : "—"),
      cls: (r) => (r.active ? "amber" : "dim") },
  ], adv.signals),
    h("p", { class: "dim t-pad t-micro" },
      "A side is called only if it was right in more than half its weeks and made money in both halves of the history. " +
      "Skipped means the signal points that way but the side failed that test."));
}

function paintRisk(el, adv) {
  const risk = adv.risk;
  replace(el.risk, dense([
    { label: "Position", amber: true, cell: (r) => `${r.coin} ${r.side} ${r.leverage}x` },
    { label: "Exposure", num: true, cell: (r) => `$${r.exposure_usd.toFixed(2)}` },
    { label: "P&L", num: true, cell: (r) => usd(r.pnl_usd), cls: (r) => tone(r.pnl_usd) },
    { label: "To stop", num: true, cell: (r) => (r.stop_distance_pct === null || r.stop_distance_pct === undefined
        ? (r.status === "target" ? "profit taken" : r.status) : `${r.stop_distance_pct.toFixed(1)}%`) },
    { label: "To liq.", num: true, cell: (r) => (r.liquidation_distance_pct === null
        ? "–" : `${r.liquidation_distance_pct.toFixed(0)}%`) },
    { label: "At stop", num: true, cell: (r) => (r.loss_at_stop_usd ? usd(-r.loss_at_stop_usd) : "–"),
      cls: () => "down" },
  ], risk.rows, { empty: "No open positions this week." }),
    h("table", { class: "t-dense t-wrap" }, h("tbody", null,
      h("tr", null, h("td", { class: "dim" }, "Total exposure"),
        h("td", { class: "n" }, `$${(risk.exposure_usd || 0).toFixed(2)} · ${((risk.exposure_of_stake || 0) * 100).toFixed(0)}% of the weekly amount`)),
      h("tr", null, h("td", { class: "dim" }, "If every stop is hit"),
        h("td", { class: "n down" }, risk.worst_case_usd ? usd(risk.worst_case_usd) : "$0.00")),
      h("tr", null, h("td", { class: "dim" }, "Left in cash"),
        h("td", { class: "n" }, `$${(risk.in_cash_usd || 0).toFixed(2)}`)))),
    exitTable(adv.exits));
}

// The exit rules in use, each against the alternative it was tested with.
function exitTable(exits) {
  if (!exits) return null;
  const row = (name, e, used) => h("tr", null, h("td", { class: used ? "amber" : "dim" }, name),
    h("td", { class: "n" }, String(e.weeks)), h("td", { class: "n" }, `${(e.ahead * 100).toFixed(0)}%`),
    h("td", { class: `n ${tone(e.avg)}` }, pct(e.avg * 100, 1)),
    h("td", { class: "n down" }, pct(e.worst * 100, 0)));
  const rows = [];
  const plans = exits.take_profit || {};
  for (const [side, word] of [["short", "Shorts"], ["long", "Longs"]]) {
    const plan = plans[side];
    if (!plan) continue;
    if (plan.target) {
      rows.push(row(`${word}: stop and take profit`, plan.with, true),
                row(`${word}: stop only`, plan.stop_only, false));
    } else {
      rows.push(row(`${word}: stop only, no take profit`, plan.stop_only, true));
      if (plan.at_one_normal_week) {
        rows.push(row(`${word}: with a take profit`, plan.at_one_normal_week, false));
      }
    }
  }
  if (!rows.length && exits.with && exits.holding) {
    rows.push(row(`Stop ${exits.stop} normal week away`, exits.with, true),
              row("Hold to Monday, no stop", exits.holding, false));
  }
  if (!rows.length) return null;
  return [h("table", { class: "t-dense" },
    h("thead", null, h("tr", null, h("th", null, "Exit rule"), h("th", { class: "n" }, "Weeks"),
      h("th", { class: "n" }, "Ahead"), h("th", { class: "n" }, "Avg week"), h("th", { class: "n" }, "Worst week"))),
    h("tbody", null, rows)),
    h("p", { class: "dim t-pad t-micro" },
      "Share of the stake, at the suggested leverage, with the take profit one normal week away. Amber rows " +
      "are the rules in use: shorts bank profit because falls tend to bounce, longs are left to run.")];
}

function paintPerformance(el, adv) {
  const names = Object.keys(adv.performance);
  if (!names.length) {
    replace(el.perf, h("p", { class: "dim t-pad" }, "No data."));
    return;
  }
  const all = names.flatMap((n) => adv.performance[n]);
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 300 130");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Thirty-day performance of each market, rebased to 100");
  if (lo < 100 && hi > 100) svg.append(rule(130 - 2 - (100 - lo) / (hi - lo) * 126, 300, COLORS.text));
  names.forEach((n, i) => svg.append(line(adv.performance[n], SERIES[i % SERIES.length], 300, 130, lo, hi)));
  replace(el.perf, h("div", { class: "t-pad" }, svg,
    h("div", { class: "t-keys" }, names.map((n, i) => {
      const last = adv.performance[n][adv.performance[n].length - 1];
      return h("span", null, h("i", { style: `background:${SERIES[i % SERIES.length]}` }), `${n} `,
        h("b", { class: `num ${tone(last - 100)}` }, last.toFixed(0)));
    })),
    h("div", { class: "dim t-micro" }, "Each market starts at 100 thirty days ago (dashed line).")));
}

function paintCorrelation(el, adv) {
  const { labels, matrix } = adv.correlation;
  if (!labels.length) {
    replace(el.corr, h("p", { class: "dim t-pad" }, "Not enough shared history."));
    return;
  }
  replace(el.corr, h("div", { class: "t-scroll" }, h("table", { class: "t-dense t-corr" },
    h("thead", null, h("tr", null, h("th", null, ""), labels.map((l) => h("th", { class: "n" }, l)))),
    h("tbody", null, matrix.map((row, i) => h("tr", null,
      h("td", { class: "amber" }, labels[i]),
      row.map((v, j) => h("td", { class: "n", style: i === j ? "" : heat(v * 100, 100) },
        i === j ? "—" : v.toFixed(2)))))))),
    h("p", { class: "dim t-pad t-micro" }, "1.00 means they moved together, 0 means unrelated. From 4-hour moves."));
}

function paintLeverage(el, adv) {
  const { levels, rows } = adv.leverage;
  replace(el.lev, dense([
    { label: "Call", amber: true, cell: (r) => `${r.coin} ${r.side}` },
    ...levels.map((l) => ({
      label: `${l}x`, num: true,
      cell: (r) => (r.liquidated[l] === null || r.liquidated[l] === undefined ? "–" : `${(r.liquidated[l] * 100).toFixed(0)}%`),
      heat: (r) => (r.liquidated[l] ? heat(-r.liquidated[l] * 100, 60) : ""),
      cls: (r) => (r.suggested === l ? "t-pick" : ""),
    })),
  ], rows),
    h("p", { class: "dim t-pad t-micro" }, "Outlined: the suggested leverage, the highest that was liquidated in at most 2% of weeks."),
    dense([
      { label: "Avg week", amber: true, cell: (r) => `${r.coin} ${r.side}` },
      ...levels.map((l) => ({
        label: `${l}x`, num: true,
        cell: (r) => (r.avg[l] === null || r.avg[l] === undefined ? "–" : pct(r.avg[l] * 100, 1)),
        cls: (r) => `${tone(r.avg[l])} ${r.suggested === l ? "t-pick" : ""}`,
      })),
    ], rows),
    h("p", { class: "dim t-pad t-micro" },
      "Average traded week as a share of the stake, with no stop. More leverage stops paying once the wipe-outs pile up."));
}

function paintScore(el, adv) {
  const board = adv.scoreboard;
  if (!board) {
    replace(el.score, h("p", { class: "dim t-pad" }, "No finished weeks yet."));
    return;
  }
  const totals = [...board.rows].reverse().map((r) => r.running_total);
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 300 40");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  if (totals.length > 1) {
    const lo = Math.min(0, ...totals);
    const hi = Math.max(0, ...totals);
    svg.append(rule(40 - 2 - (0 - lo) / ((hi - lo) || 1) * 36, 300, COLORS.text));
    svg.append(line(totals, board.total >= 0 ? COLORS.up : COLORS.down, 300, 40, lo, hi));
  }
  replace(el.score, h("div", { class: "t-pad" },
    h("div", { class: "num" }, `${board.ahead} ahead · ${board.behind} behind · ${board.no_trade} no trade · total `,
      h("b", { class: tone(board.total) }, usd(board.total))), svg),
    dense([
      { label: "Week of", amber: true, cell: (r) => r.week_of.slice(5) },
      { label: "Calls", cell: (r) => (r.calls.length
          ? r.calls.map((c) => `${c.side[0].toUpperCase()} ${c.coin}`).join(", ")
          : r.too_small && r.too_small.length ? "too small" : "stay out"),
        cls: (r) => (r.calls.length ? "" : "dim") },
      { label: "Result", num: true, cell: (r) => (r.calls.length ? usd(r.result) : "–"),
        cls: (r) => tone(r.result) },
      { label: "Total", num: true, cell: (r) => usd(r.running_total), cls: (r) => tone(r.running_total) },
    ], board.rows));
}

function paintFunding(el, adv) {
  replace(el.fund, dense([
    { label: "Coin", amber: true, cell: (r) => r.label },
    { label: "Funding/yr", num: true, cell: (r) => pct(r.funding_apr, 1), heat: (r) => heat(r.funding_apr, 30) },
    { label: "Open interest", num: true, cell: (r) => `$${compact(r.open_interest_usd)}` },
    { label: "Volume today", num: true, cell: (r) => `$${compact(r.day_volume_usd)}` },
  ], adv.funding),
    h("p", { class: "dim t-pad t-micro" }, "Positive funding is paid by longs to shorts; negative, by shorts to longs."));
}

function paintForex(el, adv) {
  replace(el.fx, dense([
    { label: "Pair", amber: true, cell: (r) => r.pair },
    { label: "Last", num: true, cell: (r) => px(r.price) },
    { label: "Last wk", num: true, cell: (r) => pct(r.last_week_pct), heat: (r) => heat(r.last_week_pct, 2.5) },
    { label: "Normal wk", num: true, cell: (r) => `${r.normal_week_pct.toFixed(2)}%` },
    { label: "Range this week", num: true, cell: (r) => `${px(r.range_low)}–${px(r.range_high)}` },
    { label: "13w", cell: (r) => (r.trend_13w === "up" ? "▲" : "▼"), cls: (r) => (r.trend_13w === "up" ? "up" : "down") },
    { label: "52w", cell: (r) => (r.trend_52w === "up" ? "▲" : "▼"), cls: (r) => (r.trend_52w === "up" ? "up" : "down") },
  ], adv.forex),
    h("p", { class: "dim t-pad t-micro" }, "No rule tested has an edge on a forex direction. Ranges are for sizing your own idea."));
}

export function paintAdvanced(el, state, helpers) {
  if (!el.wks) return;
  paintSessions(el);
  paintBook(el, state, helpers);
  const adv = state.adv;
  if (!adv) {
    for (const key of ["mov", "brd", "mood", "wks", "sig", "risk", "perf", "corr", "lev", "score", "fund", "fx"]) {
      if (!el[key].childNodes.length) replace(el[key], h("p", { class: "dim t-pad" }, "Loading…"));
    }
    return;
  }
  paintMovers(el, adv);
  paintBreadth(el, adv);
  paintMood(el, state);
  paintWorksheet(el, state, helpers);
  paintSignals(el, adv);
  paintRisk(el, adv);
  paintPerformance(el, adv);
  paintCorrelation(el, adv);
  paintLeverage(el, adv);
  paintScore(el, adv);
  paintFunding(el, adv);
  paintForex(el, adv);
}
