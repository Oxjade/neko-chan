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
  expression: `(() => {
    const out = [];
    document.querySelectorAll("[data-testid=chain-switch-current]").forEach((el) => {
      const host = el.closest("div") || el;
      const grand = host.parentElement || el;
      out.push({
        text: el.innerText.trim().slice(0, 20),
        label: (el.getAttribute("data-testid")||"") + " | aria:" + (el.getAttribute("aria-label")||"").slice(0,20) + " | title:" + (el.getAttribute("title")||"").slice(0,20),
        img: el.querySelector("img") ? (el.querySelector("img").getAttribute("src")||"").slice(0,60) : "",
        hostText: (grand.innerText||"").replace(/\\n/g," ").slice(0, 80),
        inTopBar: !!el.closest("[class*=h-\\\\[60px\\\\]],[class*=h-16],[class*=top]")
      });
    });
    return out;
  })()`, returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close(); process.exit(0);