const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page" && t.url.includes("8790"));
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 15000); });
await send("Runtime.enable");
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const keep = ["trend","portfolio","monitor","rewards","watchlists"];
    const list = [];
    document.querySelectorAll("a[href][href^='/']").forEach(a => {
      const path = (a.getAttribute("href")||"").split("?")[0];
      const label = (a.innerText||"").trim();
      if (keep.some(c => path === "/" + c || path.startsWith("/" + c + "/"))) {
        const cr = a.getBoundingClientRect();
        list.push({ path: path.slice(0,26), label: label.slice(0,14), w: Math.round(cr.width), visHdr: !!a.closest("div.header,header"), computed: getComputedStyle(a).display });
      }
    });
    const hdr = document.querySelector("div.header, header");
    return { headerText: hdr ? hdr.innerText.replace(/\\n{2,}/g," | ").slice(0, 200) : "NO-HDR", keepList: list.slice(0, 10) };
  })()`, returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close(); process.exit(0);