const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page" && t.url.includes("/sui/token/"));
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 15000); });
await send("Runtime.enable");
const r = await send("Runtime.evaluate", {
  expression: `(() => [...document.querySelectorAll("img")].filter(i => /solana/i.test(i.getAttribute("src")||"")).map((im, ix) => {
      const r = im.getBoundingClientRect();
      return { ix, src: (im.getAttribute("src")||"").slice(0,55), w: Math.round(r.width), h: Math.round(r.height), visible: im.offsetWidth>0, parentVisible: im.closest("[data-testid]")?im.closest("[data-testid]").getAttribute("data-testid"):"none", topTestid: (im.closest("[data-testid]")||{}).getAttribute && im.closest("div.header,header") ? "in-header-bar" : "not-header" };
    }))()`, returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close(); process.exit(0);