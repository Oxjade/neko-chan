import {
  api, h, replace, fmt, tone, gauge, table, figures, directionWord, signInForm,
  tradingViewChart,
} from "/web/common.js";

const app = document.getElementById("app");
const signout = document.getElementById("signout");

const state = { overview: null, result: null, flash: null, sizing: loadSizing(),
                form: { pair: "EURUSD", direction: "long", stop: "", target: "", cost: "0" },
                chartPair: null, chartNode: null };

// Shared with the desk: a per-browser convenience, nothing depends on it.
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

function pct(v, digits = 2) {
  return v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

function share(v) {
  return v === null || v === undefined ? "–" : `${(v * 100).toFixed(0)}%`;
}

function pairRow(pair) {
  return state.overview.pairs.find((r) => r.pair === pair);
}

function suggest(pair) {
  const row = pairRow(pair);
  if (!row) return;
  state.form.stop = (0.75 * row.normal_week_pct).toFixed(2);
  state.form.target = (1.5 * row.normal_week_pct).toFixed(2);
}

// ---- left column -----------------------------------------------------------

function planForm() {
  const f = state.form;
  const error = h("p", { class: "loss small", role: "alert" });
  const pair = h("select", { name: "pair" }, state.overview.pairs.map((r) =>
    h("option", { value: r.pair, selected: r.pair === f.pair }, r.pair)));
  const direction = h("select", { name: "direction" },
    h("option", { value: "long", selected: f.direction === "long" }, "Long (buy)"),
    h("option", { value: "short", selected: f.direction === "short" }, "Short (sell)"));
  const stop = h("input", { type: "text", name: "stop", inputmode: "decimal", value: f.stop });
  const target = h("input", { type: "text", name: "target", inputmode: "decimal", value: f.target });
  const account = h("input", { type: "text", name: "account", inputmode: "decimal",
                               value: String(state.sizing.account) });
  const risk = h("input", { type: "text", name: "risk", inputmode: "decimal",
                            value: String(state.sizing.risk) });
  const cost = h("input", { type: "text", name: "cost", inputmode: "decimal", value: f.cost });
  const normal = h("p", { class: "soft small" });
  const describe = () => {
    const row = pairRow(pair.value);
    normal.textContent = row
      ? `A normal week for ${row.pair} is ${row.normal_week_pct.toFixed(2)}% right now.`
      : "";
  };
  describe();
  pair.addEventListener("change", () => {
    f.pair = pair.value;
    suggest(pair.value);
    stop.value = f.stop;
    target.value = f.target;
    describe();
    showChart(pair.value);
  });
  const button = h("button", { class: "button", type: "submit" }, "Check this plan");
  return h("form", { class: "panel stack", novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      const values = { stop: Number(stop.value), target: Number(target.value),
                       account: Number(account.value), risk: Number(risk.value),
                       cost: Number(cost.value || 0) };
      const need = (ok, field, text) => {
        if (ok) return false;
        error.textContent = text;
        field.focus();
        return true;
      };
      if (need(values.stop >= 0.05 && values.stop <= 20, stop,
               "Enter the stop distance as a percentage between 0.05 and 20.")) return;
      if (need(values.target >= 0.05 && values.target <= 50, target,
               "Enter the target distance as a percentage between 0.05 and 50.")) return;
      if (need(values.account > 0, account, "Enter your account size in USD.")) return;
      if (need(values.risk > 0 && values.risk <= 100, risk,
               "Enter the risk per trade as a percentage between 0 and 100.")) return;
      if (need(values.cost >= 0 && values.cost <= 5, cost,
               "Enter the trading cost as a percentage between 0 and 5, or 0 for none.")) return;
      Object.assign(f, { pair: pair.value, direction: direction.value,
                         stop: stop.value, target: target.value, cost: cost.value || "0" });
      state.sizing = { account: values.account, risk: values.risk };
      saveSizing();
      button.disabled = true;
      state.flash = null;
      try {
        state.result = await api("/api/web/weekly/plan", {
          pair: f.pair, direction: f.direction, stop_pct: values.stop, target_pct: values.target,
          account_usd: values.account, risk_pct: values.risk, cost_pct: values.cost });
      } catch (err) {
        if (err.status === 401) {
          renderSignIn([]);
          return;
        }
        state.result = null;
        state.flash = { bad: true, text: err.message };
      }
      document.activeElement.blur();
      render();
    } },
    h("h2", { style: "font-size:var(--t-h3)" }, "Your plan for the week"),
    h("label", { class: "field" }, h("span", null, "Pair"), pair),
    h("label", { class: "field" }, h("span", null, "Direction"), direction),
    normal,
    h("label", { class: "field" }, h("span", null, "Stop distance (%)"), stop),
    h("label", { class: "field" }, h("span", null, "Target distance (%)"), target),
    h("label", { class: "field" }, h("span", null, "Account size (USD)"), account),
    h("label", { class: "field" }, h("span", null, "Risk per trade (%)"), risk),
    h("label", { class: "field" }, h("span", null, "Trading cost per trade (%, optional)"), cost),
    h("p", { class: "soft small" },
      "Leave at 0 to see results before costs, or enter your broker's spread and commission."),
    error, button);
}

// ---- right column ----------------------------------------------------------

function verdictText(r) {
  const a = r.all;
  const won = share(a.win_rate);
  const needed = `${share(a.breakeven_win_rate)}${r.cost_pct > 0 ? "" : " before trading costs"}`;
  if (a.breakeven_win_rate === null) return `This shape won ${won} of past weeks on ${r.pair}.`;
  if (a.win_rate_low > a.breakeven_win_rate) {
    return `This shape has beaten break-even on ${r.pair}: it won ${won} of weeks and needed ${needed}.`;
  }
  if (a.win_rate > a.breakeven_win_rate) {
    return `This shape has been about break-even on ${r.pair}: it won ${won} of weeks and needed ${needed}.`;
  }
  return `This shape has lost money on ${r.pair}: it won ${won} of weeks and needed ${needed}.`;
}

function resultSection() {
  const r = state.result;
  if (!r) {
    return h("section", { class: "panel stack" },
      h("h2", { style: "font-size:var(--t-h3)" }, "No plan checked yet"),
      h("p", { class: "soft" },
        "Fill in the pair, direction, stop and target on the left to see what a plan of that shape has done since 1999."));
  }
  const a = r.all;
  const s = r.sizing;
  const side = directionWord(r.direction);
  const trendRows = [
    { name: "With the 13-week trend", ...r.with_trend },
    { name: "Against the 13-week trend", ...r.against_trend },
  ];
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, `${side} ${r.pair}, checked against ${a.weeks} past weeks`),
    h("div", { class: "panel" }, gauge({
      trades: a.weeks, wins: a.wins, winRate: a.win_rate, low: a.win_rate_low,
      high: a.win_rate_high, breakeven: a.breakeven_win_rate, verdictText: verdictText(r) })),
    figures([
      ["Entry (last fixing)", fmt.price(r.price)],
      ["Stop", fmt.price(r.stop_price)],
      ["Target", fmt.price(r.target_price)],
      ["Stop in normal weeks", r.stop_in_weeks.toFixed(2)],
      ["Target in normal weeks", r.target_in_weeks.toFixed(2)],
    ]),
    h("h3", null, "What happened in past weeks"),
    figures([
      ["Target reached first", share(a.target_first)],
      ["Stop reached first", share(a.stop_first)],
      ["Neither by Friday", share(a.week_ended)],
      ["Average result when neither", pct(a.week_ended_avg_pct), tone(a.week_ended_avg_pct)],
      ["Average result per trade", pct(a.avg_result_pct, 3), tone(a.avg_result_pct)],
      ["That average at your size", fmt.signedUsd(r.avg_result_usd), tone(r.avg_result_usd)],
    ]),
    h("p", { class: "notice" },
      `With ${fmt.usd(state.sizing.account)} and ${state.sizing.risk}% risk: ` +
      `${s.lots >= 0.01 ? s.lots.toFixed(2) : s.lots.toFixed(4)} lots ` +
      `(${Math.round(s.units).toLocaleString("en-US")} units, ${fmt.usd(s.position_usd)}, ` +
      `${s.leverage.toFixed(1)}x). The stop costs ${fmt.usd(s.loss_at_stop_usd)}; the target ` +
      `makes ${fmt.usd(s.gain_at_target_usd)}.` +
      (s.lots < 0.01
        ? " That is below the 0.01-lot minimum at most brokers, so this account is too small to risk so little on this stop."
        : "")),
    h("h3", null, "Does the trend matter for this shape?"),
    table([
      { label: "Past weeks", cell: (t) => t.name },
      { label: "Weeks", num: true, cell: (t) => String(t.weeks) },
      { label: "Won", num: true, cell: (t) => share(t.win_rate) },
      { label: "Needed", num: true, cell: (t) => share(t.breakeven_win_rate) },
      { label: "Average result", num: true, cell: (t) => pct(t.avg_result_pct, 3),
        cls: (t) => tone(t.avg_result_pct) },
    ], trendRows, ""),
    h("p", { class: "soft small" },
      `Your plan is ${r.plan_is_with_trend ? "with" : "against"} the current 13-week trend, which is ` +
      `${r.trend_13w}. Stops and targets are checked on one central-bank fixing a day since ` +
      `${r.history_from.slice(0, 4)}, so real intraday touches happen a little more often. ` +
      (r.cost_pct > 0
        ? `Trading costs of ${r.cost_pct}% per trade are included; overnight financing is not.`
        : "No trading costs are included, so real results will be a little lower.")));
}

function rangesSection() {
  const rows = state.overview.pairs;
  const held = rows.reduce((sum, r) => sum + r.range_held, 0) / (rows.length || 1);
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "This week's normal ranges"),
    h("p", { class: "soft small" },
      `Rates as of ${state.overview.as_of}. In about ${share(held)} of past weeks the pair ` +
      `closed the week inside this range.`),
    table([
      { label: "Pair", cell: (r) => r.pair },
      { label: "Last fixing", num: true, cell: (r) => fmt.price(r.price) },
      { label: "Last week", num: true, cell: (r) => pct(r.last_week_pct), cls: (r) => tone(r.last_week_pct) },
      { label: "Normal week", num: true, cell: (r) => `${r.normal_week_pct.toFixed(2)}%` },
      { label: "Likely range", cell: (r) => `${fmt.price(r.range_low)} to ${fmt.price(r.range_high)}` },
      { label: "13-week trend", cell: (r) => (r.trend_13w === "up" ? "Up" : "Down") },
      { label: "52-week trend", cell: (r) => (r.trend_52w === "up" ? "Up" : "Down") },
    ], rows, "No rates are available."));
}

const chartTitle = h("h2", { style: "font-size:var(--t-h3)" });

// Swap the chart in place when the pair changes; leave it alone otherwise so
// a re-render does not reload it.
function showChart(pair) {
  if (state.chartPair === pair && state.chartNode) return;
  const next = tradingViewChart(`FX_IDC:${pair}`, "D");
  if (state.chartNode && state.chartNode.isConnected) state.chartNode.replaceWith(next);
  state.chartNode = next;
  state.chartPair = pair;
  chartTitle.textContent = `${pair} chart`;
}

function chartSection() {
  showChart(state.form.pair);
  return h("section", { class: "stack" }, chartTitle, state.chartNode);
}

function render() {
  signout.hidden = false;
  replace(app,
    state.flash
      ? h("p", { class: state.flash.bad ? "notice bad" : "notice", role: "status" }, state.flash.text)
      : null,
    h("p", { class: "notice", style: "margin-top:var(--s4)" },
      "No rule tested picks a pair's direction for the week better than a coin flip (14 rules, 27 years of rates). The direction is your call. This page shows what a plan of your shape has done, and how big to trade it."),
    h("div", { class: "console", style: "margin-top:var(--s5)" },
      h("aside", null, planForm()),
      h("main", null, h("div", { class: "stack" },
        resultSection(), chartSection(), rangesSection()))));
}

// ---- start -----------------------------------------------------------------

function renderSignIn(missing) {
  signout.hidden = true;
  replace(app, signInForm(missing, start));
}

signout.addEventListener("click", async () => {
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
    state.overview = await api("/api/web/weekly");
  } catch (err) {
    replace(app, h("p", { class: "notice bad" }, err.message));
    return;
  }
  if (!pairRow(state.form.pair) && state.overview.pairs.length) {
    state.form.pair = state.overview.pairs[0].pair;
  }
  if (!state.form.stop) suggest(state.form.pair);
  render();
}

start();
