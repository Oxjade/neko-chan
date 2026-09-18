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
    // capture every fiber node in the ancestor chain with its type name + relevant props
    let node = el;
    const chain = [];
    for (let i = 0; i < 8 && node; i++) {
      const key = Object.keys(node).find((k) => k.startsWith("__reactFiber"));
      if (key) {
        let f = node[key];
        for (let d = 0; f && d < 22; d++, f = f.return) {
          const type = f.type;
          const nm = (typeof type === "function" ? (type.displayName || type.name || "fn") : typeof type === "string" ? type : type ? "<obj>" : "<null>");
          const p = f.memoizedProps || {};
          const interesting = {};
          for (const k of Object.keys(p)) {
            const v = p[k];
            if (typeof v === "string" && v.length < 40) interesting[k] = v;
            else if (Array.isArray(v) && v.length <= 30) interesting[k] = v.length + ":[arr]";
            else if (Array.isArray(v) && v.length > 30) interesting[k] = v.length + ":[arr]";
          }
          const hasChain = JSON.stringify(Object.keys(p)).includes("chain") || JSON.stringify(interesting).includes("sol");
          if (hasChain || nm.includes("Chain") || nm.includes("chain")) {
            chain.push({ nm, props: interesting });
          }
          if (chain.length > 30) break;
        }
      }
      node = node.parentElement;
    }
    return { fiberChain: chain };
  })()`,
  returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1).slice(0, 3000));
ws.close();
process.exit(0);