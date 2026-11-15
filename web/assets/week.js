import { api, h, replace, fmt, table, signInForm } from "/web/common.js";

const app = document.getElementById("app");
const signout = document.getElementById("signout");

const NAMES = { BTC: "Bitcoin", ETH: "Ether" };
const state = { week: null, money: loadMoney() };

// Shared with the desk and the weekly plan: a per-browser convenience only.
function loadMoney() {
  try {
    const saved = JSON.parse(localStorage.getItem("neko-desk-sizing") || "null");
    if (saved && saved.account > 0) return saved.account;
  } catch { /* storage unavailable */ }
  return 100;
}

function saveMoney() {
  try {
    const saved = JSON.parse(localStorage.getItem("neko-desk-sizing") || "null") || { risk: 1 };
    localStorage.setItem("neko-desk-sizing", JSON.stringify({ ...saved, account: state.money }));
  } catch { /* storage unavailable */ }
}

function list(names) {
  return names.length > 1
    ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`
    : names[0] || "";
}

function signed(v, digits = 1) {
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

function longDate(iso) {
  const date = new Date(`${iso}T00:00:00Z`);
  return date.toLocaleDateString(undefined, { month: "long", day: "numeric", timeZone: "UTC" });
}

// ---- the answer ------------------------------------------------------------

function answerPanel() {
  const w = state.week;
  const holding = w.hold.map((c) => NAMES[c] || c);
  return h("section", { class: "panel stack" },
    h("h1", null, w.answer === "hold" ? `This week: hold ${list(holding)}.` : "This week: stay in cash."),
    h("p", { class: "lead" },
      w.answer === "hold"
        ? "Both checks pass for the coins named. Own them this week, with no leverage."
        : "The checks below do not both pass, so the tested rule holds no coins this week."),
    h("p", { class: "soft small" },
      `Decided from the week that ended ${longDate(w.mood.date)}. The answer changes at most once a week, on Monday.`));
}

function whyPanel() {
  const w = state.week;
  const moodOk = w.mood.value >= 50;
  const checks = [{
    passed: moodOk, label: "The market's mood is greedy",
    detail: `The Fear & Greed index was ${w.mood.value} of 100 (${w.mood.label.toLowerCase()}). It needs to be 50 or more.`,
  }, ...w.coins.map((c) => ({
    passed: c.rose, label: `${NAMES[c.coin] || c.coin} rose over the last four weeks`,
    detail: `It moved ${signed(c.four_week_change_pct)}.`,
  }))];
  return h("section", { class: "panel stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "Why"),
    h("ul", { class: "gate" }, checks.map((check) =>
      h("li", { "data-passed": String(check.passed) },
        h("span", { class: "tick", "aria-hidden": "true" }, check.passed ? "✓" : ""),
        h("div", null,
          h("div", null, check.label, h("span", { class: "soft" }, check.passed ? " (yes)" : " (no)")),
          h("div", { class: "soft small" }, check.detail))))),
    h("p", { class: "soft small" },
      "A coin is held only when the mood check and that coin's own check both pass."));
}

function stepsPanel() {
  const w = state.week;
  const each = state.money / w.coins.length;
  const parts = w.coins.map((c) =>
    `${fmt.usd(each)} in ${c.hold ? (NAMES[c.coin] || c.coin) : "cash"}`);
  const steps = w.answer === "hold" ? [
    ["Buy on a spot exchange",
     `Buy ${list(w.hold.map((c) => NAMES[c] || c))} so that you own the coins. Do not use futures, margin or leverage.`],
    ["Split the money equally", `With ${fmt.usd(state.money)}: ${list(parts)}.`],
    ["Check again next Monday", "Follow the new answer. Sell any coin the page no longer says to hold."],
  ] : [
    ["Hold no coins", "If you bought Bitcoin or Ether in an earlier week, sell them."],
    ["Keep the money in cash", `Leave all ${fmt.usd(state.money)} in dollars or a dollar stablecoin.`],
    ["Check again next Monday", "Buy only when this page says to hold."],
  ];
  const money = h("input", { type: "text", name: "money", inputmode: "decimal",
                             value: String(state.money) });
  const error = h("p", { class: "loss small", role: "alert" });
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "What to do"),
    h("ol", { class: "steps" }, steps.map(([title, text]) =>
      h("li", null, h("h3", null, title), h("p", { class: "soft" }, text)))),
    h("form", { class: "inline-form", novalidate: true,
      onsubmit: (event) => {
        event.preventDefault();
        error.textContent = "";
        const value = Number(money.value);
        if (!(value > 0)) {
          error.textContent = "Enter the amount in USD.";
          money.focus();
          return;
        }
        state.money = value;
        saveMoney();
        render();
      } },
      h("label", { class: "field" }, h("span", null, "Your money for this plan (USD)"), money),
      h("button", { class: "button quiet", type: "submit" }, "Update amounts")),
    error);
}

function recordPanel() {
  const rec = state.week.record;
  if (!rec) return null;
  const rows = [{ name: "Following this rule", ...rec.rule }, { name: "Always holding", ...rec.hold }];
  const year = (iso) => iso.slice(0, 4);
  return h("section", { class: "stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "How this rule has done"),
    table([
      { label: `Bitcoin and Ether, ${year(rec.from)} to ${year(rec.to)}`, cell: (r) => r.name },
      { label: "1 USD became", num: true, cell: (r) => fmt.usd(r.all.grew_to) },
      { label: "Worst fall on the way", num: true, cell: (r) => `${r.all.worst_fall_pct.toFixed(0)}%`,
        cls: () => "loss" },
      { label: "Worst single week", num: true, cell: (r) => `${r.all.worst_week_pct.toFixed(0)}%`,
        cls: () => "loss" },
      { label: "Weeks in the market", num: true, cell: (r) => `${(r.all.time_invested * 100).toFixed(0)}%` },
    ], rows, ""),
    h("p", { class: "soft small" },
      `It held up in each half: 1 USD became ${fmt.usd(rec.rule.first_half.grew_to)} against ` +
      `${fmt.usd(rec.hold.first_half.grew_to)} up to ${year(rec.split)}, and ` +
      `${fmt.usd(rec.rule.second_half.grew_to)} against ${fmt.usd(rec.hold.second_half.grew_to)} since. ` +
      `The answer changed about ${Math.round(rec.rule.all.switches_per_year)} times a year for each coin, ` +
      `and ${(rec.rule.all.weeks_up_when_in * 100).toFixed(0)}% of the weeks it was in were up.`),
    h("p", { class: "notice" },
      "This is six years of one market, tested on past prices. Following the rule still meant a fall of " +
      `${Math.abs(rec.rule.all.worst_fall_pct).toFixed(0)}% at one point, and long stretches in cash. ` +
      "Past results do not promise future ones. Only use money you can afford to lose."));
}

function lessonsPanel() {
  const lessons = [
    ["Leverage to get rich in a week", "At 20x, aiming to triple the money lost everything in 56% of the weeks tested."],
    ["Buying because everyone is afraid", "Buying at extreme fear and selling at extreme greed was right in only 41 to 45% of weeks, the worst rule tested."],
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
    answerPanel(), whyPanel(), stepsPanel(), recordPanel(), lessonsPanel()));
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
      `This week's answer could not be worked out. ${err.message}`));
    return;
  }
  render();
}

start();
