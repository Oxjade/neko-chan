const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page" && t.url.includes("/sui/token/"));
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 15000); });
await send("Runtime.enable");

// collect console errors during a 3s window
const errors = [];
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.method === "Runtime.consoleAPICalled" && ["error", "assert"].includes(m.params.type)) errors.push((m.params.args||[]).map(a => a.value || a.description || "").join(" ").slice(0, 140)); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };

const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const bt = document.body.innerText;
    const out = {};
    out.bodyLen = bt.length;
    // dynamic token signals
    const sigs = [/BLUB/, /MARKET CAP/i, /Holders/i, /Temperature/i, /\\\\\$0\\.0/, /24h/i, /price/i, /chart/i];
    out.signals = Object.fromEntries(sigs.map(s => [s.source, s.test(bt)]));
    out.hasTokenAddrInBody = bt.includes("0x4d671396980c41e02cbeda518e48c9127ccd177a4db3b9aa2e5b17763aa24f67");
    out.buttons = [...document.querySelectorAll("button")].map(b => b.innerText.trim().slice(0,20)).filter(Boolean).slice(0, 14);
    out.canvases = document.querySelectorAll("canvas").length;
    out.hrefCount = document.querySelectorAll("a[href]").length;
    out.mainNav = !!document.querySelector("nav");
    const nums = (bt.match(/\\\\$[0-9.,]+|[0-9,.]+\\\\s?%/g)||[]).slice(0,8);
    out.numLike = nums;
    out.checkmarks = bt.replace(/\\s/g," ").slice(0, 1200);
    return out;
  })()`,
  returnByValue: true
});
await new Promise(r2 => setTimeout(r2, 2500));
console.log("=== DOM ==="); console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
console.log("=== console errors (" + errors.length + ") ===");
console.log(errors.slice(0, 10).join("\\n") || "(none)");
ws.close();
process.exit(0);