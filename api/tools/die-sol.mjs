const CDR = "http://127.0.0.1:9333";
const tabs = await (await fetch(CDR + "/json/list")).json();
const tab = tabs.find((t) => t.type === "page");
if (!tab) { console.log("NO-PAGE"); process.exit(2); }
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(m.error.message)) : res(m.result); } };
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); });
await send("Runtime.enable");
const r = await send("Runtime.evaluate", {
  expression: `(() => {
    const bt = document.body ? document.body.innerText : "";
    const solWord = (bt.match(/\\bSOL\\b/g) || []).length;
    const links = [...document.querySelectorAll("a[href]")].map((a) => a.getAttribute("href")).filter(Boolean);
    const chainLinks = links.filter((h) => /\\/sol\\/|\\/sui\\/|\\/eth\\/|\\/base\\/|\\/bsc\\//.test(h)).slice(0, 12);
    return {
      url: location.href.slice(0, 130),
      solWordCount: solWord,
      chainHrefs: chainLinks,
      allHrefSample: links.slice(0, 8),
      bodyHead: bt.replace(/\\n{2,}/g, " | ").slice(0, 260)
    };
  })()`,
  returnByValue: true
});
console.log(JSON.stringify(r.value, null, 1));
ws.close();
process.exit(0);