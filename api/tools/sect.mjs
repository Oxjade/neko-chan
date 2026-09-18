const t = process.argv[2];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const tab = await (await fetch(`http://127.0.0.1:9333/json/new?${encodeURIComponent(t)}`, { method: "PUT" })).json();
const { default: WS } = await import("ws");
const ws = new WS(tab.webSocketDebuggerUrl);
let mid = 0; const pend = new Map();
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++mid; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
ws.on("message", (d) => { const m = JSON.parse(d); if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } });
await new Promise((r) => ws.on("open", r));
await send("Runtime.enable"); await sleep(Number(process.argv[3] ?? 12000));
const expr = `(() => {
  const root = document.querySelector('#__next') || document.body;
  return [...root.children].map(c => ({
    tag: c.tagName, id: c.id, cls: (c.className||'').toString().slice(0,40),
    visible: !!(c.offsetWidth || c.offsetHeight),
    text: (c.innerText||'').slice(0,120).replace(/\\n+/g,' | ')
  }));
})()`;
const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true });
console.log(JSON.stringify(r.result?.value, null, 1));
await fetch(`http://127.0.0.1:9333/json/close/${tab.id}`); ws.close(); process.exit(0);
