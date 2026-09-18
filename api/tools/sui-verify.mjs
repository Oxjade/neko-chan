const CDR = "http://127.0.0.1:9333";
const BLUB = "0x4d671396980c41e02cbeda518e48c9127ccd177a4db3b9aa2e5b17763aa24f67";
const URL = "http://127.0.0.1:8790/sui/token/" + BLUB;
const tabs0 = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page");
const tab = tabs0[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 20000); });
await send("Runtime.enable");
console.log("navigating (hard) ...");
await send("Page.navigate", { url: URL });
await new Promise((r) => setTimeout(r, 19000));
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const out = { url: location.href.slice(0, 110), title: (document.title||"").slice(0,50), bodyLen: document.body ? document.body.innerText.length : -1 };
    // hydration check: meaningful content rendered?
    const head = document.body ? document.body.innerText.replace(/\\n{2,}/g, " | ").slice(0, 220) : "";
    out.bodyHead = head;
    const trig = document.querySelector("[data-testid=chain-multi-select-trigger]");
    out.triggerText = trig ? trig.innerText.trim().slice(0, 24) : null;
    // any Siu icons/images in header?
    out.triggerImg = trig && trig.querySelector("img") ? (trig.querySelector("img").getAttribute("src")||"").slice(0,60) : "";
    out.chainChips = (document.body.innerText.match(/\\b(SOL|Sui|Base|BSC|ETH|Arbitrum|Tron)\\b/g)||[]).slice(0, 12);
    // errors?
    out.h2 = head.includes("Neko | The Fastest Sui");
    return out;
  })()`,
  returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close();
process.exit(0);