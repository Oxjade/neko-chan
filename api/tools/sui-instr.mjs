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
await send("Runtime.evaluate", {
  expression: `(() => {
    window.__log = [];
    const origAssign = location.assign.bind(location);
    try { location.assign = function(u){ window.__log.push("assign:"+u); return origAssign(u); }; } catch(e){ window.__log.push("assign-patch-fail"); }
    const origPush = history.pushState.bind(history);
    history.pushState = function(...a){ window.__log.push("pushState:"+(a[2]||"")); return origPush.apply(history,a); };
    document.addEventListener("click", function(e){ window.__log.push("guard-capture-run"); }, true);
  })()`, returnByValue: true
});
const loc = await send("Runtime.evaluate", {
  expression: `(() => { const a=[...document.querySelectorAll("a[href]")].find(a=>{const h=a.getAttribute("href")||"";return h.indexOf("/trend?chain=sol")!==-1&&a.getBoundingClientRect().width>0}); if(!a)return null; const r=a.getBoundingClientRect(); return {x:r.x+5,y:r.y+r.height/2,href:a.getAttribute("href")}; })()`,
  returnByValue: true
});
const { x, y, href } = loc?.result?.value ?? {};
console.log("target:", href, "at", x, y);
await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
await new Promise((r) => setTimeout(r, 7000));
const after = await send("Runtime.evaluate", {
  expression: `({ url: location.href, log: window.__log })`, returnByValue: true
});
console.log(JSON.stringify(after?.result?.value, null, 1));
ws.close(); process.exit(0);