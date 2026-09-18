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

const probe = await send("Runtime.evaluate", {
  expression: `(() => {
    const out = { dialogs: [], triggerInfo: null };
    const trig = document.querySelector("[data-testid=chain-multi-select-trigger]");
    if (trig) {
      const rid = trig.getAttribute("aria-controls");
      out.triggerInfo = { rid, expanded: trig.getAttribute("aria-expanded"), text: trig.innerText.trim().slice(0, 30) };
      if (trig.getAttribute("aria-expanded") !== "true") { trig.click(); }
    }
    // wait handled outside; gather role=dialog / radix popovers already present
    out.dialogs = [...document.querySelectorAll("[role=dialog], [data-radix-popper-content-wrapper]")].map((d) => ({
      id: d.id || d.getAttribute("data-state") || "?",
      text: (d.innerText || "").replace(/\\n{2,}/g, " | ").slice(0, 300),
      opts: [...d.querySelectorAll("[data-radix-collection-item], [role=option], [data-value]")].map((x) => ((x.getAttribute("data-value") || x.textContent || "").trim().slice(0, 24))).slice(0, 20)
    }));
    return out;
  })()`,
  returnByValue: true
});
console.log("PROBE:", JSON.stringify(probe?.result?.value ?? probe, null, 1));
await new Promise((r) => setTimeout(r, 1500));

const after = await send("Runtime.evaluate", {
  expression: `(() => {
    const open = [...document.querySelectorAll("[data-radix-popper-content-wrapper]")];
    return {
      openCount: open.length,
      menus: open.map((d) => ({
        state: d.getAttribute("data-state"),
        text: (d.innerText || "").replace(/\\n{2,}/g, " | ").slice(0, 320),
        items: [...d.querySelectorAll("[data-radix-collection-item]")].map((x) => x.textContent.trim().slice(0, 20)).slice(0, 20)
      }))
    };
  })()`,
  returnByValue: true
});
console.log("AFTER:", JSON.stringify(after?.result?.value ?? after, null, 1));
ws.close();
process.exit(0);