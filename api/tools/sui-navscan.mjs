const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page");
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 20000); });
await send("Runtime.enable");
await send("Page.navigate", { url: "http://127.0.0.1:8790/" });
await new Promise((r) => setTimeout(r, 16000));
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const out = { nav: [], allTopHrefs: [] };
    // gather nav-links: anchors + onClick buttons inside header-ish containers
    const cands = [...document.querySelectorAll("a[href], button")];
    const cut = ["snipex","snipe","bot","call","contest","follow","kol","referral","rank","ai","devSnipe","tokenSnipe","trenches","copy"];
    out.allTopHrefs = [...document.querySelectorAll("a[href]")].map(a => a.getAttribute("href")).filter(Boolean).slice(0, 40);
    document.querySelectorAll("a[href],button").forEach((el) => {
      const href = el.getAttribute && el.getAttribute("href") || "";
      const text = (el.innerText||"").trim().replace(/\\n/g," ").slice(0, 30);
      const low = (href + " " + text).toLowerCase();
      if (cut.some(c => low.includes(c))) {
        out.nav.push({ tag: el.tagName, href: href.slice(0,50), text, testid: el.getAttribute? el.getAttribute("data-testid")||"" : "", inHeader: !!el.closest("div.header,header") });
      }
    });
    return out;
  })()`, returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close(); process.exit(0);