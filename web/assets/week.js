import { api, h, replace, fmt, tone, table, signInForm } from "/web/common.js";

const app = document.getElementById("app");
const signout = document.getElementById("signout");

const NAMES = { BTC: "Bitcoin", ETH: "Ether", SOL: "Solana", SUI: "Sui" };
const LEVERAGES = [[0, "Suggested"], [1, "None (1x)"], [2, "2x"], [3, "3x"], [5, "5x"],
                   [10, "10x"], [20, "20x"], [30, "30x"]];
const state = { week: null, board: null, boardError: null, plan: loadPlan() };

// Weekly amount and leverage: a per-browser convenience only.
function loadPlan() {
  try {
    const saved = JSON.parse(localStorage.getItem("neko-week-plan") || "null");
    if (saved && saved.stake > 0 && LEVERAGES.some(([v]) => v === saved.leverage)) return saved;
  } catch { /* storage unavailable */ }
  return { stake: 18, leverage: 0 };
}

function savePlan() {
  try {
    localStorage.setItem("neko-week-plan", JSON.stringify(state.plan));
  } catch { /* storage unavailable */ }
}

async function loadBoard() {
  state.boardError = null;
  try {
    state.board = await api(
      `/api/web/week/scoreboard?stake=${encodeURIComponent(state.plan.stake)}&leverage=${state.plan.leverage}`);
  } catch (err) {
    state.board = null;
    state.boardError = err.message;
  }
}

function name(coin) {
  return NAMES[coin] || coin;
}

function list(items) {
  return items.length > 1
    ? `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`
    : items[0] || "";
}

function signed(v, digits = 1) {
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

function share(v) {
  return v === null || v === undefined ? "–" : `${(v * 100).toFixed(0)}%`;
}

function longDate(iso) {
  const date = new Date(`${iso}T00:00:00Z`);
  return date.toLocaleDateString(undefined, { month: "long", day: "numeric", timeZone: "UTC" });
}

function callWords(call) {
  return `${call.side} ${name(call.coin)} at ${Number(call.leverage)}x`;
}

// ---- the answer ------------------------------------------------------------

function answerPanel() {
  const w = state.week;
  const out = w.coins.filter((c) => !c.side).map((c) => name(c.coin));
  let headline;
  if (!w.calls.length) headline = "This week: stay out.";
  else {
    headline = `This week: ${list(w.calls.map(callWords))}.`;
    if (out.length) headline += ` Stay out of ${list(out)}.`;
  }
  return h("section", { class: "panel stack" },
    h("h1", { style: "max-width:22ch" }, headline),
    h("p", { class: "lead" },
      w.calls.length
        ? "These are the trades the tested rule makes this week, on perps, at the leverage that almost never got liquidated in testing."
        : "The tested rule has no trade this week. Staying out is part of the plan."),
    h("p", { class: "soft small" },
      `This call is for the week starting Monday ${longDate(w.week_of)}. ` +
      `The next call comes on Monday ${longDate(w.next_call)}.`));
}

function previewPanel() {
  const w = state.week;
  const p = w.preview;
  if (!p || !p.coins.length) return null;
  const hoursLeft = (new Date(`${w.next_call}T00:00:00Z`) - Date.now()) / 3600000;
  if (hoursLeft > 48 || hoursLeft < 0) return null;       // only worth showing close to Monday
  const calls = p.coins.filter((c) => c.side);
  const out = p.coins.filter((c) => !c.side).map((c) => name(c.coin));
  let text = calls.length ? `${list(calls.map(callWords))}.` : "Stay out.";
  text = text.charAt(0).toUpperCase() + text.slice(1);
  if (calls.length && out.length) text += ` Stay out of ${list(out)}.`;
  return h("section", { class: "panel stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, `Next week, if nothing changes by Monday ${longDate(w.next_call)}`),
    h("p", { class: "verdict" }, text),
    h("p", { class: "soft small" },
      `A preview from today's mood (${p.mood.value} of 100, ${p.mood.label.toLowerCase()}) and current prices. ` +
      `The call becomes final at 00:00 UTC on Monday, with its entry and stop prices.`));
}

function whyPanel() {
  const w = state.week;
  const greedy = w.mood.value >= 50;
  const checks = [{
    passed: true,
    label: greedy ? "The market's mood is greedy, which favours longs"
                  : "The market's mood is fearful, which favours shorts",
    detail: `The Fear & Greed index was ${w.mood.value} of 100 (${w.mood.label.toLowerCase()}). 50 or more is greedy.`,
  }];
  for (const c of w.coins) {
    const moved = `${name(c.coin)} moved ${signed(c.four_week_change_pct)} over the last four weeks`;
    if (c.side) {
      checks.push({ passed: true, label: `${moved}, so the call is ${c.side}`,
        detail: `Going ${c.side} on ${name(c.coin)} ${recordWords(c)}` });
    } else if (c.skipped) {
      checks.push({ passed: false, label: `${moved}, but there is no call`,
        detail: `The rule points ${c.signal}, but that did not hold up in testing: going ${c.signal} on ${name(c.coin)} ${recordWords(c)}` });
    } else {
      checks.push({ passed: false, label: `${moved}, against the mood, so there is no call`,
        detail: "A call needs the mood and the coin's last four weeks to point the same way." });
    }
  }
  return h("section", { class: "panel stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "Why"),
    h("ul", { class: "gate" }, checks.map((check) =>
      h("li", { "data-passed": String(check.passed) },
        h("span", { class: "tick", "aria-hidden": "true" }, check.passed ? "✓" : ""),
        h("div", null, h("div", null, check.label), h("div", { class: "soft small" }, check.detail))))));
}

function recordWords(c) {
  const r = c.record;
  if (!r) return "";
  return `was right in ${share(r.first_half.right)} of weeks up to 2023 and ${share(r.second_half.right)} since.`;
}

function ticket(called, each, smallest) {
  const usd = (c) => each * c.leverage;
  return table([
    { label: "Trade", cell: (c) => `${c.side === "long" ? "Long" : "Short"} ${name(c.coin)}` },
    { label: "Leverage", num: true, cell: (c) => `${c.leverage}x` },
    { label: "Position", num: true,
      cell: (c) => `${fmt.usd(usd(c))}${usd(c) < smallest ? " (too small)" : ""}`,
      cls: (c) => (usd(c) < smallest ? "loss" : "") },
    { label: "Entry", num: true, cell: (c) => `about ${fmt.price(c.entry)}` },
    // each level carries what it costs or makes on a second line, so the table fits without scrolling
    { label: "Stop loss", num: true,
      cell: (c) => (c.stop_price
        ? h("div", null,
            h("div", null, `${fmt.price(c.stop_price)} (${c.stop_pct.toFixed(1)}% away)`),
            c.stake_lost_at_stop
              ? h("div", { class: "loss small" }, `loses ${fmt.usd(each * c.stake_lost_at_stop)}`) : null)
        : "–") },
    { label: "Take profit", num: true,
      cell: (c) => (c.target_price
        ? h("div", null,
            h("div", null, `${fmt.price(c.target_price)} (${c.target_pct.toFixed(1)}% away)`),
            c.stake_made_at_target
              ? h("div", { class: "gain small" }, `makes ${fmt.usd(each * c.stake_made_at_target)}`) : null)
        : "None: close next Monday") },
  ], called, "");
}

function stepsPanel() {
  const w = state.week;
  const each = state.plan.stake / w.coins.length;
  const called = w.coins.filter((c) => c.side);
  const ex = w.exits || {};
  let steps;
  if (called.length) {
    const idle = w.coins.length - called.length;
    steps = [
      ["Open the trade on Monday",
       `Use the table below. Enter at the market price, or with a limit order at the current price.` +
       `${idle ? ` Keep the other ${fmt.usd(each * idle)} in cash.` : ""}`],
      called.some((c) => c.target_price)
        ? ["Set the stop loss and the take profit, then leave them",
           "Place both at the prices shown as soon as the trade is open; a take profit only where the table " +
           "shows one. Do not move them, add to the trade or raise the leverage."]
        : ["Set the stop loss, then leave it",
           "Place the stop at the price shown as soon as the trade is open. Do not move it, add to the trade or raise the leverage."],
      ["Close it next Monday", "Close whatever is still open, then follow the new call."],
    ];
  } else {
    steps = [
      ["Open nothing", "Close any trade left from an earlier week."],
      ["Keep the money in cash", `Leave all ${fmt.usd(state.plan.stake)} in dollars or a dollar stablecoin.`],
      ["Check again next Monday", "Trade only when this page makes a call."],
    ];
  }
  const smallest = w.min_order_usd || 0;
  const small = called.filter((c) => each * c.leverage < smallest);
  const tp = ex.take_profit || {};
  const cents = (v) => (v * 100).toFixed(1);
  const takeProfitWords = [
    tp.short && tp.short.target
      ? `Shorts take profit one normal week from the entry. In testing that level was reached in ` +
        `${share(tp.short.reached)} of short weeks; shorts ended the week ahead ${share(tp.short.with.ahead)} of ` +
        `the time with it, against ${share(tp.short.stop_only.ahead)} without, and the average week was no ` +
        `lower (${cents(tp.short.with.avg)} against ${cents(tp.short.stop_only.avg)} cents per dollar). ` +
        `Falls tend to bounce, so banking them paid.`
      : null,
    tp.long && !tp.long.target && tp.long.at_one_normal_week
      ? `Longs have no take profit: a target one normal week away cut the average long week from ` +
        `${cents(tp.long.stop_only.avg)} to ${cents(tp.long.at_one_normal_week.avg)} cents per dollar, and none ` +
        `of the ${tp.long.plans_tested} take-profit plans tested was as good as the stop alone. ` +
        `Rises tend to keep going.`
      : null,
  ].filter(Boolean).join(" ");
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "What to do"),
    h("ol", { class: "steps" }, steps.map(([title, text]) =>
      h("li", null, h("h3", null, title), h("p", { class: "soft" }, text)))),
    called.length ? ticket(called, each, smallest) : null,
    small.length ? h("p", { class: "notice bad" },
      `Too small to place: ${list(small.map((c) =>
        `${name(c.coin)} at ${c.leverage}x would be a ${fmt.usd(each * c.leverage)} position`))}. ` +
      `The exchange's smallest order is ${fmt.usd(smallest)}. Use a weekly amount of at least ` +
      `${fmt.usd(Math.ceil(Math.max(...small.map((c) => c.min_weekly_amount))))}, or skip ` +
      `${small.length > 1 ? "those trades" : "that trade"}.`) : null,
    called.length ? h("p", { class: "soft small" },
      `The stop sits one normal week's move from the entry (${list(called.map((c) =>
        `${c.normal_week_pct.toFixed(1)}% for ${name(c.coin)}`))}). ` +
      (ex.with && ex.holding
        ? `In testing it cut the worst week from losing ${Math.abs(ex.holding.worst * 100).toFixed(0)}% of the ` +
          `weekly amount to ${Math.abs(ex.with.worst * 100).toFixed(0)}%, and lowered the average week from ` +
          `${(ex.holding.avg * 100).toFixed(1)} to ${(ex.with.avg * 100).toFixed(1)} cents per dollar. `
        : "") +
      `Tighter stops were hit too often by ordinary swings and did worse.`) : null,
    called.length ? h("p", { class: "soft small" },
      `${takeProfitWords} Waiting for a better entry price with a limit order missed too many winning weeks.`)
      : null);
}

// ---- week by week ----------------------------------------------------------

function boardPanel() {
  const stake = h("input", { type: "text", name: "stake", inputmode: "decimal",
                             value: String(state.plan.stake) });
  const leverage = h("select", { name: "leverage" }, LEVERAGES.map(([value, label]) =>
    h("option", { value, selected: value === state.plan.leverage }, label)));
  const error = h("p", { class: "loss small", role: "alert" });
  const button = h("button", { class: "button quiet", type: "submit" }, "Show my weeks");
  const form = h("form", { class: "inline-form", novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      const value = Number(stake.value);
      if (!(value > 0)) {
        error.textContent = "Enter the weekly amount in USD.";
        stake.focus();
        return;
      }
      state.plan = { stake: value, leverage: Number(leverage.value) };
      savePlan();
      button.disabled = true;
      await loadBoard();
      render();
    } },
    h("label", { class: "field" }, h("span", null, "Amount each week (USD)"), stake),
    h("label", { class: "field" }, h("span", null, "Leverage"), leverage),
    button);
  const head = [
    h("h2", { style: "font-size:var(--t-h3)" }, "Week by week"),
    h("p", { class: "soft" },
      "What a fresh weekly amount would have made in each finished week, following this page's calls and stop loss. The amount is split equally between the coins; a coin with no call stays in cash."),
    form, error,
  ];
  if (state.boardError) {
    return h("section", { class: "stack" }, ...head,
      h("p", { class: "notice bad" }, `The weeks could not be loaded. ${state.boardError}`));
  }
  const b = state.board;
  if (!b) return h("section", { class: "stack" }, ...head);
  const capped = Object.entries(b.capped || {});
  const risky = b.expect.filter((e) => e.liquidated >= 0.1);
  return h("section", { class: "stack" }, ...head,
    h("p", { class: "verdict" },
      `Last ${b.rows.length} weeks: ${b.ahead} ahead, ${b.behind} behind, ${b.no_trade} with no trade. ` +
      `Total ${fmt.signedUsd(b.total)}.`),
    table([
      { label: "Week of", cell: (r) => longDate(r.week_of) },
      { label: "Call", cell: (r) => {
          const small = r.too_small && r.too_small.length ? `too small to place: ${list(r.too_small)}` : "";
          if (!r.calls.length) return small ? `No trade (${small})` : "Stay out";
          return list(r.calls.map((c) => `${c.side === "long" ? "Long" : "Short"} ${c.coin} ${Number(c.leverage)}x`)) +
            (small ? ` (${small})` : "");
        } },
      { label: "Mood", num: true, cell: (r) => String(r.mood) },
      { label: "Result", num: true,
        cell: (r) => (r.calls.length
          ? `${fmt.signedUsd(r.result)}${r.wiped.length ? ` (${list(r.wiped)} liquidated)` : ""}` +
            `${r.stopped && r.stopped.length ? ` (${list(r.stopped)} stopped)` : ""}` +
            `${r.took_profit && r.took_profit.length ? ` (${list(r.took_profit)} took profit)` : ""}`
          : "No trade"),
        cls: (r) => (r.calls.length ? tone(r.result) : "") },
      { label: "Running total", num: true, cell: (r) => fmt.signedUsd(r.running_total),
        cls: (r) => tone(r.running_total) },
    ], b.rows, "No finished weeks to show yet."),
    capped.length ? h("p", { class: "soft small" },
      `${list(capped.map(([coin, max]) => `${name(coin)} allows at most ${max}x`))}, ` +
      `so that part is traded at its limit.`) : null,
    b.expect.length ? h("div", { class: "stack" },
      h("h3", null, "What to expect at this leverage"),
      table([
        { label: "Call", cell: (e) => `${e.side === "long" ? "Long" : "Short"} ${name(e.coin)} at ${e.leverage}x` },
        { label: "Weeks traded", num: true, cell: (e) => String(e.weeks) },
        { label: "Ended ahead", num: true, cell: (e) => share(e.ahead) },
        { label: "Liquidated", num: true, cell: (e) => share(e.liquidated),
          cls: (e) => (e.liquidated >= 0.1 ? "loss" : "") },
        { label: "Average week, per 1 USD put in", num: true, cell: (e) => fmt.signedUsd(e.avg_per_week),
          cls: (e) => tone(e.avg_per_week) },
      ], b.expect, "")) : null,
    risky.length ? h("p", { class: "notice bad" },
      `At this leverage ${list(risky.map((e) => `${e.side} ${name(e.coin)}`))} lost the whole weekly share in ` +
      `${list(risky.map((e) => share(e.liquidated)))} of the weeks traded. A small move against the trade ` +
      `during the week closes it, even when the week ends in its favour.`) : null,
    h("p", { class: "soft small" },
      "Six years of prices, with exchange fees and a typical funding cost on longs. No rule tested made a profit every week: roughly half the traded weeks end ahead, and the gains come from the winning weeks being larger than the losing ones."));
}

// ---- record and lessons ----------------------------------------------------

function recordPanel() {
  const w = state.week;
  const rec = w.spot_record;
  const rows = w.coins.filter((c) => c.record);
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "How far to trust it"),
    h("p", { class: "notice" },
      "These calls come from six years of past prices. They have been right a little more than half the time, there will be losing weeks in a row, and past results do not promise future ones. Only use money you can afford to lose."),
    rec ? h("p", { class: "soft small" },
      `Without leverage or shorting, holding Bitcoin and Ether only in "long" weeks turned 1 USD into ` +
      `${fmt.usd(rec.rule.all.grew_to)} since ${rec.from.slice(0, 4)}, against ${fmt.usd(rec.hold.all.grew_to)} ` +
      `for holding all the time, with a worst fall of ${Math.abs(rec.rule.all.worst_fall_pct).toFixed(0)}% ` +
      `against ${Math.abs(rec.hold.all.worst_fall_pct).toFixed(0)}%.`) : null,
    rows.length ? table([
      { label: "This week's direction", cell: (c) => `${c.signal === "long" ? "Long" : "Short"} ${name(c.coin)}` },
      { label: "Weeks right, to 2023", num: true, cell: (c) => share(c.record.first_half.right) },
      { label: "Average week", num: true, cell: (c) => signed(c.record.first_half.avg_pct, 2),
        cls: (c) => tone(c.record.first_half.avg_pct) },
      { label: "Weeks right, since 2023", num: true, cell: (c) => share(c.record.second_half.right) },
      { label: "Average week", num: true, cell: (c) => signed(c.record.second_half.avg_pct, 2),
        cls: (c) => tone(c.record.second_half.avg_pct) },
      { label: "Called", cell: (c) => (c.side ? "Yes" : "No, skipped") },
    ], rows, "") : null);
}

function lessonsPanel() {
  const lessons = [
    ["High leverage", "At 30x the weekly amount was liquidated in about 70% of the weeks traded, and the total was a loss."],
    ["Shorting Bitcoin", "It was right in 48% of weeks since 2023 and made nothing, so the platform skips it."],
    ["Buying because everyone is afraid", "Buying at extreme fear and selling at extreme greed was the worst rule tested."],
    ["Guessing a forex direction", "23 rules were tested on 27 years of rates. None beat a coin flip."],
    ["Fast in-and-out trading", "With a 0.5% fee on each fill the trades needed to win 52% of the time and won 35%."],
  ];
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "What lost money in testing"),
    h("ul", { class: "gate" }, lessons.map(([title, text]) =>
      h("li", { "data-passed": "false" },
        h("span", { class: "tick", "aria-hidden": "true" }, ""),
        h("div", null, h("div", null, title), h("div", { class: "soft small" }, text))))));
}

function render() {
  signout.hidden = false;
  replace(app, h("div", { class: "week" },
    answerPanel(), previewPanel(), whyPanel(), stepsPanel(), boardPanel(), recordPanel(), lessonsPanel()));
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
    state.week = await api("/api/web/week");
  } catch (err) {
    if (err.status === 401) {
      renderSignIn([]);
      return;
    }
    replace(app, h("p", { class: "notice bad" },
      `This week's calls could not be worked out. ${err.message}`));
    return;
  }
  await loadBoard();
  render();
}

start();
