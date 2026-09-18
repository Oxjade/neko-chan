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
    // walk up through fiber to find a node whose memoizedProps has an options/chains array
    let node = el;
    const seen = [];
    for (let i = 0; i < 12 && node; i++) {
      const key = Object.keys(node).find((k) => k.startsWith("__reactFiber"));
      if (key) {
        let f = node[key];
        for (let d = 0; f && d < 20; d++, f = f.return) {
          const p = f.memoizedProps;
          if (!p) continue;
          const arrKey = Object.keys(p).find((k) => Array.isArray(p[k]) && p[k].length > 1 && typeof p[k][0] === "object" && (p[k][0].chain || p[k][0].name || p[k][0].value || p[k][0].key || p[k][0].label));
          if (arrKey) seen.push({ depth: i + ":" + d, prop: arrKey, sample: JSON.stringify(p[arrKey]).slice(0, 700) });
        }
      }
      node = node.parentElement;
    }
    return { candidates: seen.slice(0, 4) };
  })()`,
  returnByValue: true
});
console.log("FIBER:", JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close();
process.exit(0);