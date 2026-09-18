const CDR = process.env.CDR || "http://127.0.0.1:9333";
const WANT = "BLUB";
const tabs = await (await fetch(CDR + "/json/list")).json();
const tab = tabs.find((t) => t.type === "page" && t.url.includes("/sol/token/")) || tabs.find((t) => t.type === "page");
if (!tab) { console.log("NO_TAB"); process.exit(2); }

const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let idc = 0ksid;
const pend = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) {
    const { res, rej } = pend.get(m.id);
    pend.delete(m.id);
    m.error ? rej(new Error(m.error.message)) : res(m.result);
  }
};
const send = (method, params = {}) =>
  new Promise((res, rej) => {
    const id = ++idc;
    pend.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params }));
  });

await send("Runtime.enable");
await new Promise((r) => setTimeout(r, 5000));

const expr = `(() => {
  const g = (sel) => (document.querySelector(sel)?.innerText || "").trim().slice(0, 90) || null;
  const all = [...document.querySelectorAll("h1,h2,h3,div,span")]
    .map(e => (e.innerText || "").trim())
    .filter(t => /BLUB|\\$|0\\.0/.test(t));
  return {
    title: document.title.slice(0, 90),
    url: location.href.slice(0, 100),
    h1: g("h1"),
    nameish: all.slice(0, 4),
    bodyHasBLUB: (document.body?.innerText || "").includes("BLUB"),
    bodyHasDollar: /\\$/.test(document.body?.innerText || "")
  };
})()`;
const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true });
console.log(JSON.stringify(r.value, null, 1));
ws.close();
process.exit(0);
