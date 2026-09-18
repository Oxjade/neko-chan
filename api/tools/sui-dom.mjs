const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page" && t.url.includes("/sui/token/"));
if (!tabs.length) { console.log("NO-PAGE"); process.exit(2); }
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = (e) => rej(new Error("ws-error")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 12000); });

await send("Runtime.enable");

// 1) the stray "SOL" on the page: is it a chip/button/label or body copy?
const sol = await send("Runtime.evaluate", {
  expression: `(() => {
    const nodes = [...document.querySelectorAll("body *")].filter((el) => el.children.length === 0 && /\\bSOL\\b/.test(el.textContent || "")).slice(0, 5);
    return nodes.map((el) => ({ tag: el.tagName, text: (el.textContent || "").trim().slice(0, 40), cls: (el.className || "").toString().slice(0, 60), tagRow: (el.parentElement ? el.parentElement.textContent : "").trim().slice(0, 80) }));
  })()`,
  returnByValue: true
});
console.log("SOL-NODES:", JSON.stringify(sol?.result?.value ?? sol));

// 2) any chain selector: elements whose clickable text is a chain name
const sel = await send("Runtime.evaluate", {
  expression: `(() => {
    const names = ["Solana", "SOL", "Base", "BSC", "Ethereum", "ETH", "Tron", "Arbitrum", "Sui"];
    const found = {};
    for (const n of names) {
      const els = [...document.querySelectorAll("button, [role=menuitem], li, a")].filter((el) => el.textContent && el.textContent.trim() === n).slice(0, 3);
      if (els.length) found[n] = els.map((el) => el.tagName + "." + (el.className || "").toString().slice(0, 40));
    }
    return found;
  })()`,
  returnByValue: true
});
console.log("CHAIN-SELECTOR:", JSON.stringify(sel?.result?.value ?? sel));
ws.close();
process.exit(0);