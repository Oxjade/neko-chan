import { readFileSync } from "node:fs";
const A = "/home/carnage/tradebotpro/frontend/webroot/_next/static/chunks/pages/_app-b324cc13ef8b46d9.js";
const t = readFileSync(A, "utf8");

// 1) ordered object-literal keys:  .Sol=...,_,.BSC=...,_,.Robinhood=...,_,.Base=...
const reObj = /\.(Sol|sol)=["'][a-z]+["'],_\.(BSC|bsc)=["'][a-z]+["'],_\.(Robinhood|robinhood)=["'][a-z]+["']/g;
let m;
let i = 0;
for (m of t.matchAll(reObj)) {
  const at = m.index;
  console.log("OBJ-HIT", ++i, "@", at, ":", t.slice(at - 30, at + 240));
  if (i >= 4) break;
}
if (!i) console.log("no ordered obj literal exported");

// 2) search allValues counting sources: what feeds "allValues" at 3102684 — scan backward for the assignment chain
const iAll = t.indexOf("allValues", 3100000);
console.log("\nallValues @", iAll);
const before = t.slice(iAll - 2600, iAll);
const refs = new Map();
for (const k of Object.keys(refs)) {}
// print keyword markers in the 2600-char window
for (const kw of ["enabledChains", "options", "allValues", "chains", "useState", "useMemo", "filter", "map(", "Object.keys", "Object.values", "sui", "sol", "priority", "order"]) {
  let c = 0; let p = 0;
  while ((p = before.indexOf(kw, p)) !== -1) { c++; p += kw.length; }
  if (c) console.log("  in-window '" + kw + "':", c);
}