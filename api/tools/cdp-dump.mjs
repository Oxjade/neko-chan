// Attach to the first chromium page tab and dump rendered text + table state.
// usage: node tools/cdp-dump.mjs [url]
import { inspect } from "node:util";
const target = process.argv[2] ?? "http://127.0.0.1:8790/trenches";
const PORT = 9333;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const tab = await (
  await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(target)}`, { method: "PUT" })
).json();
const { default: WS } = await import("ws");
const ws = new WS(tab.webSocketDebuggerUrl);
let id = 0;
const pending = new Map();
const send = (method, params = {}) =>
  new Promise((res, rej) => {
    const mid = ++id;
    pending.set(mid, { res, rej });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
ws.on("message", (d) => {
  const m = JSON.parse(d.toString());
  if (m.id && pending.has(m.id)) {
    const p = pending.get(m.id);
    pending.delete(m.id);
    m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
  }
});
await new Promise((r) => ws.on("open", r));
await send("Runtime.enable");
await sleep(Number(process.argv[3] ?? 9000));
const expr = `(() => {
  const t = document.body.innerText.replace(/\\n{2,}/g, "\\n").slice(0, 1500);
  const rows = document.querySelectorAll("table tbody tr").length;
  const skeletons = document.querySelectorAll("[class*=skeleton],[class*=loading]").length;
  return { rows, skeletons, text: t };
})()`;
const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true });
console.log(inspect(r.result.value, { depth: 3 }));
await fetch(`http://127.0.0.1:${PORT}/json/close/${tab.id}`);
ws.close();
process.exit(0);
