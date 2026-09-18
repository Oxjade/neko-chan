const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page" && t.url.includes("8790"));
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 20000); });
await send("Runtime.enable");
await send("Page.navigate", { url: "http://127.0.0.1:8790/" });
await new Promise((r) => setTimeout(r, 16000));
const info = await send("Runtime.evaluate", {
  expression: `(() => {
    const cands = [...document.querySelectorAll("a[href]")].filter(a => {
      const h = a.getAttribute("href")||"";
      return h.startsWith("http://127.0.0.1:8790/trend?chain=sol") || h.startsWith("/trend?chain=sol");
    });
    return cands.map(a => ({ text: (a.innerText||"").trim().slice(0,12), href: (a.getAttribute("href")||"").slice(0,40), w: Math.round(a.getBoundingClientRect().width), h: Math.round(a.getBoundingClientRect().height) }));
  })()`, returnByValue: true
});
console.log("matching anchors:", JSON.stringify(info?.result?.value));
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const a = [...document.querySelectorAll("a[href]")].find(a => (a.getAttribute("href")||"").includes("/trend?chain=sol") && a.getBoundingClientRect().width > 0);
    if (!a) return "not-found";
    const disp = document.createEvent("MouseEvents");
    disp.initEvent("click", true, true);
    a.dispatchEvent(disp);
    return "dispatched on: " + a.innerText.trim();
  })()`, returnByValue: true
});
console.log("script click:", r?.result?.value);
await new Promise((res) => setTimeout(res, 7000));
const after = await send("Runtime.evaluate", { expression: `location.href`, returnByValue: true });
console.log("after URL:", after?.result?.value);
ws.close(); process.exit(0);