const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page" && t.url.includes("/sui/token/"));
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 12000); });
await send("Runtime.enable");

const dialog = await send("Runtime.evaluate", {
  expression: `(() => {
    const trigger = document.querySelector("[data-testid=chain-multi-select-trigger]");
    if (!trigger) return { noTrigger: true };
    // ensure dialog is open
    if (trigger.getAttribute("aria-expanded") !== "true") trigger.click();
    return { opened: trigger.getAttribute("aria-expanded") };
  })()`,
  returnByValue: true
});
console.log("OPEN:", JSON.stringify(dialog?.result?.value ?? dialog));
await new Promise((r) => setTimeout(r, 1200));

const content = await send("Runtime.evaluate", {
  expression: `(() => {
    const trig = document.querySelector("[data-testid=chain-multi-select-trigger]");
    const cid = trig ? trig.getAttribute("aria-controls") : "";
    const el = cid ? document.getElementById(cid) : null;
    const bt = el ? el.innerText : "";
    return {
      cid,
      exists: !!el,
      text: bt.replace(/\\n{2,}/g, " | ").slice(0, 400),
      itemCount: el ? el.querySelectorAll("[role=option], [role=menuitem], label, .cursor-pointer").length : 0,
      items: el ? [...el.querySelectorAll("[role=option], [role=menuitem]")].map((x) => x.textContent.trim().slice(0, 30)).slice(0, 16) : []
    };
  })()`,
  returnByValue: true
});
console.log("DIALOG:", JSON.stringify(content?.result?.value ?? content, null, 1));

const triggerInfo = await send("Runtime.evaluate", {
  expression: `(() => {
    const el = document.querySelector("[data-testid=chain-multi-select-trigger]");
    if (!el) return { none: true };
    const img = el.querySelector("img");
    return {
      iconSrc: img ? (img.getAttribute("src") || "").slice(0, 120) : "",
      iconName: img ? (img.getAttribute("data-icon") || "") : "",
      innerText: el.innerText.trim().slice(0, 40),
      innerHTML: el.innerHTML.slice(0, 300)
    };
  })()`,
  returnByValue: true
});
console.log("TRIGGER:", JSON.stringify(triggerInfo?.result?.value ?? triggerInfo, null, 1));
ws.close();
process.exit(0);