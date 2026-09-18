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
const pt = await send("Runtime.evaluate", {
  expression: `(() => { const a=[...document.querySelectorAll("a[href]")].find(a=>(a.getAttribute("href")||"").startsWith("/trend?chain=sol")&&a.getBoundingClientRect().width>0); if(!a)return null; const r=a.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`,
  returnByValue: true
});
const { x, y } = pt?.result?.value ?? {};
if (x == null) { console.log("trend link not found"); process.exit(1); }
const targetId = tab.id;
await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
await new Promise((r) => setTimeout(r, 8000));
const after = await send("Runtime.evaluate", { expression: `location.href`, returnByValue: true });
console.log("after click URL:", after?.result?.value);
ws.close(); process.exit(0);