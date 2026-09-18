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
    const cut = ["/trade","/follow","/perpetual","/ai","/copytrade","/bot","/call","/refer","/kol","/contest"];
    const found = [];
    document.querySelectorAll("a[href]").forEach(a => {
      const path = (a.getAttribute("href")||"").split("?")[0];
      if (cut.some(c => path === c || path.startsWith(c + "/") || path.startsWith(c + "."))) {
        const r = a.getBoundingClientRect();
        found.push({ path: path.slice(0,28), label: (a.innerText||a.getAttribute("aria-label")||"").trim().slice(0,16), w: Math.round(r.width), h: Math.round(r.height), display: getComputedStyle(a).display, inHeader: !!a.closest("div.header, div[class*=header], header") });
      }
    });
    const keepFound = [];
    ["/trend","/portfolio","/monitor","/rewards","/watchlists"].forEach(c => {
      document.querySelectorAll("a[href^='/" + c + "']").forEach(a => {
        const r = a.getBoundingClientRect();
        if (r.width > 5 && r.height > 5 && !keepFound.some(k => k.dup)) keepFound.push({ path: (a.getAttribute("href")||"").slice(0,28), label: (a.innerText||"").trim().slice(0,16) });
      });
    });
    return { cutAtomsFound: found, keepNavStillVisible: keepFound.slice(0,6) };
  })()`, returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close(); process.exit(0);