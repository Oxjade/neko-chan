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
    const out = { testids: [], imgsInHeader: [], solCtx: [] };
    document.querySelectorAll("[data-testid]").forEach(el => {
      const tid = el.getAttribute("data-testid") || "";
      if (/chain|multi/i.test(tid)) out.testids.push(tid + " :: " + el.innerText.replace(/\\n/g," ").slice(0,28));
    });
    const header = document.querySelector("div.header, header") || document.body;
    header.querySelectorAll("img").forEach(im => {
      const s = im.getAttribute("src") || "";
      if (/chain|sol|sui|network/i.test(s)) out.imgsInHeader.push(s.slice(0,70));
    });
    // any element containing the word SOL with attribute hints
    document.querySelectorAll("[title], [aria-label]").forEach(el => {
      const t = (el.getAttribute("title")||"") + " " + (el.getAttribute("aria-label")||"");
      if (/\\bSOL\\b|Solana/i.test(t)) out.solCtx.push(t.slice(0,50));
    });
    return out;
  })()`, returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close(); process.exit(0);