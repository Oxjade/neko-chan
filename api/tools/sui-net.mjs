const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page" && t.url.includes("/sui/token/"));
if (!tabs.length) { console.log("NO-PAGE"); process.exit(2); }
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 12000); });
await send("Runtime.enable");
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const es = performance.getEntriesByType("resource").map((e) => e.name);
    const api = es.filter((n) => /api|config|chain|network/.test(n)).slice(0, 30);
    const js = es.filter((n) => n.includes("_next/static/chunks")).slice(0, 30);
    return {
      total: es.length,
      apiCalls: api,
      chunkCount: js.length,
      chunks: js.map((u) => u.split("/").pop()).slice(0, 24)
    };
  })()`,
  returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close();
process.exit(0);