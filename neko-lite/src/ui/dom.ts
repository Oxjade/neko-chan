// Building the page: elements, formatting, tables and the win-rate gauge shared by the screens.
// Every value reaches the page through textContent, never innerHTML.

export type Child = Node | string | number | boolean | null | undefined | Child[];
export type Attrs = Record<string, string | number | boolean | null | undefined | ((event: Event) => unknown)> | null;

/** Create an element. Attributes that are null, undefined or false are left off; "on..." keys are listeners. */
export function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs?: Attrs, ...children: Child[]): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class") node.className = String(value);
    else if (typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of (children as unknown[]).flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function replace(target: Element, ...children: Child[]): void {
  const nodes = (children as unknown[]).flat(Infinity).filter(Boolean) as Array<Node | string>;
  target.replaceChildren(...nodes.map((child) => (child instanceof Node ? child : String(child))));
}

const usd = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2,
});

type Maybe = number | null | undefined;

export const fmt = {
  usd: (v: Maybe) => (v === null || v === undefined ? "–" : usd.format(v)),
  signedUsd: (v: Maybe) => (v === null || v === undefined ? "–" : (v > 0 ? "+" : "") + usd.format(v)),
  /** A share between 0 and 1 as a percentage. */
  pct: (v: Maybe, digits = 1) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(digits)}%`),
  price: (v: Maybe) => {
    if (v === null || v === undefined) return "–";
    const digits = v >= 1000 ? 0 : v >= 10 ? 2 : 4;
    return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  },
};

export function tone(value: Maybe): string {
  if ((value as number) > 0) return "gain";
  if ((value as number) < 0) return "loss";
  return "";
}

export interface Column<Row> {
  label: string;
  num?: boolean;
  cell: (row: Row) => Child;
  cls?: (row: Row) => string;
}

export function table<Row>(columns: Array<Column<Row>>, rows: Row[], emptyText: string): HTMLElement {
  if (!rows.length) return h("p", { class: "empty" }, emptyText);
  return h("div", { class: "table-scroll" },
    h("table", null,
      h("thead", null, h("tr", null, columns.map((c) =>
        h("th", { scope: "col", class: c.num ? "num" : "" }, c.label)))),
      h("tbody", null, rows.map((row) => h("tr", null, columns.map((c) =>
        h("td", { class: [c.num ? "num" : "", c.cls ? c.cls(row) : ""].join(" ").trim() },
          c.cell(row))))))));
}

/** A list of labelled figures: [label, value, optional class for the value]. */
export function figures(items: Array<[string, string, string?]>): HTMLElement {
  return h("dl", { class: "figures" }, items.map(([label, value, cls]) =>
    h("div", null, h("dt", null, label), h("dd", { class: cls || "" }, value))));
}

export function directionWord(direction: string): string {
  return direction === "short" ? "Short" : "Long";
}

// ---- the gauge: a win rate, the range it likely sits in, and the win rate needed to break even ----

/** Rates are shares between 0 and 1. */
export interface GaugeInput {
  trades: number;
  wins: number;
  winRate: number | null;
  low: number | null;
  high: number | null;
  breakeven: number | null;
  verdictText?: string;
}

type Verdict = "none" | "unknown" | "above" | "below" | "unclear";

function verdictOf(p: GaugeInput): Verdict {
  if (!p.trades) return "none";
  if (p.breakeven === null || p.breakeven === undefined) return "unknown";
  if ((p.low as number) > p.breakeven) return "above";
  if ((p.high as number) < p.breakeven) return "below";
  return "unclear";
}

const VERDICT_TEXT: Record<Verdict, string> = {
  none: "No closed trades yet, so there is no win rate to show.",
  unknown: "Too few trades to work out a break-even win rate.",
  above: "The win rate beats break-even across its whole likely range.",
  below: "The win rate is under break-even across its whole likely range.",
  unclear: "Not proven either way: the likely range still straddles break-even.",
};

// A label near either end of the track is anchored to that end so it stays on the page.
function edgeClass(position: number): string {
  if (position < 14) return " edge-left";
  if (position > 86) return " edge-right";
  return "";
}

export function gauge(p: GaugeInput): HTMLElement {
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
    const low = p.low as number;
    const rate = p.winRate as number;
    const width = Math.max(((p.high as number) - low) * 100, 1);
    track.append(
      h("div", { class: "gauge-range", style: `left:${low * 100}%;width:${width}%` }),
      h("div", { class: "gauge-dot", style: `left:${rate * 100}%` }),
    );
    const at = rate * 100;
    root.append(h("div", { class: `gauge-label bottom${edgeClass(at)}`, style: `left:${at}%` },
      `Win rate ${fmt.pct(rate)}`));
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

/**
 * Run `pause` when the tab is hidden and `resume` when it is shown again, so
 * a page nobody is looking at does not keep asking for data.
 */
export function whileVisible(pause: () => void, resume: () => void): void {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) pause();
    else resume();
  });
}
