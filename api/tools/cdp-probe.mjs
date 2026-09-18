// Headless-Chromium CDP probe: load a mirror route, capture every network
// request to app namespaces + console errors + a screenshot.
// usage: node tools/cdp-probe.mjs <path> [waitMs]
import fs from "node:fs";

const raw = process.argv[2] ?? "/trenches";
const target = raw.startsWith("http") ? raw : `http://127.0.0.1:8790${raw}`;
const waitMs = Number(process.argv[3] ?? 12000);
const PORT = 9333;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function newTab() {
  const r = await fetch(
    `http://127.0.0.1:${PORT}/json/new?${encodeURIComponent("about:blank")}&latest`,
    { method: "PUT" },
  );
  return r.json();
}

const { default: WS } = await import("ws");
const tab = await newTab();
const ws = new WS(tab.webSocketDebuggerUrl, { perMessageDeflate: false });
let id = 0;
const pending = new Map();
const reqs = [];
const errs = [];
function send(method, params = {}) {
  return new Promise((res, rej) => {
    const mid = ++id;
    pending.set(mid, { res, rej });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
}
ws.on("message", (d) => {
  const m = JSON.parse(d.toString());
  if (m.id && pending.has(m.id)) {
    const p = pending.get(m.id);
    pending.delete(m.id);
    m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
    return;
  }
  if (m.method === "Network.webSocketCreated") {
    errs.push(`WS-URL ${m.params.url}`);
  }
  if (m.method === "Network.webSocketFrameError") {
    errs.push(`WS-ERR ${m.params.response?.errorCode ?? "?"} @${(m.params.url ?? "").slice(0, 90)}`);
  }
  if (m.method === "Network.requestWillBeSent") {
    const u = new URL(m.params.request.url);
    if (/^\/(api\/v1|mrwapi|rrs|account|wallet-api|defi|oauth)/.test(u.pathname))
      reqs.push({
        m: m.params.request.method,
        p: u.pathname,
        q: Object.fromEntries(u.searchParams),
        body: (m.params.request.postData ?? "").slice(0, 1200),
      });
  }
  if (m.method === "Runtime.consoleAPICalled" && /error|warning/.test(m.params.type)) {
    const txt = (m.params.args ?? []).map(a => a.description ?? a.value).join(" ").slice(0, 200);
    if (/not iterable/.test(txt)) {
      const st = (m.params.stackTrace?.callFrames ?? []).slice(0, 6)
        .map(f => `${f.functionName}@${f.url.split("/").pop()}:${f.lineNumber + 1}`).join(" <- ");
      errs.push(`ITER-STACK ${txt.slice(0,80)} || ${st}`);
    }
  }
  if (m.method === "Log.entryAdded") {
    const en = m.params.entry;
    if (en.level === "error" && /TypeError|is not iterable/.test(en.text ?? ""))
      errs.push(`LOG ${en.text.slice(0, 120)} @ ${en.url ?? "?"}:${en.lineNumber ?? "?"}`);
  }
  if (m.method === "Runtime.exceptionThrown") {
    const d = m.params.exceptionDetails;
    const stack = (d.exception?.stack ?? d.stack ?? "").split("\n").slice(0, 5).join(" << ");
    errs.push(((d.exception?.description ?? "exc").split("\n")[0]) + " || " + stack);
  }
  if (m.method === "Console.messageAdded" && m.params.message.level === "error")
    errs.push(m.params.message.text.slice(0, 200));
});

await new Promise((r) => ws.on("open", r));
await send("Network.enable");
await send("Log.enable");
await send("Runtime.enable");
await send("Page.enable");
await send("Page.navigate", { url: target });
await sleep(waitMs);
const shot = "/tmp/probe.png";
const { data } = await send("Page.captureScreenshot", { format: "png" });
fs.writeFileSync(shot, Buffer.from(data, "base64"));
const seen = new Map();
for (const r of reqs) seen.set(`${r.m} ${r.p}`, r);
fs.writeFileSync("/tmp/probe-reqs.json", JSON.stringify([...seen.values()], null, 1));
console.log(`${reqs.length} api calls, ${seen.size} distinct:`);
for (const r of seen.values())
  console.log(
    " ",
    r.m,
    r.p,
    r.q && Object.keys(r.q).length ? JSON.stringify(r.q).slice(0, 160) : "",
    r.body ? "BODY " + r.body.slice(0, 260) : "",
  );
console.log("errors:\n  " + errs.slice(0, 6).join("\n  ") + "\n");
console.log(`screenshot: ${shot}`);
ws.close();
process.exit(0);
