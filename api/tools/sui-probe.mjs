const CDR = "http://127.0.0.1:9333";
const BLUB = "0x4d671396980c41e02cbeda518e48c9127ccd177a4db3b9aa2e5b17763aa24f67";
const URL = "http://127.0.0.1:8790/sui/token/" + BLUB;

const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page");
if (!tabs.length) { console.log("NO-PAGE"); process.exit(2); }
const tab = tabs[0];
console.error("attaching:", tab.id, (tab.url || "").slice(0, 60));

const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = (e) => rej(new Error("ws-error " + e.message)); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout " + method)); } }, 15000); });

await send("Page.enable");
await send("Runtime.enable");
console.error("navigating ...");
const nav = await send("Page.navigate", { url: URL });
console.error("navId:", JSON.stringify(nav));
await new Promise((r) => setTimeout(r, 14000));

const res = await send("Runtime.evaluate", {
  expression: `(() => {
    const bt = document.body ? document.body.innerText : "";
    const hrefs = [...document.querySelectorAll("a[href]")].map((a) => a.getAttribute("href")).filter(Boolean);
    return {
      url: location.href.slice(0, 140),
      title: (document.title || "").slice(0, 60),
      solHrefs: hrefs.filter((h) => /\\/sol\\//.test(h)).slice(0, 10),
      suiHrefs: hrefs.filter((h) => /\\/sui\\//.test(h)).length,
      solWord: (bt.match(/\\bSOL\\b/g) || []).length,
      ethWord: (bt.match(/\\bETH|Ethereum\\b/g) || []).length,
      bodyHead: bt.replace(/\\n{2,}/g, " | ").slice(0, 320),
      hrefsTotal: hrefs.length
    };
  })()`,
  returnByValue: true
});
console.log("RESULT:", JSON.stringify(res || { errno: "null" }, null, 1));
ws.close();
process.exit(0);