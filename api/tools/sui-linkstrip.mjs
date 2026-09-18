const CDR = "http://127.0.0.1:9333";
const tabs = (await (await fetch(CDR + "/json/list")).json()).filter((t) => t.type === "page" && t.url.includes("8790"));
const tab = tabs[0];
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws")); });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error("timeout")); } }, 20000); });
await send("Runtime.enable");
await send("Page.navigate", { url: "http://127.0.0.1:8790/" });
await new Promise((r) => setTimeout(r, 16000));
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    // 1) assert the guard script is loaded
    const guardLoaded = [...document.scripts].some(s => s.textContent.includes("NK-LINKHOOD") || s.textContent.includes("chain=sol"));
    // 2) find the /trend nav link, simulate the handler's rewrite decision
    const trendA = [...document.querySelectorAll("a[href^='/trend?chain=sol']:not([href^='/trend?chain=sol'])")].length; // intentionally 0-match sanity
    const trendA2 = [...document.querySelectorAll("a[href]")].find(a => (a.getAttribute("href")||"").startsWith("/trend?chain=sol") && a.getBoundingClientRect().width > 0);
    let afterRewrite = null;
    if (trendA2) {
      const h = trendA2.getAttribute("href").replace("?chain=sol","").replace("&chain=sol","");
      afterRewrite = h;
    }
    // 3) inspect pending hrefs with chain=sol among visible nav links
    const stillSol = [...document.querySelectorAll("a[href]")].filter(a => (a.getAttribute("href")||"").includes("chain=sol") && a.getBoundingClientRect().width > 0).map(a => a.getAttribute("href"));
    return { guardLoaded, trendHrefFound: !!trendA2, afterRewrite, stillSolVisible: stillSol.slice(0,6) };
  })()`, returnByValue: true
});
console.log(JSON.stringify(r?.result?.value ?? r, null, 1));
ws.close(); process.exit(0);