const CDR = "http://127.0.0.1:9333";
const want = "0x4d671396980c41e02cbeda518e48c9127ccd177a4db3b9aa2e5b17763aa24f67";
const tabs = await (await fetch(CDR + "/json/list")).json();
const tab = tabs.find((t) => t.type === "page" && t.url.includes("/sol/token/" + want)) ?? tabs.find((t) => t.type === "page");
const ws = new WebSocket(tab.webSocketDebuggerUrl);
let mid = 0; const pend = new Map();
const send = (m, p = {}) => new Promise((res, rej) => { const i = ++mid; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
ws.on("message", (d) => { const m = JSON.parse(d); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(m.error.message)) : res(m.result); } });
await new Promise((r) => ws.on("open", r));
await send("Runtime.enable");
await new Promise((r) => setTimeout(r, 4000));
const expr = `(() => {
  const pick = (sel) => { const e = document.querySelector(sel); return e ? (e.innerText || "").trim().slice(0, 120) : null; };
  return {
    title: document.title.slice(0, 100),
    h1: pick("h1"),
    headerName: pick(".token-name, .token-info .name, [class*=tokenName]"),
    headerSymbol: pick(".token-symbol, [class*=tokenSymbol]"),
    headerPrice: pick(".token-price, [class*=price]"),
    anyText: (document.body.innerText || "").slice(0, 220)
  };
})()`;
const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true });
console.log(JSON.stringify(r.result.value, null, 1));
ws.send(JSON.stringify({ id: ++mid, method: "Page.navigate", params: { url: tab.url } }));
ws.close(); process.exit(0);
