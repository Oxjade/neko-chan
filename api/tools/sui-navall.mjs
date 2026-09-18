const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page");
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 20000); });
await send("Runtime.enable");
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const navA = [...document.querySelectorAll("a[href]")].filter(a => !!a.closest("div.header,header,nav") || /^\\/[a-zA-Z]/.test(a.getAttribute("href")||""));
    const seen = new Set(); const items = [];
    navA.forEach(a => {
      const href = a.getAttribute("href") || "";
      const path = href.split("?")[0];
      const text = (a.innerText||a.getAttribute("aria-label")||"").trim().replace(/\\s+/g," ").slice(0,26);
      const key = path + "|" + text;
      if (seen.has(key)) return; seen.add(key);
      items.push({ path: path.slice(0,40), text, mightBeNav: a.closest("div.header,header,nav") ? "HDR" : "body" });
    });
    return items;
  })()`, returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close(); process.exit(0);