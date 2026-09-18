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
    const el = document.querySelector("[data-testid=chain-multi-select-trigger]");
    if (!el) return { noEl: true };
    const fk = Object.keys(el).find((k) => k.startsWith("__reactFiber$"));
    const out = [];
    let f = fk ? el[fk] : null;
    for (let d = 0; f && d < 32; d++, f = f.return) {
      const type = f.type;
      const nm = typeof type === "function" ? (type.displayName || type.name || "fn?") : typeof type === "string" ? "<" + type + ">" : type ? "<obj>" : "<null>";
      let src = "";
      if (typeof type === "function") { try { src = (type.toString() || "").slice(0, 220); } catch {} }
      out.push({ d, nm, src });
    }
    return out;
  })()`,
  returnByValue: true
});
const v = r?.result?.value ?? r?.value;
console.log(JSON.stringify(v, null, 1).slice(0, 5000));
ws.close();
process.exit(0);