const CDR = "http://127.0.0.1:9333";
const BLUB = "0x4d671396980c41e02cbeda518e48c9127ccd177a4db3b9aa2e5b17763aa24f67";
const URL = "http://127.0.0.1:8790/sui/token/" + BLUB;
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page");
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 20000); });
await send("Runtime.enable");
await send("Page.navigate", { url: URL });
await new Promise((r) => setTimeout(r, 16000));
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const bt = document.body.innerText;
    const trig = document.querySelector("[data-testid=chain-multi-select-trigger]");
    const delim = document.querySelectorAll("[data-testid=chain-switch-current]");
    return {
      title: document.title,
      titleBranded: document.title.includes("Neko") && !document.title.includes("GMGN") && !document.title.includes("Multi-Chain"),
      chainhoodTag: [...document.querySelectorAll("style")].some(s => s.textContent.includes("display:none!important") && s.textContent.includes("chain-switch-current")),
      trigDisplay: trig ? getComputedStyle(trig).display : "no-elem",
      trigVisible: trig ? (trig.offsetWidth > 0) : false,
      switchDisplays: [...delim].map(e => getComputedStyle(e).display),
      solWords: (bt.match(/\\bSOL\\b/g) || []).length,
      solHrefs: [...document.querySelectorAll("a[href]")].filter(a => /\\/sol\\//.test(a.getAttribute("href") || "")).length,
      solIconsInHeader: [...document.querySelectorAll("img")].filter(i => /solana/i.test(i.getAttribute("src") || "")).length,
      bodyLen: bt.length,
      hydraSigns: { buttons: document.querySelectorAll("button").length, canvases: document.querySelectorAll("canvas").length, nav: !!document.querySelector("nav") },
      headlineSui: bt.includes("Fastest Sui Meme Trading Terminal")
    };
  })()`,
  returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close();
process.exit(0);