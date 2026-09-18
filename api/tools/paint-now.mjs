// Node 22 built-ins only: global fetch + global WebSocket (no ws import, no TS)
const CDR = "http://127.0.0.1:9333";
const WANT = "0x4d671396980c41e02cbeda518e48c9127ccd177a4db3b9aa2e5b17763aa24f67";

const tabs = await (await fetch(CDR + "/json/list")).json();
const tab = tabs.find((t) => t.type === "page" && t.url.includes(WANT)) || tabs.find((t) => t.type === "page");
if (!tab) { console.log("NO-TAB"); process.exit(2); }

const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let mid = 0; const pend = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) {
    const h = pend.get(m.id); pend.delete(m.id);
    if (m.error) h.rej(new Error(m.error.message)); else h.res(m.result);
  }
};
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++mid; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
});

await send("Runtime.enable");
await new Promise((r) => setTimeout(r, 9000));

const expr = `(() => {
  const g = (s) => { const e = document.querySelector(s); return e ? (e.innerText || "").trim().slice(0, 60) : null; };
  const bodyTxt = document.body ? document.body.innerText : "";
  return {
    url: location.href.slice(0, 110),
    title: document.title.slice(0, 100),
    h1: g("h1"),
    hasBLUB: bodyTxt.includes("BLUB"),
    hasDollar: /\\$/.test(bodyTxt),
    headerText: (document.querySelector("header") ? document.querySelector("header").innerText.slice(0, 60) : null)
  };
})()`;
const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true }).catch((e) => ({ err: String(e && e.message || e) }));
console.log("RESULT=" + JSON.stringify(r && r.value));
ws.close();
process.exit(0);
