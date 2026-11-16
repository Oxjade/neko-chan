import {
  api, h, replace, fmt, tone, gauge, gaugeFromPerformance, performanceFigures,
  gateList, equityChart, table, directionWord,
} from "/web/common.js";

const tradeColumns = [
  { label: "Closed", cell: (t) => fmt.when(t.closed_at) },
  { label: "Market", cell: (t) => t.symbol },
  { label: "Side", cell: (t) => directionWord(t.direction) },
  { label: "Entry", num: true, cell: (t) => fmt.price(t.entry) },
  { label: "Exit", num: true, cell: (t) => fmt.price(t.exit) },
  { label: "Leverage", num: true, cell: (t) => `${t.leverage}x` },
  { label: "Result after fees", num: true, cell: (t) => fmt.signedPct(t.net_pct),
    cls: (t) => tone(t.net_pct) },
];

function renderRecord(data) {
  const perf = data.performance;
  if (!data.bots) {
    replace(document.getElementById("hero-gauge"),
      h("div", { class: "stack" },
        h("p", { class: "verdict" }, "No record is published yet."),
        h("p", { class: "soft" },
          "The paper bots are running but none has been made public. Results appear here once the operator publishes one.")));
    replace(document.getElementById("record-body"),
      h("p", { class: "empty" }, "Nothing to show until a paper bot is published."));
  } else {
    replace(document.getElementById("hero-gauge"), gaugeFromPerformance(perf));
    replace(document.getElementById("record-body"),
      performanceFigures(perf),
      h("div", { class: "panel" }, equityChart(perf.curve, perf.starting)),
      h("h3", null, "Latest closed trades"),
      table(tradeColumns, data.trades, "No trades have closed yet."));
  }
  replace(document.getElementById("gate-body"), gateList(data.gate));
}

function renderReplay(replay) {
  if (!replay || !replay.trades) return;
  const section = document.getElementById("replay");
  const costs = replay.by_cost || {};
  const names = {
    gross: "No costs",
    venue_only: "Exchange fees only",
    with_platform_fee: "Exchange fees plus the 0.5% platform fee",
  };
  const rows = Object.entries(names).filter(([key]) => costs[key]).map(([key, label]) => ({
    label, ...costs[key],
  }));
  const from = (replay.data?.from || "").slice(0, 10);
  const to = (replay.data?.to || "").slice(0, 10);
  const base = replay.random_baseline;
  replace(document.getElementById("replay-body"),
    h("p", null,
      `The same decision engine, run over ${(replay.data?.symbols || []).join(" and ")} ` +
      `five-minute prices from ${from} to ${to}. It took ${replay.trades} trades. ` +
      `That is far too few to prove an edge, and it is shown here so the gap is visible.`),
    h("div", { class: "panel" }, gauge({
      trades: replay.trades, wins: replay.wins, winRate: replay.win_rate,
      low: replay.win_rate_low, high: replay.win_rate_high,
      breakeven: costs.with_platform_fee?.breakeven_win_rate ?? null,
    })),
    table([
      { label: "Costs charged", cell: (r) => r.label },
      { label: "Cost per round trip", num: true, cell: (r) => `${r.round_trip_cost_pct.toFixed(2)}%` },
      { label: "Win rate needed", num: true, cell: (r) => fmt.pct(r.breakeven_win_rate) },
      { label: "Average trade", num: true, cell: (r) => fmt.signedPct(r.avg_net_pct_of_notional),
        cls: (r) => tone(r.avg_net_pct_of_notional) },
    ], rows, ""),
    base && base.mean_win_rate != null
      ? h("p", { class: "soft" },
          `For comparison, entering at the same moments in a random direction wins ` +
          `${fmt.pct(base.mean_win_rate)} of the time on average, and between ` +
          `${fmt.pct(base.p05)} and ${fmt.pct(base.p95)} in nine runs out of ten.`)
      : null);
  section.hidden = false;
}

async function load() {
  try {
    const data = await api("/api/web/public/track-record");
    renderRecord(data);
    renderReplay(data.replay);
  } catch (error) {
    const message = h("p", { class: "notice bad" }, `The record could not be loaded. ${error.message}`);
    replace(document.getElementById("hero-gauge"), message);
    replace(document.getElementById("record-body"));
    replace(document.getElementById("gate-body"),
      h("p", { class: "soft" }, "Reload the page to try again."));
  }
}

const form = document.getElementById("waitlist");
const note = document.getElementById("waitlist-note");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  const email = form.elements.email.value.trim();
  note.className = "small";
  if (!email) {
    note.textContent = "Enter your email address.";
    note.classList.add("loss");
    form.elements.email.focus();
    return;
  }
  button.disabled = true;
  try {
    await api("/api/web/public/waitlist", { email });
    note.textContent = "You are on the list.";
    note.classList.add("gain");
    form.reset();
  } catch (error) {
    note.textContent = error.message;
    note.classList.add("loss");
  } finally {
    button.disabled = false;
  }
});

load();
