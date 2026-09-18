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
// install a spy that reports execution order
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    window.__order = [];
    document.addEventListener("click", function(){ window.__order.push("capture-doc"); }, true);
    document.addEventListener("click", function(){ window.__order.push("bubble-doc"); }, false);
    const a = [...document.querySelectorAll("a[href]")].find(a => (a.getAttribute("href")||"").includes("/trend?chain=sol") && a.getBoundingClientRect().width > 0);
    const disp = document.createEvent("MouseEvents");
    disp.initEvent("click", true, true);
    a.dispatchEvent(disp);
    return { dispatched: !!a, orderAfterDispatch: window.__order };
  })()`, returnByValue: true
});
console.log("probe1:", JSON.stringify(r?.result?.value));
await new Promise((res) => setTimeout(res, 5000));
const r2 = await send("Runtime.evaluate", { expression: `({ url: location.href, order: window.__order })`, returnByValue: true });
console.log("after:", JSON.stringify(r2?.result?.value));
ws.close(); process.exit(0);