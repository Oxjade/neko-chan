// Shared by the public page and the console: fetch, formatting, and the
// pieces both pages draw (gauge, figures, gate, equity chart, tables).
// Every value reaches the page through textContent, never innerHTML.

export async function api(path, body) {
  const options = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("The server did not answer. Check your connection and try again.");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    /* no body */
  }
  if (!response.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : null;
    const error = new Error(detail || `The request failed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data;
}

export function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function replace(target, ...children) {
  target.replaceChildren(...children.flat().filter(Boolean));
}

const usd = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2,
});

export const fmt = {
  usd: (v) => (v === null || v === undefined ? "–" : usd.format(v)),
  signedUsd: (v) =>
    v === null || v === undefined ? "–" : (v > 0 ? "+" : "") + usd.format(v),
  pct: (v, digits = 1) =>
    v === null || v === undefined ? "–" : `${(v * 100).toFixed(digits)}%`,
  signedPct: (v, digits = 2) =>
    v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`,
  price: (v) => {
    if (v === null || v === undefined) return "–";
    const digits = v >= 1000 ? 0 : v >= 10 ? 2 : 4;
    return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  },
  ratio: (v) => (v === null || v === undefined ? "–" : v.toFixed(2)),
  when: (iso) => {
    if (!iso) return "–";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "–";
    return date.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  },
};

export function tone(value) {
  if (value > 0) return "gain";
  if (value < 0) return "loss";
  return "";
}

// ---- the gauge ---------------------------------------------------------

function verdictOf(p) {
  if (!p.trades) return "none";
  if (p.breakeven === null || p.breakeven === undefined) return "unknown";
  if (p.low > p.breakeven) return "above";
  if (p.high < p.breakeven) return "below";
  return "unclear";
}

const VERDICT_TEXT = {
  none: "No closed trades yet, so there is no win rate to show.",
  unknown: "Too few trades to work out a break-even win rate.",
  above: "The win rate beats break-even across its whole likely range.",
  below: "The win rate is under break-even across its whole likely range.",
  unclear: "Not proven either way: the likely range still straddles break-even.",
};

function edgeClass(position) {
  if (position < 14) return " edge-left";
  if (position > 86) return " edge-right";
  return "";
}

// p: { trades, wins, winRate, low, high, breakeven } with rates as 0..1
export function gauge(p) {
  const verdict = verdictOf(p);
  const track = h("div", { class: "gauge-track" });
  const root = h("div", {
    class: "gauge", "data-verdict": verdict, role: "img",
    "aria-label": p.trades
      ? `Win rate ${fmt.pct(p.winRate)} over ${p.trades} trades, likely range ` +
        `${fmt.pct(p.low)} to ${fmt.pct(p.high)}` +
        (p.breakeven != null ? `, break-even ${fmt.pct(p.breakeven)}` : "")
      : "No closed trades yet",
  });
  if (p.trades) {
    const left = p.low * 100;
    const width = Math.max((p.high - p.low) * 100, 1);
    track.append(
      h("div", { class: "gauge-range", style: `left:${left}%;width:${width}%` }),
      h("div", { class: "gauge-dot", style: `left:${p.winRate * 100}%` }),
    );
    const at = p.winRate * 100;
    root.append(h("div", { class: `gauge-label bottom${edgeClass(at)}`, style: `left:${at}%` },
      `Win rate ${fmt.pct(p.winRate)}`));
  }
  if (p.breakeven !== null && p.breakeven !== undefined) {
    const at = p.breakeven * 100;
    track.append(h("div", { class: "gauge-mark", style: `left:${at}%` }));
    root.append(h("div", { class: `gauge-label top${edgeClass(at)}`, style: `left:${at}%` },
      `Break-even ${fmt.pct(p.breakeven)}`));
  }
  root.append(track, h("div", { class: "gauge-ends", "aria-hidden": "true" },
    h("span", null, "0%"), h("span", null, "100%")));
  return h("div", { class: "stack" },
    h("p", { class: "verdict" }, p.verdictText || VERDICT_TEXT[verdict]),
    root,
    p.trades
      ? h("p", { class: "soft small" },
          `${p.wins} wins in ${p.trades} closed trades. The shaded bar is the range the true ` +
          `win rate most likely sits in (95% interval); it narrows as trades accumulate.`)
      : null);
}

export function gaugeFromPerformance(perf, verdictText) {
  return gauge({
    verdictText,
    trades: perf.trades, wins: perf.wins, winRate: perf.win_rate,
    low: perf.win_rate_low, high: perf.win_rate_high, breakeven: perf.breakeven_win_rate,
  });
}

// ---- figures, gate, chart, tables ---------------------------------------

export function figures(items) {
  return h("dl", { class: "figures" }, items.map(([label, value, cls]) =>
    h("div", null, h("dt", null, label), h("dd", { class: cls || "" }, value))));
}

export function performanceFigures(perf) {
  return figures([
    ["Closed trades", String(perf.trades)],
    ["Average trade after fees", fmt.signedUsd(perf.expectancy), tone(perf.expectancy)],
    ["Net result", fmt.signedPct(perf.trades ? perf.return_pct : null), tone(perf.return_pct)],
    ["Fees paid", fmt.usd(perf.fees)],
    ["Average win to average loss", fmt.ratio(perf.payoff_ratio)],
    ["Worst drawdown", perf.trades ? `${perf.max_drawdown_pct.toFixed(1)}%` : "–"],
  ]);
}

export function gateList(gate) {
  return h("ul", { class: "gate" }, gate.checks.map((check) =>
    h("li", { "data-passed": String(check.passed) },
      h("span", { class: "tick", "aria-hidden": "true" }, check.passed ? "✓" : ""),
      h("div", null,
        h("div", null, check.label,
          h("span", { class: "soft" }, check.passed ? " (passed)" : " (not yet)")),
        h("div", { class: "soft small" }, check.detail)))));
}

export function equityChart(curve, starting) {
  if (!curve.length) {
    return h("p", { class: "empty" }, "The equity line appears after the first closed trade.");
  }
  const values = [starting, ...curve.map((p) => p.equity)];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const W = 1000;
  const H = 180;
  const pad = 8;
  const x = (i) => (i / (values.length - 1)) * W;
  const y = (v) => pad + (1 - (v - min) / span) * (H - 2 * pad);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  const last = values[values.length - 1];
  svg.setAttribute("aria-label",
    `Equity after each closed trade, from ${fmt.usd(starting)} to ${fmt.usd(last)}`);
  const base = document.createElementNS(ns, "line");
  base.setAttribute("x1", "0");
  base.setAttribute("x2", String(W));
  base.setAttribute("y1", String(y(starting)));
  base.setAttribute("y2", String(y(starting)));
  base.setAttribute("stroke", "var(--line)");
  base.setAttribute("stroke-width", "2");
  base.setAttribute("stroke-dasharray", "6 6");
  base.setAttribute("vector-effect", "non-scaling-stroke");
  const line = document.createElementNS(ns, "polyline");
  line.setAttribute("points", values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" "));
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", last >= starting ? "var(--gain)" : "var(--loss)");
  line.setAttribute("stroke-width", "2.5");
  line.setAttribute("stroke-linejoin", "round");
  line.setAttribute("vector-effect", "non-scaling-stroke");
  svg.append(base, line);
  return h("div", { class: "chart" }, svg,
    h("div", { class: "axis" },
      h("span", null, `Start ${fmt.usd(starting)} (dashed)`),
      h("span", null, `Low ${fmt.usd(min)}`),
      h("span", null, `High ${fmt.usd(max)}`),
      h("span", { class: tone(last - starting) }, `Now ${fmt.usd(last)}`)));
}

// columns: [{ label, num, cell(row) -> string | Node, cls(row) }]
export function table(columns, rows, emptyText) {
  if (!rows.length) return h("p", { class: "empty" }, emptyText);
  return h("div", { class: "table-scroll" },
    h("table", null,
      h("thead", null, h("tr", null, columns.map((c) =>
        h("th", { scope: "col", class: c.num ? "num" : "" }, c.label)))),
      h("tbody", null, rows.map((row) => h("tr", null, columns.map((c) =>
        h("td", { class: [c.num ? "num" : "", c.cls ? c.cls(row) : ""].join(" ").trim() },
          c.cell(row))))))));
}

export function directionWord(direction) {
  return direction === "short" ? "Short" : "Long";
}

export function marketLabel(symbol) {
  return String(symbol || "").split(":").pop();
}

export const positionColumns = [
  { label: "Market", cell: (p) => marketLabel(p.symbol) },
  { label: "Side", cell: (p) => directionWord(p.direction) },
  { label: "Entry", num: true, cell: (p) => fmt.price(p.entry) },
  { label: "Last price", num: true, cell: (p) => fmt.price(p.mark) },
  { label: "Stop", num: true, cell: (p) => fmt.price(p.stop) },
  { label: "Target", num: true, cell: (p) => fmt.price(p.target) },
  { label: "Leverage", num: true, cell: (p) => `${p.leverage}x` },
  { label: "Unrealised", num: true, cell: (p) => fmt.signedUsd(p.unrealized),
    cls: (p) => tone(p.unrealized) },
];

export const tradeColumns = [
  { label: "Closed", cell: (t) => fmt.when(t.closed_at) },
  { label: "Market", cell: (t) => marketLabel(t.symbol) },
  { label: "Side", cell: (t) => directionWord(t.direction) },
  { label: "Entry", num: true, cell: (t) => fmt.price(t.entry) },
  { label: "Exit", num: true, cell: (t) => fmt.price(t.exit) },
  { label: "Size", num: true, cell: (t) => fmt.usd(t.notional) },
  { label: "Leverage", num: true, cell: (t) => `${t.leverage}x` },
  { label: "Before fees", num: true, cell: (t) => fmt.signedUsd(t.gross), cls: (t) => tone(t.gross) },
  { label: "Fees", num: true, cell: (t) => fmt.usd(t.fees) },
  { label: "After fees", num: true, cell: (t) => fmt.signedUsd(t.net), cls: (t) => tone(t.net) },
];

// ---- setup notice and sign-in, shared by the console and the desk -----------

const SETUP_HELP = {
  WEB_OPERATOR_PASSWORD: "the password for the console and the desk",
  TG_VAULT_MASTER_KEY: "the key that encrypts stored secrets, needed to create bots",
};

export function setupNotice(missing) {
  if (!missing || !missing.length) return null;
  return h("div", { class: "notice stack" },
    h("p", null, "Finish the server setup. Set these environment variables and restart:"),
    h("ul", null, missing.map((name) =>
      h("li", null, h("code", null, name), `: ${SETUP_HELP[name] || ""}`))));
}

export function signInForm(missing, onSignedIn) {
  const error = h("p", { class: "loss small", role: "alert" });
  const password = h("input", { type: "password", name: "password",
                                autocomplete: "current-password", required: true });
  const button = h("button", { class: "button", type: "submit" }, "Sign in");
  const off = (missing || []).includes("WEB_OPERATOR_PASSWORD");
  button.disabled = off;
  const form = h("form", { class: "panel stack", novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      if (!password.value) {
        error.textContent = "Enter the console password.";
        password.focus();
        return;
      }
      button.disabled = true;
      try {
        await api("/api/web/login", { password: password.value });
        await onSignedIn();
      } catch (err) {
        error.textContent = err.message;
        button.disabled = false;
        password.select();
      }
    } },
    h("h1", { style: "font-size:var(--t-h2)" }, "Sign in"),
    off ? setupNotice(["WEB_OPERATOR_PASSWORD"]) : null,
    h("label", { class: "field" }, h("span", null, "Console password"), password),
    error, button);
  if (!off) queueMicrotask(() => password.focus());
  return h("div", { class: "signin" }, form);
}

// ---- live chart (TradingView's free embeddable widget) ----------------------
// The chart is drawn by TradingView in the viewer's browser. It is for looking
// at the market; none of the numbers on these pages come from it.

export function tradingViewChart(symbol, interval = "D") {
  const config = {
    autosize: true, symbol, interval, timezone: "Etc/UTC", theme: "light", style: "1",
    locale: "en", allow_symbol_change: true, hide_side_toolbar: true, calendar: false,
    support_host: "https://www.tradingview.com",
  };
  const script = document.createElement("script");
  script.type = "text/javascript";
  script.async = true;
  script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
  script.textContent = JSON.stringify(config);
  return h("div", { class: "tradingview-widget-container chart-embed" },
    h("div", { class: "tradingview-widget-container__widget" }),
    h("div", { class: "tradingview-widget-copyright soft small" },
      h("a", { href: "https://www.tradingview.com/", rel: "noopener nofollow", target: "_blank" },
        "Chart by TradingView")),
    script);
}
