// Forex plan: the trader's own idea for the week (pair, direction, stop and
// target in pips), what a plan of that shape has done since 1999, and how big
// to trade it. Port of web/assets/weekly.js, without the sign-in.
//
// No rule tested picks a forex direction better than a coin flip, so this
// page never suggests one.

import { getForexOverview, getForexPlan, type ForexOverview, type ForexPair, type ForexPlan, type ForexSummary } from "../core/api";
import { directionWord, figures, fmt, gauge, h, replace, table, tone } from "./dom";

const app = document.getElementById("app") as HTMLElement;

interface Sizing {
  account: number;
  risk: number;
}

const state: {
  overview: ForexOverview | null;
  result: ForexPlan | null;
  flash: { bad: boolean; text: string } | null;
  sizing: Sizing;
  form: { pair: string; direction: string; stop: string; target: string; spread: string };
  chartPair: string | null;
  chartNode: HTMLElement | null;
} = { overview: null, result: null, flash: null, sizing: loadSizing(),
      form: { pair: "EURUSD", direction: "long", stop: "", target: "", spread: "0" },
      chartPair: null, chartNode: null };

const message = (err: unknown) => (err instanceof Error ? err.message : String(err));

// Account size and risk: a per-browser convenience, never sent anywhere.
function loadSizing(): Sizing {
  try {
    const saved = JSON.parse(localStorage.getItem("neko-desk-sizing") || "null");
    if (saved && saved.account > 0 && saved.risk > 0) return saved;
  } catch { /* storage unavailable */ }
  return { account: 100, risk: 1 };
}

function saveSizing(): void {
  try {
    localStorage.setItem("neko-desk-sizing", JSON.stringify(state.sizing));
  } catch { /* storage unavailable */ }
}

// Distances are in pips, the unit a forex broker quotes.
function pips(v: number | null | undefined): string {
  return v === null || v === undefined ? "–" : `${Number(v.toFixed(1))} pips`;
}

function signedPips(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(digits)} pips`;
}

// What one pip is worth. Small accounts trade sizes where a pip is a fraction of a
// cent, so under a dollar it is shown to four decimals: pips times this is the money.
function pipUsd(v: number): string {
  return v >= 1 ? fmt.usd(v) : `$${v.toFixed(4)}`;
}

function share(v: number | null | undefined): string {
  return v === null || v === undefined ? "–" : `${(v * 100).toFixed(0)}%`;
}

function pairRow(pair: string): ForexPair | undefined {
  return state.overview?.pairs.find((r) => r.pair === pair);
}

// A starting shape: a stop three quarters of a normal week away, a target twice as far.
function suggest(pair: string): void {
  const row = pairRow(pair);
  if (!row) return;
  state.form.stop = String(Math.max(1, Math.round(0.75 * row.normal_week_pips)));
  state.form.target = String(Math.max(1, Math.round(1.5 * row.normal_week_pips)));
}

// ---- left column -----------------------------------------------------------

function planForm(): HTMLElement {
  const f = state.form;
  const rows = state.overview?.pairs ?? [];
  const error = h("p", { class: "loss small", role: "alert" });
  const pair = h("select", { name: "pair" }, rows.map((r) =>
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
  const spread = h("input", { type: "text", name: "spread", inputmode: "decimal", value: f.spread });
  const normal = h("p", { class: "soft small" });
  const describe = () => {
    const row = pairRow(pair.value);
    normal.textContent = row
      ? `A normal week for ${row.pair} is ${Math.round(row.normal_week_pips)} pips ` +
        `(${row.normal_week_pct.toFixed(2)}%) right now. ` +
        "A pip is the fourth decimal place of the price (the second for yen pairs)."
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
                       spread: Number(spread.value || 0) };
      const need = (ok: boolean, field: HTMLInputElement, text: string) => {
        if (ok) return false;
        error.textContent = text;
        field.focus();
        return true;
      };
      if (need(values.stop > 0 && values.stop <= 5000, stop,
               "Enter the stop distance in pips: more than 0 and at most 5000.")) return;
      if (need(values.target > 0 && values.target <= 5000, target,
               "Enter the target distance in pips: more than 0 and at most 5000.")) return;
      if (need(values.account > 0, account, "Enter your account size in USD.")) return;
      if (need(values.risk > 0 && values.risk <= 100, risk,
               "Enter the risk per trade as a percentage between 0 and 100.")) return;
      if (need(values.spread >= 0 && values.spread < values.stop, spread,
               "Enter your broker's spread in pips: 0 or more, and smaller than the stop.")) return;
      Object.assign(f, { pair: pair.value, direction: direction.value,
                         stop: stop.value, target: target.value, spread: spread.value || "0" });
      state.sizing = { account: values.account, risk: values.risk };
      saveSizing();
      button.disabled = true;
      state.flash = null;
      try {
        state.result = await getForexPlan(f.pair, f.direction, values.stop, values.target,
                                          values.account, values.risk, values.spread);
      } catch (err) {
        state.result = null;
        state.flash = { bad: true, text: message(err) };
      }
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      render();
    } },
    h("h2", { style: "font-size:var(--t-h3)" }, "Your plan for the week"),
    h("label", { class: "field" }, h("span", null, "Pair"), pair),
    h("label", { class: "field" }, h("span", null, "Direction"), direction),
    normal,
    h("label", { class: "field" }, h("span", null, "Stop distance (pips)"), stop),
    h("label", { class: "field" }, h("span", null, "Target distance (pips)"), target),
    h("label", { class: "field" }, h("span", null, "Account size (USD)"), account),
    h("label", { class: "field" }, h("span", null, "Risk per trade (%)"), risk),
    h("label", { class: "field" }, h("span", null, "Broker's spread (pips, optional)"), spread),
    h("p", { class: "soft small" },
      "Leave at 0 to see results before costs, or enter what a round trip costs at your broker: the spread, plus any commission, in pips."),
    error, button);
}

// ---- right column ----------------------------------------------------------

function verdictText(r: ForexPlan): string {
  const a = r.all;
  const won = share(a.win_rate);
  const needed = `${share(a.breakeven_win_rate)}${r.spread_pips > 0 ? "" : " before trading costs"}`;
  if (a.breakeven_win_rate === null) return `This shape won ${won} of past weeks on ${r.pair}.`;
  if ((a.win_rate_low as number) > a.breakeven_win_rate) {
    return `This shape has beaten break-even on ${r.pair}: it won ${won} of weeks and needed ${needed}.`;
  }
  if ((a.win_rate as number) > a.breakeven_win_rate) {
    return `This shape has been about break-even on ${r.pair}: it won ${won} of weeks and needed ${needed}.`;
  }
  return `This shape has lost money on ${r.pair}: it won ${won} of weeks and needed ${needed}.`;
}

function resultSection(): HTMLElement {
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
  const trendRows: Array<ForexSummary & { name: string }> = [
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
      ["Stop", `${pips(r.stop_pips)}, at ${fmt.price(r.stop_price)}`],
      ["Target", `${pips(r.target_pips)}, at ${fmt.price(r.target_price)}`],
      ["Lost at the stop", fmt.usd(s.loss_at_stop_usd), "loss"],
      ["Made at the target", fmt.usd(s.gain_at_target_usd), "gain"],
      ["One pip at your size", pipUsd(s.pip_value_usd)],
      ["Stop in normal weeks", r.stop_in_weeks.toFixed(2)],
      ["Target in normal weeks", r.target_in_weeks.toFixed(2)],
    ]),
    h("h3", null, "What happened in past weeks"),
    figures([
      ["Target reached first", share(a.target_first)],
      ["Stop reached first", share(a.stop_first)],
      ["Neither by Friday", share(a.week_ended)],
      ["Average result when neither", signedPips(a.week_ended_avg_pips), tone(a.week_ended_avg_pips)],
      ["Average result per trade", signedPips(a.avg_result_pips), tone(a.avg_result_pips)],
      ["That average at your size", fmt.signedUsd(r.avg_result_usd), tone(r.avg_result_usd)],
    ]),
    h("p", { class: "notice" },
      `With ${fmt.usd(state.sizing.account)} and ${state.sizing.risk}% risk: ` +
      `${s.lots >= 0.01 ? s.lots.toFixed(2) : s.lots.toFixed(4)} lots ` +
      `(${Math.round(s.units).toLocaleString("en-US")} units, ${fmt.usd(s.position_usd)}, ` +
      `${s.leverage.toFixed(1)}x). One pip is worth ${pipUsd(s.pip_value_usd)} at that size ` +
      `(${pipUsd(s.pip_value_per_lot_usd)} on a full lot). The stop, ${pips(r.stop_pips)} away, costs ` +
      `${fmt.usd(s.loss_at_stop_usd)}; the target, ${pips(r.target_pips)} away, makes ` +
      `${fmt.usd(s.gain_at_target_usd)}.` +
      (s.lots < 0.01
        ? " That is below the 0.01-lot minimum at most brokers, so this account is too small to risk so little on this stop."
        : "")),
    h("h3", null, "Does the trend matter for this shape?"),
    table([
      { label: "Past weeks", cell: (t) => t.name },
      { label: "Weeks", num: true, cell: (t) => String(t.weeks) },
      { label: "Won", num: true, cell: (t) => share(t.win_rate) },
      { label: "Needed", num: true, cell: (t) => share(t.breakeven_win_rate) },
      { label: "Average result", num: true, cell: (t) => signedPips(t.avg_result_pips),
        cls: (t) => tone(t.avg_result_pips) },
    ], trendRows, ""),
    h("p", { class: "soft small" },
      `Your plan is ${r.plan_is_with_trend ? "with" : "against"} the current 13-week trend, which is ` +
      `${r.trend_13w}. Stops and targets are checked on one central-bank fixing a day since ` +
      `${r.history_from.slice(0, 4)}, so real intraday touches happen a little more often. ` +
      (r.spread_pips > 0
        ? `Your broker's spread of ${pips(r.spread_pips)} per trade is included; overnight financing is not.`
        : "No trading costs are included, so real results will be a little lower.")));
}

function rangesSection(): HTMLElement {
  const rows = state.overview?.pairs ?? [];
  const held = rows.reduce((sum, r) => sum + r.range_held, 0) / (rows.length || 1);
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "This week's normal ranges"),
    h("p", { class: "soft small" },
      `Rates as of ${state.overview?.as_of}. In about ${share(held)} of past weeks the pair ` +
      `closed the week inside this range.`),
    table<ForexPair>([
      { label: "Pair", cell: (r) => r.pair },
      { label: "Last fixing", num: true, cell: (r) => fmt.price(r.price) },
      { label: "Last week", num: true, cell: (r) => signedPips(r.last_week_pips, 0), cls: (r) => tone(r.last_week_pips) },
      { label: "Normal week", num: true, cell: (r) => `${Math.round(r.normal_week_pips)} pips` },
      { label: "Likely range", cell: (r) => `${fmt.price(r.range_low)} to ${fmt.price(r.range_high)}` },
      { label: "13-week trend", cell: (r) => (r.trend_13w === "up" ? "Up" : "Down") },
      { label: "52-week trend", cell: (r) => (r.trend_52w === "up" ? "Up" : "Down") },
    ], rows, "No rates are available."));
}

// ---- the pair's chart ----------------------------------------------------------
//
// The chart is TradingView's own widget, a few megabytes from another company's
// servers. It is loaded only when asked for, so the page itself stays light.

function tradingViewChart(symbol: string, interval = "D"): HTMLElement {
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

const chartTitle = h("h2", { style: "font-size:var(--t-h3)" });
let chartWanted = false;

// Swap the chart in place when the pair changes; leave it alone otherwise so
// a re-render does not reload it.
function showChart(pair: string): void {
  chartTitle.textContent = `${pair} chart`;
  if (state.chartPair === pair && state.chartNode) return;
  const next = chartWanted
    ? tradingViewChart(`FX_IDC:${pair}`, "D")
    : h("p", null, h("button", { class: "button quiet", type: "button",
        onclick: () => {
          chartWanted = true;
          state.chartPair = null;
          showChart(state.form.pair);
        } }, `Show the ${pair} chart`));
  if (state.chartNode && state.chartNode.isConnected) state.chartNode.replaceWith(next);
  state.chartNode = next;
  state.chartPair = pair;
}

function chartSection(): HTMLElement {
  showChart(state.form.pair);
  return h("section", { class: "stack" }, chartTitle, state.chartNode);
}

function render(): void {
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

async function start(): Promise<void> {
  try {
    state.overview = await getForexOverview();
  } catch (err) {
    replace(app, h("p", { class: "notice bad" }, message(err)));
    return;
  }
  if (!pairRow(state.form.pair) && state.overview.pairs.length) {
    state.form.pair = state.overview.pairs[0].pair;
  }
  if (!state.form.stop) suggest(state.form.pair);
  render();
}

start();
