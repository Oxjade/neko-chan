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
console.log("navigating:", URL.slice(0, 60), "...");
await send("Page.navigate", { url: URL });
await new Promise((r) => setTimeout(r, 18000));

const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const el = document.querySelector("[data-testid=chain-multi-select-trigger]");
    const out = { url: location.href.slice(0, 100), triggerText: el ? el.innerText.trim().slice(0, 30) : null };
    if (!el) return out;
    const fk = Object.keys(el).find((k) => k.startsWith("__reactFiber$"));
    const found = [];
    let f = fk ? el[fk] : null;
    for (let d = 0; f && d < 50; d++, f = f.return) {
      const p = f.memoizedProps || {};
      const keys = Object.keys(p);
      if (keys.includes("options") && Array.isArray(p.options)) {
        out.optionsProp = { depth: d, sample: JSON.stringify(p.options).slice(0, 700) };
        found.push(d);
      }
      if (Array.isArray(p.value) && p.value.length) {
        out.valueProp = { depth: d, sample: JSON.stringify(p.value).slice(0, 200) };
      }
    }
    out.foundDepths = found;
    return out;
  })()`,
  returnByValue: true
});
console.log("R1:", JSON.stringify(r && r.value ? r.value : r, null, 1).slice(0, 1600));
ws.close();
process.exit(0);