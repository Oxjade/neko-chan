const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page" && t.url.includes("/sui/token/"));
if (!tabs.length) { console.log("NO-PAGE"); process.exit(2); }
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 12000); });
await send("Runtime.enable");

const info = await send("Runtime.evaluate", {
  expression: `(() => {
    const btn = [...document.querySelectorAll("button")].find((el) => /\\bSOL\\b/.test(el.textContent || "") || (el.className || "").toString().includes("cursor-not-allowed"));
    if (!btn) return { found: false };
    const info = {
      found: true,
      disabled: btn.disabled,
      hasDisabledClass: (btn.className || "").toString().includes("disabled"),
      cls: (btn.className || "").toString().slice(0, 100),
      text: (btn.textContent || "").trim().slice(0, 60),
      aria: btn.getAttribute("aria-label") || btn.getAttribute("title") || ""
    };
    // expand the element for outerHTML (minified, but shows hierarchy)
    btn._probe = 1;
    info.outer = btn.outerHTML.slice(0, 400);
    info.parents = (() => { let p = btn.parentElement, out = []; for (let i = 0; i < 4 && p; i++) { out.push((p.tagName || "") + "." + (p.className || "").toString().slice(0, 50)); p = p.parentElement; } return out; })();
    // click it and see what the DOM adds (the chain dropdown)
    btn.click();
    return info;
  })()`,
  returnByValue: true
});
console.log("BUTTON:", JSON.stringify(info?.result?.value ?? info, null, 1));
await new Promise((r) => setTimeout(r, 1500));
const after = await send("Runtime.evaluate", {
  expression: `(() => {
    // after click: what appeared?
    const all = [...document.querySelectorAll("body *")].filter((el) => el.children.length === 0);
    const chainish = all.filter((el) => /\\b(sol|base|bsc|eth|ethereum|tron|arbitrum|sui)\\b/i.test(el.textContent || "") && !/price|token|balance/i.test(el.textContent || "")).slice(0, 14);
    const bt = document.body.innerText;
    return {
      chainish: chainish.map((el) => (el.tagName + ":" + (el.textContent || "").trim()).slice(0, 40)),
      solAfter: (bt.match(/\\bSOL\\b/g) || []).length,
      otherChips: ["BASE","BSC","ETH","TRON","Sui","SOL"].filter((w) => (bt.match(new RegExp("\\\\b" + w + "\\\\b", "g")) || []).length).map((w) => w + "x" + (bt.match(new RegExp("\\\\b" + w + "\\\\b", "g")) || []).length)
    };
  })()`,
  returnByValue: true
});
console.log("AFTER-CLICK:", JSON.stringify(after?.result?.value ?? after, null, 1));
ws.close();
process.exit(0);