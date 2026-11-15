// Building the page: elements, formatting and tables shared by both screens.
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
