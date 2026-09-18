// One-off DOM state checker for the mirrored token page.
const t = process.argv[2] ?? "http://127.0.0.1:8790/sui/token/0x5ecbc3b11d23c6897449071dae9896f9fe8a51345f5465233fb82cb7d5c6f1bb";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const tab = await (await fetch(`http://127.0.0.1:9333/json/new?${encodeURIComponent(t)}`, { method: "PUT" })).json();
const { default: WS } = await import("ws");
const ws = new WS(tab.webSocketDebuggerUrl);
let mid = 0;
const pend = new Map();
const send = (m, p = {}) =>
  new Promise((res, rej) => { const i = ++mid; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
ws.on("message", (d) => {
  const m = JSON.parse(d);
  if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); }
});
await new Promise((r) => ws.on("open", r));
await send("Runtime.enable");
await sleep(Number(process.argv[3] ?? 9000));
const expr = `(() => {
  const txt = document.body.innerText;
  return {
    hasName: txt.includes('MOCHI') || txt.includes('Mochi'),
    next: (document.querySelector('#__next')?.innerText || '').slice(0, 700),
    hasDollar: txt.indexOf('$0.00') >= 0 || txt.indexOf('$0.0') >= 0,
    canvases: document.querySelectorAll('canvas').length,
    htmlLen: document.documentElement.innerHTML.length,
    head: txt.slice(0, 200).replace(/\\n+/g, ' | '),
    loadingText: txt.toLowerCase().includes('loading')
  };
})()`;
const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true });
console.log(JSON.stringify(r.result?.value ?? r, null, 1));
await fetch(`http://127.0.0.1:9333/json/close/${tab.id}`);
ws.close();
process.exit(0);
