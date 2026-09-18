import WebSocket from "file:///usr/share/nodejs/ws/index.js";
const CDR = "http://127.0.0.1:9333";
const want = "0x4d671396980c41e02cbeda518e48c9127ccd177a4db3b9aa2e5b17763aa24f67";
const tabs = await (await fetch(CDR + "/json/list")).json();
const tab = tabs.find((t: any) => t.type === "page" && t.url.includes("/sol/token/" + want)) ?? tabs.find((t: any) => t.type === "page");
if (!tab) { console.log("NO-TAB"); process.exit(2); }
const ws = new WebSocket(tab.webSocketDebuggerUrl);
let mid = 0; const pend = new Map();
const send = (m: string, p: any = {}) => new Promise((res, rej) => { const i = ++mid; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method: m, params: p })); });
ws.on("message", (d: Buffer) => { const m = JSON.parse(d.toString()); if (m.id && pend.has(m.id)) { const h = pend.get(m.id)!; pend.delete(m.id); m.error ? h.rej(new Error(m.error.message)) : h.res(m.result); } });
await new Promise(r => ws.on("open", r));
await send("Runtime.enable");
await new Promise(r => setTimeout(r, 3000));
const { result } = await send("Runtime.evaluate", {
  expression: `(() => {
    const bt = document.body ? document.body.innerText : "";
    const lines = bt.split("\\n").map(s => s.trim()).filter(Boolean);
    return {
      title: document.title,
      url: location.href.slice(0, 110),
      h1: (document.querySelector("h1")?.innerText ?? "").slice(0, 80),
      blubHeader: lines.filter(l => /BLUB|BLUB|BLUB|Neko/i.test(l)).slice(0, 6),
      dollarLine: lines.filter(l => /\\$/.test(l)).slice(0, 4),
      solLine: lines.filter(l => /sol|SOL/i.test(l)).slice(0, 3),
      totalLines: lines.length,
      first12: lines.slice(0, 12)
    };
  })()`,
  returnByValue: true
});
console.log(JSON.stringify(result.value, null, 1));
ws.close(); process.exit(0);
