import {
  api, h, replace, fmt, tone, gaugeFromPerformance, performanceFigures,
  gateList, equityChart, table, figures, directionWord,
} from "/web/common.js";

const app = document.getElementById("app");
const signout = document.getElementById("signout");

const state = { bots: [], aiKey: null, selected: null, detail: null, managesAgents: true,
                setupMissing: [], waitlist: 0, timer: null, flash: null, email: null };

const MARKETS = ["BTC", "ETH", "SOL", "SUI", "HYPE"];
const STYLES = [["trend", "Slow trend (tested rule, no AI key needed)"],
                ["scalp", "Scalp (minutes to hours)"], ["intraday", "Intraday (hours)"],
                ["auto", "Both"]];
const RISKS = [["conservative", "Conservative"], ["balanced", "Balanced"],
               ["aggressive", "Aggressive"]];
const INTERVALS = [[60, "Every minute"], [120, "Every 2 minutes"], [300, "Every 5 minutes"],
                   [600, "Every 10 minutes"]];
const PROVIDERS = [["openai", "OpenAI"], ["claude", "Anthropic"], ["openrouter", "OpenRouter"],
                   ["deepseek", "DeepSeek"]];

const SETUP_HELP = {
  WEB_OPERATOR_PASSWORD: "the password for this console",
  TG_VAULT_MASTER_KEY: "the key that encrypts stored secrets, needed to create bots",
};

function setupNotice(missing) {
  if (!missing.length) return null;
  return h("div", { class: "notice stack" },
    h("p", null, "Finish the server setup. Set these environment variables and restart:"),
    h("ul", null, missing.map((name) =>
      h("li", null, h("code", null, name), `: ${SETUP_HELP[name] || ""}`))));
}

// ---- sign in ---------------------------------------------------------------

function renderSignIn(missing) {
  signout.hidden = true;
  const error = h("p", { class: "loss small", role: "alert" });
  const password = h("input", { type: "password", name: "password",
                                autocomplete: "current-password", required: true });
  const button = h("button", { class: "button", type: "submit" }, "Sign in");
  const off = missing.includes("WEB_OPERATOR_PASSWORD");
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
        await start();
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
  replace(app, h("div", { class: "signin" }, form));
  if (!off) password.focus();
}

// ---- data ------------------------------------------------------------------

async function loadBots() {
  const data = await api("/api/web/console/bots");
  state.bots = data.bots;
  state.aiKey = data.ai_key;
  state.managesAgents = data.manages_agents;
  state.setupMissing = data.setup_missing;
  state.email = data.email;
  if (!state.bots.some((b) => b.id === state.selected)) {
    state.selected = state.bots.length ? state.bots[0].id : null;
  }
}

async function loadDetail() {
  state.detail = state.selected === null
    ? null
    : await api(`/api/web/console/bots/${state.selected}`);
}

async function refresh() {
  try {
    await loadBots();
    await loadDetail();
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
    await api(path, body ?? {});
    if (done) state.flash = { bad: false, text: done };
  } catch (err) {
    state.flash = { bad: true, text: err.message };
  }
  await refresh();
}

// ---- left column -----------------------------------------------------------

function select(options, value, name) {
  return h("select", { name }, options.map(([v, label]) =>
    h("option", { value: v, selected: String(v) === String(value) }, label)));
}

function botList() {
  if (!state.bots.length) {
    return h("p", { class: "empty" }, "No bots yet. Create a paper bot below.");
  }
  return h("ul", { class: "botlist" }, state.bots.map((bot) =>
    h("li", null, h("button", {
      type: "button", "aria-current": String(bot.id === state.selected),
      onclick: async () => { state.selected = bot.id; state.flash = null; await refresh(); },
    }, h("span", null, bot.name), stateBadge(bot)))));
}

function stateBadge(bot) {
  if (bot.mode !== "paper") return h("span", { class: "badge", "data-state": "live" }, "Live");
  const words = { running: "Running", paused: "Paused", stopped: "Stopped" };
  return h("span", { class: "badge", "data-state": bot.state }, words[bot.state] || bot.state);
}

function newBotForm() {
  const error = h("p", { class: "loss small", role: "alert" });
  const name = h("input", { type: "text", name: "name", maxlength: "40", required: true });
  const boxes = MARKETS.map((m) => h("input", {
    type: "checkbox", name: "market", value: m, checked: m === "BTC" || m === "ETH" }));
  const style = select(STYLES, "trend", "style");
  const risk = select(RISKS, "balanced", "risk");
  const interval = select(INTERVALS, 120, "interval");
  const startUsd = h("input", { type: "text", name: "start_usd", inputmode: "decimal", value: "10" });
  const goalUsd = h("input", { type: "text", name: "goal_usd", inputmode: "decimal" });
  const goalDays = h("input", { type: "text", name: "goal_days", inputmode: "numeric" });
  const button = h("button", { class: "button", type: "submit" }, "Create paper bot");
  return h("form", { class: "panel stack", novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      const watchlist = boxes.filter((b) => b.checked).map((b) => b.value);
      if (!name.value.trim()) {
        error.textContent = "Give the bot a name.";
        name.focus();
        return;
      }
      if (!watchlist.length) {
        error.textContent = "Choose at least one market.";
        return;
      }
      const start = Number(startUsd.value);
      if (!(start >= 1)) {
        error.textContent = "Enter a starting amount of at least 1 USD.";
        startUsd.focus();
        return;
      }
      const payload = { name: name.value.trim(), watchlist, trader_type: style.value,
        risk_profile: risk.value, interval_sec: Number(interval.value), start_usd: start };
      if (goalUsd.value.trim() || goalDays.value.trim()) {
        const target = Number(goalUsd.value);
        const days = Number(goalDays.value);
        if (!(target > start)) {
          error.textContent = "The goal must be more than the starting amount.";
          goalUsd.focus();
          return;
        }
        if (!Number.isInteger(days) || days < 1) {
          error.textContent = "Enter the number of days as a whole number.";
          goalDays.focus();
          return;
        }
        payload.goal_usd = target;
        payload.goal_days = days;
      }
      button.disabled = true;
      try {
        const bot = await api("/api/web/console/bots", payload);
        state.selected = bot.id;
        state.flash = { bad: false, text: "Paper bot created. It starts paused." };
        await refresh();
      } catch (err) {
        error.textContent = err.message;
        button.disabled = false;
      }
    } },
    h("h2", { style: "font-size:var(--t-h3)" }, "New paper bot"),
    h("p", { class: "soft small" }, "Trades simulated money at real prices."),
    h("label", { class: "field" }, h("span", null, "Name"), name),
    h("label", { class: "field" }, h("span", null, "Starting amount (USD)"), startUsd),
    h("label", { class: "field" }, h("span", null, "Goal amount (USD, optional)"), goalUsd),
    h("label", { class: "field" }, h("span", null, "Days to reach the goal"), goalDays),
    h("fieldset", { class: "checks" }, h("legend", null, "Markets"),
      MARKETS.map((m, i) => h("label", null, boxes[i], m))),
    h("label", { class: "field" }, h("span", null, "Trading style"), style),
    h("label", { class: "field" }, h("span", null, "Risk profile"), risk),
    h("label", { class: "field" }, h("span", null, "Decision interval"), interval),
    error, button);
}

function aiKeyForm() {
  const error = h("p", { class: "loss small", role: "alert" });
  const provider = select(PROVIDERS, state.aiKey?.provider || "openai", "provider");
  const key = h("input", { type: "password", name: "api_key", autocomplete: "off",
                           required: true });
  const button = h("button", { class: "button quiet", type: "submit" }, "Save key");
  return h("form", { class: "panel stack", novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      if (key.value.trim().length < 8) {
        error.textContent = "Paste the full API key.";
        key.focus();
        return;
      }
      button.disabled = true;
      try {
        await api("/api/web/console/ai-key", { provider: provider.value, api_key: key.value.trim() });
        state.flash = { bad: false, text: "Key saved. Restart running bots to use it." };
        await refresh();
      } catch (err) {
        error.textContent = err.message;
        button.disabled = false;
      }
    } },
    h("h2", { style: "font-size:var(--t-h3)" }, "AI model key"),
    h("p", { class: "soft small" }, state.aiKey
      ? `Saved: ${state.aiKey.provider}, model ${state.aiKey.model || "default"}. The key is encrypted and never shown again.`
      : "Without a key the agent holds and opens no trades."),
    h("label", { class: "field" }, h("span", null, "Provider"), provider),
    h("label", { class: "field" }, h("span", null, "API key"), key),
    error, button);
}

function emailPanel() {
  const email = state.email || { missing: [], to: "", hour_utc: 8 };
  if (email.missing.length) {
    return h("section", { class: "panel stack" },
      h("h2", { style: "font-size:var(--t-h3)" }, "Daily email"),
      h("p", { class: "soft small" },
        "Off. To get a progress email every day, set these on the server and restart:"),
      h("ul", { class: "small" }, email.missing.map((name) => h("li", null, h("code", null, name)))));
  }
  return h("section", { class: "panel stack" },
    h("h2", { style: "font-size:var(--t-h3)" }, "Daily email"),
    h("p", { class: "soft small" },
      `A progress report goes to ${email.to} every day at ${String(email.hour_utc).padStart(2, "0")}:00 UTC.`),
    h("button", { class: "button quiet", type: "button",
      onclick: () => act("/api/web/console/report/send", {}, `Report sent to ${email.to}.`) },
      "Send report now"));
}

const GOAL_STATUS = {
  reached: "Goal reached.",
  missed: "The deadline passed before the goal was reached.",
  "wiped out": "The account is empty.",
  "on pace": "On pace so far.",
  behind: "Behind the pace the goal needs.",
};

function goalPanel(goal) {
  if (!goal) return null;
  return h("section", { class: "panel stack" },
    h("h2", { style: "font-size:var(--t-h3)" },
      `Goal: ${fmt.usd(goal.target_usd)} by ${fmt.when(goal.deadline).split(",").slice(0, 1).join("")}`),
    h("p", { class: "verdict" }, GOAL_STATUS[goal.status] || goal.status),
    figures([
      ["Account now", fmt.usd(goal.equity)],
      ["Started at", fmt.usd(goal.starting)],
      ["Of the way there", fmt.pct(goal.progress, 0), tone(goal.progress)],
      ["Days left", goal.days_left.toFixed(0)],
      ["Growth still needed per day", goal.needed_daily_pct === null ? "–" : `${goal.needed_daily_pct.toFixed(1)}%`],
    ]),
    goal.needed_daily_pct !== null && goal.needed_daily_pct > 2
      ? h("p", { class: "notice" },
          `Reaching this needs ${goal.needed_daily_pct.toFixed(1)}% growth every single day. ` +
          `Professional funds aim for that much in a month or more, so treat this goal as a long shot.`)
      : null);
}

// ---- right column ----------------------------------------------------------

const positionColumns = [
  { label: "Market", cell: (p) => p.symbol },
  { label: "Side", cell: (p) => directionWord(p.direction) },
  { label: "Entry", num: true, cell: (p) => fmt.price(p.entry) },
  { label: "Last price", num: true, cell: (p) => fmt.price(p.mark) },
  { label: "Stop", num: true, cell: (p) => fmt.price(p.stop) },
  { label: "Target", num: true, cell: (p) => fmt.price(p.target) },
  { label: "Leverage", num: true, cell: (p) => `${p.leverage}x` },
  { label: "Unrealised", num: true, cell: (p) => fmt.signedUsd(p.unrealized),
    cls: (p) => tone(p.unrealized) },
];

const tradeColumns = [
  { label: "Closed", cell: (t) => fmt.when(t.closed_at) },
  { label: "Market", cell: (t) => t.symbol },
  { label: "Side", cell: (t) => directionWord(t.direction) },
  { label: "Entry", num: true, cell: (t) => fmt.price(t.entry) },
  { label: "Exit", num: true, cell: (t) => fmt.price(t.exit) },
  { label: "Size", num: true, cell: (t) => fmt.usd(t.notional) },
  { label: "Leverage", num: true, cell: (t) => `${t.leverage}x` },
  { label: "Before fees", num: true, cell: (t) => fmt.signedUsd(t.gross), cls: (t) => tone(t.gross) },
  { label: "Fees", num: true, cell: (t) => fmt.usd(t.fees) },
  { label: "After fees", num: true, cell: (t) => fmt.signedUsd(t.net), cls: (t) => tone(t.net) },
];

function decisionList(decisions) {
  if (!decisions.length) {
    return h("p", { class: "empty" },
      "No decisions logged yet. They appear here once the bot is running.");
  }
  return h("ul", { class: "decisions" }, decisions.map((d) => {
    const acted = d.action && d.action !== "hold";
    const head = acted
      ? `${directionWord(d.direction)} ${d.symbol} at ${fmt.price(d.price)}`
      : "Held";
    return h("li", null,
      h("div", null, h("strong", null, head),
        h("span", { class: "soft" }, `  ${fmt.when(d.ts)}`),
        acted ? h("span", { class: "soft" }, d.filled ? "  filled" : "  not filled") : null),
      d.reasoning ? h("div", { class: "why" }, d.reasoning) : null,
      d.error ? h("div", { class: "why loss" }, d.error) : null);
  }));
}

function botView() {
  if (!state.detail) {
    return h("div", { class: "panel stack" },
      h("h1", { style: "font-size:var(--t-h2)" }, "No bot selected"),
      h("p", { class: "soft" }, "Create a paper bot to start building a record."));
  }
  const { bot, report, decisions, goal } = state.detail;
  const running = bot.state === "running";
  const head = h("div", { class: "bothead" },
    h("div", null,
      h("h1", { style: "font-size:var(--t-h2)" }, bot.name),
      h("p", { class: "soft small" },
        `${bot.watchlist.join(", ") || "Default markets"}, ${bot.trader_type}, ${bot.risk_profile} risk`)),
    h("div", { class: "actions" },
      stateBadge(bot),
      bot.mode === "paper" ? h("div", { class: "checks" },
        h("label", null,
          h("input", { type: "checkbox", checked: bot.is_public,
            onchange: (event) => act(`/api/web/console/bots/${bot.id}/public`,
              { is_public: event.target.checked },
              event.target.checked ? "Shown on the public page." : "Hidden from the public page.") }),
          "Show on public page")) : null,
      bot.mode === "paper" ? h("button", {
        class: running ? "button quiet" : "button", type: "button",
        onclick: () => act(`/api/web/console/bots/${bot.id}/${running ? "pause" : "start"}`, {},
          running ? "Paused." : "Started."),
      }, running ? "Pause" : "Start") : null));

  if (!report) {
    return h("div", { class: "stack" }, head,
      h("p", { class: "notice" },
        "This bot trades with real money. The web console shows and controls paper bots only, so its account is not displayed here."));
  }
  const acct = report.account;
  return h("div", { class: "stack" },
    head,
    bot.last_error ? h("p", { class: "notice bad" }, `Last error: ${bot.last_error}`) : null,
    !state.managesAgents ? h("p", { class: "notice" },
      "Agent processes are run by another supervisor, so Start and Pause only change the bot's flag.") : null,
    goalPanel(goal),
    h("section", { class: "panel" }, gaugeFromPerformance(report.performance)),
    h("section", { class: "panel stack" },
      h("h2", { style: "font-size:var(--t-h3)" },
        report.gate.passed ? "Ready for live trading: all checks pass" : "Not ready for live trading"),
      gateList(report.gate)),
    h("section", { class: "stack" },
      h("h2", { style: "font-size:var(--t-h3)" }, "Account"),
      figures([
        ["Equity", fmt.usd(acct.equity)],
        ["Cash", fmt.usd(acct.cash)],
        ["Margin in use", fmt.usd(acct.margin_in_use)],
        ["Unrealised", fmt.signedUsd(acct.unrealized), tone(acct.unrealized)],
      ])),
    h("section", { class: "stack" },
      h("h2", { style: "font-size:var(--t-h3)" }, "Open positions"),
      table(positionColumns, report.positions, "No open positions.")),
    h("section", { class: "stack" },
      h("h2", { style: "font-size:var(--t-h3)" }, "Performance after fees"),
      performanceFigures(report.performance),
      h("div", { class: "panel" },
        equityChart(report.performance.curve, report.performance.starting))),
    h("section", { class: "stack" },
      h("h2", { style: "font-size:var(--t-h3)" }, "Closed trades"),
      table(tradeColumns, report.trades, "No trades have closed yet.")),
    h("section", { class: "stack" },
      h("h2", { style: "font-size:var(--t-h3)" }, "Recent decisions"),
      h("div", { class: "panel" }, decisionList(decisions))));
}

function render() {
  signout.hidden = false;
  const active = document.activeElement;
  if (active && app.contains(active) && ["INPUT", "SELECT"].includes(active.tagName)
      && active.type !== "checkbox") {
    return; // do not rebuild the page under someone typing
  }
  replace(app,
    setupNotice(state.setupMissing),
    state.flash
      ? h("p", { class: state.flash.bad ? "notice bad" : "notice", role: "status" }, state.flash.text)
      : null,
    h("div", { class: "console", style: "margin-top:var(--s5)" },
      h("aside", null,
        h("section", { class: "panel stack" },
          h("h2", { style: "font-size:var(--t-h3)" }, "Bots"), botList()),
        newBotForm(),
        aiKeyForm(),
        emailPanel(),
        h("p", { class: "soft small" },
          `${state.waitlist} ${state.waitlist === 1 ? "person" : "people"} on the notify list.`)),
      h("main", null, botView())));
}

// ---- start -----------------------------------------------------------------

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
    state.waitlist = (await api("/api/web/console/waitlist")).total;
  } catch {
    state.waitlist = 0;
  }
  await refresh();
  stopPolling();
  state.timer = setInterval(refresh, 15000);
}

start();
