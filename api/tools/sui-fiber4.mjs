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
    const lines = [];
    let f = fiberKey ? el[fiberKey] : null;
    for (let d = 0; f && d < 45; d++, f = f.return) {
      const type = f.type;
      const nm = typeof type === "function" ? (type.displayName || type.name || "fn?") : typeof type === "string" ? "<" + type + ">" : type ? "<obj>" : "<null>";
      const p = f.memoizedProps || {};
      const pk = Object.keys(p);
      // only log fibers that hold app-level data: options/members/value/chains/lists/labels
      const interestingKeys = ["options", "members", "chains", "value", "values", "list", "items", "children", "label", "labels", "config", "data", "current"];
      const hit = pk.filter((k) => interestingKeys.includes(k) && k !== "children");
      if (hit.length) {
        const shaped = {};
        const seen = new WeakSet();
        const safe = (v) => {
          const stab = (k, b) => {
            if (typeof b === "object" && b !== null) {
              if (seen.has(b)) return "[cycle]";
              seen.add(b);
              if (b instanceof Element || b instanceof Node) return (b.tagName || b.nodeName || "[node]");
              if (typeof b.nativeEvent !== "undefined" || typeof b.__reactFiber !== "undefined") return "[react]";
            }
            return b;
          };
          try { return JSON.stringify(v, stab); } catch { return "[json-err]"; }
        };
        for (const k of hit) {
          const v = p[k];
          if (Array.isArray(v)) shaped[k] = safe(v).slice(0, 420);
          else if (typeof v === "object" && v) shaped[k] = safe(v).slice(0, 300);
          else if (typeof v === "string") shaped[k] = v.length > 200 ? v.slice(0, 200) + "…" : v;
        }
        lines.push("DEPTH " + d + " <" + nm + "> " + JSON.stringify(shaped));
      }
    }
    return { out: lines.slice(0, 40) };
  })()`,
  returnByValue: true
});
const path = "/home/carnage/tradebotpro/api/tools/fiber-dump.json";
const { writeFileSync } = await import("node:fs");
writeFileSync(path, JSON.stringify(r?.result?.value ?? r, null, 1));
console.log("wrote", path);
ws.close();
process.exit(0);