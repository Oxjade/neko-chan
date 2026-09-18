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
    const out = { solVisibleChecks: {}, suiNodes: [] };
    const bt = document.body.innerText;
    out.solVisibleChecks.SOLtokens = (bt.match(/\\bSOL\\b/g)||[]).length;
    out.solVisibleChecks.ChainLinksSol = [...document.querySelectorAll("a[href]")].filter(a => /\\/sol\\//.test(a.getAttribute("href")||"")).length;
    // where does "Sui" appear? tag + context (header vs body copy)
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n; const suiMentions = [];
    while ((n = walker.nextNode()) && suiMentions.length < 6) {
      if (/\\bSui\\b/.test(n.textContent||"")) {
        suiMentions.push({ tag: n.parentElement ? n.parentElement.tagName : "?", text: n.textContent.trim().slice(0, 60), inHeader: !!n.parentElement.closest("header") });
      }
    }
    out.suiNodes = suiMentions;
    // header layout — what changed where the select was?
    const header = document.querySelector("header");
    out.headerText = header ? header.innerText.replace(/\\n{2,}/g, " | ").slice(0, 260) : "NO-HEADER";
    out.anyDataTestidChain = [...document.querySelectorAll("[data-testid*=chain-multi]")].map(x => x.getAttribute("data-testid") + ":" + x.innerText.trim().slice(0,20)).slice(0,5);
    return out;
  })()`,
  returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close();
process.exit(0);