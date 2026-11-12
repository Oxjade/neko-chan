import {
  api, h, replace, fmt, tone, gaugeFromPerformance, performanceFigures,
  gateList, equityChart, table, figures, directionWord,
} from "/web/common.js";

const app = document.getElementById("app");
const signout = document.getElementById("signout");

const state = { bots: [], aiKey: null, selected: null, detail: null, managesAgents: true,
                setupMissing: [], waitlist: 0, timer: null, flash: null };

const MARKETS = ["BTC", "ETH", "SOL", "SUI", "HYPE"];
const STYLES = [["scalp", "Scalp (minutes to hours)"], ["intraday", "Intraday (hours)"],
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
  const style = select(STYLES, "scalp", "style");
  const risk = select(RISKS, "balanced", "risk");
  const interval = select(INTERVALS, 120, "interval");
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
      button.disabled = true;
      try {
        const bot = await api("/api/web/console/bots", {
          name: name.value.trim(), watchlist, trader_type: style.value,
          risk_profile: risk.value, interval_sec: Number(interval.value) });
        state.selected = bot.id;
        state.flash = { bad: false, text: "Paper bot created. It starts paused." };
        await refresh();
      } catch (err) {
        error.textContent = err.message;
        button.disabled = false;
      }
    } },
    h("h2", { style: "font-size:var(--t-h3)" }, "New paper bot"),
    h("p", { class: "soft small" }, "Starts with 1,000 USD of simulated money."),
    h("label", { class: "field" }, h("span", null, "Name"), name),
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
  const { bot, report, decisions } = state.detail;
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
