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
// Attach a capture listener BEFORE any click, and check elementFromPoint of the anchor center
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    window.__hit = [];
    // mimic the guard exactly
    window.addEventListener("click", function(e){
      var a=e.target&&e.target.closest?e.target.closest("a[href]"):null;
      window.__hit.push({ t: a ? a.getAttribute("href") : "NO-ANCHOR", tag: a ? "A" : "none" });
    }, true);
    const a=[...document.querySelectorAll("a[href]")].find(a=>{const h=a.getAttribute("href")||"";return h.indexOf("/trend?chain=sol")!==-1&&a.getBoundingClientRect().width>0});
    const pts = [];
    const host = a && a.getBoundingClientRect();
    if (host) {
      for (let dx=-5;dx<=5;dx+=5) for (let dy=-5;dy<=5;dy+=5) {
        const el = document.elementFromPoint(host.x+host.width/2+dx, host.y+host.height/2+dy);
        pts.push(el ? (el.tagName + "." + ((el.className||"").toString().split(" ")[0]||"")) : "none");
      }
    }
    const allLinks = [...document.querySelectorAll("a[href]")].map(a => ({ h: a.getAttribute("href").slice(0,28), x: Math.round(a.getBoundingClientRect().x), y: Math.round(a.getBoundingClientRect().y), w: Math.round(a.getBoundingClientRect().width) })).filter(o => o.y < 60 && o.w > 0).slice(0, 12);
    return { found: !!a, centerEls: pts, headerLinks: allLinks };
  })()`, returnByValue: true
});
console.log(JSON.stringify(r?.result?.value, null, 1));
ws.close(); process.exit(0);