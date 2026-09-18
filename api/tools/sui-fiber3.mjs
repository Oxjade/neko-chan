const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page" && t.url.includes("/sui/token/"));
if (!tabs.length) { console.log("NO-PAGE"); process.exit(2); }
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 12000); });
await send("Runtime.enable");

const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const el = document.querySelector("[data-testid=chain-multi-select-trigger]");
    if (!el) return { noEl: true };
    const fiberKey = Object.keys(el).find((k) => k.startsWith("__reactFiber"));
    const out = [];
    let f = fiberKey ? el[fiberKey] : null;
    for (let d = 0; f && d < 30; d++, f = f.return) {
      const type = f.type;
      const nm = typeof type === "function" ? (type.displayName || type.name || "fn?") : typeof type === "string" ? "<" + type + ">" : type ? "<obj>" : "<null>";
      const p = f.memoizedProps || {};
      const pk = Object.keys(p);
      const shaped = {};
      for (const k of pk) {
        const v = p[k];
        if (Array.isArray(v)) shaped[k] = "ARR(" + v.length + ")";
        else if (typeof v === "string") shaped[k] = v.length > 50 ? v.slice(0, 50) + "…" : v;
      }
      out.push({ nm, keys: pk.length, props: shaped });
      if (d > 18 && nm === "<null>") break;
    }
    return { ancestorChain: out };
  })()`,
  returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1).slice(0, 4200));
ws.close();
process.exit(0);