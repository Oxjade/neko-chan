const CDR = "http://127.0.0.1:9333";
const targets = [
  "http://127.0.0.1:8790/",
  "http://127.0.0.1:8790/trend",
  "http://127.0.0.1:8790/sui/token/0x4d671396980c41e02cbeda518e48c9127ccd177a4db3b9aa2e5b17763aa24f67"
];
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page");
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 20000); });
await send("Runtime.enable");
for (const URL of targets) {
  await send("Page.navigate", { url: URL });
  await new Promise((r) => setTimeout(r, 16000));
  const r = await send("Runtime.evaluate", {
    expression: `(() => {
      const bt = document.body.innerText;
      const trig = document.querySelector("[data-testid=chain-multi-select-trigger]");
      const cutHrefs = ["trade","follow","perpetual","ai","copy","refer","kol","contest","bot","call"];
      const navAtoms = [];
      document.querySelectorAll("a[href]").forEach(a => {
        if (!a.closest("div.header,header,nav") && !(a.getAttribute("href")||"").startsWith("/")) return;
        const href = a.getAttribute("href") || "";
        const path = href.split("?")[0];
        if (cutHrefs.some(c => path.startsWith("/" + c))) {
          const r = a.getBoundingClientRect();
          if (r.width > 0 && r.height > 0) navAtoms.push(path.slice(0,30) + ":" + (a.innerText||a.getAttribute("aria-label")||"").trim().slice(0,14));
        }
      });
      return {
        url: location.pathname.slice(0,32),
        title: document.title.slice(0,38),
        titleBranded: document.title.includes("Neko") && !document.title.includes("GMGN") && !document.title.includes("Multi-Chain"),
        chainTrigVisible: trig ? (trig.offsetWidth > 0) : false,
        chainhoodTag: [...document.querySelectorAll("style")].some(s => s.textContent.includes("display:none!important") && s.textContent.includes("chain-switch-current")),
        solWords: (bt.match(/\\bSOL\\b/g) || []).length,
        solHrefs: [...document.querySelectorAll("a[href]")].filter(a => /\\/sol\\//.test(a.getAttribute("href")||"")).length,
        visibleNavAtoms: navAtoms,
        bodyLen: bt.length,
        buttons: document.querySelectorAll("button").length,
        navVisible: !!document.querySelector("nav, div.header")
      };
    })()`, returnByValue: true
  });
  console.log("=== " + URL.slice(21) + " ===");
  console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
}
ws.close(); process.exit(0);