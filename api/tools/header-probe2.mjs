
const CDR = "http://127.0.0.1:9333";
const tabs = await (await fetch(CDR + "/json/list")).json();
const tab = tabs.find((t) => t.type === "page") ?? (await (await fetch(CDR + "/json/new?http://127.0.0.1:8790/sol/token/0x4d671396980c41e02cbeda518e48c9127ccd177a4db3b9aa2e5b17763aa24f67", { method: "PUT" })).json());
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let mid = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const h = pend.get(m.id); pend.delete(m.id); m.error ? h.j(new Error(m.error.message)) : h.r(m.result); } };
const send = (method, params = {}) => new Promise((r, j) => { const i = ++mid; pend.set(i, { r, j }); ws.send(JSON.stringify({ id: i, method, params })); });
await send("Runtime.enable");
await new Promise((r) => setTimeout(r, 9000));
const expr = `(() => {
  const g = (s) => { const e = document.querySelector(s); return e ? (e.innerText || "").trim().slice(0, 80) : null; };
  const txt = document.body ? (document.body.innerText || "") : "";
  const meta = {
    title: document.title.slice(0, 120),
    h1: g("h1"),
    header: g(".header, header"),
    nameish: txt.split("\\n").map(s => s.trim()).filter(Boolean).slice(0, 6),
    hasName: /BLUB|BLUB/i.test(txt),
    hasUsd: /\\$/.test(txt),
    url: location.href.slice(0, 100)
  };
  return meta;
})()`;
const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true });
console.log(JSON.stringify(r.value, null, 1));
ws.close(); process.exit(0);
