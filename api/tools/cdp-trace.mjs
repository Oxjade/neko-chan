// Pause on the uncaught TypeError and report the throwing frame + suspect call.
import fs from "node:fs";
const target = process.argv[2] ?? "http://127.0.0.1:8790/sui/token/x";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PORT = 9333;
const tab = await (
  await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(target)}`, { method: "PUT" })
).json();
const { default: WS } = await import("ws");
const ws = new WS(tab.webSocketDebuggerUrl);
let id = 0;
const pending = new Map();
const send = (m, p = {}) =>
  new Promise((res, rej) => {
    const mid = ++id;
    pending.set(mid, { res, rej });
    ws.send(JSON.stringify({ id: mid, method: m, params: p }));
  });
let paused = false;
ws.on("message", async (d) => {
  const m = JSON.parse(d.toString());
  if (m.id && pending.has(m.id)) {
    const p = pending.get(m.id);
    pending.delete(m.id);
    m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
    return;
  }
  if (m.method === "Debugger.paused" && !paused) {
    if (m.params.reason !== "TypeError") {
      try { await send("Debugger.resume"); } catch {}
      return;
    }
    paused = true;
    const f = m.params.callFrames[0];
    console.log("EXCEPTION:", JSON.stringify(m.params.reason), m.params.data?.description?.slice(0, 160));
    console.log("FRAME:", f.functionName, f.url, ":", f.lineNumber + 1, ":", f.columnNumber + 1);
    console.log("STACK:", m.params.callFrames.slice(0, 6).map(x => `${x.functionName}@${x.url.split("/").pop()}:${x.lineNumber + 1}`).join(" <- "));
    for (const sc of (f.scopeChain ?? [])) {
      if (sc.type === "local" && sc.object?.objectId) {
        try {
          const r = await send("Runtime.getProperties", { objectId: sc.object.objectId });
          for (const pr of r.result.filter(x => ["g", "b", "y", "t", "e"].includes(x.name)))
            console.log("  scope", pr.name, "=", JSON.stringify(pr.value?.description ?? pr.value?.value ?? pr.type)?.slice(0, 300));
        } catch {}
      }
    }
    await send("Debugger.resume");
    setTimeout(async () => {
      await fetch(`http://127.0.0.1:${PORT}/json/close/${tab.id}`);
      process.exit(0);
    }, 2500);
  }
});
await new Promise((r) => ws.on("open", r));
await send("Debugger.enable");
await send("Debugger.setPauseOnExceptions", { state: "uncaught" });
await send("Page.enable");
await send("Page.navigate", { url: target });
await sleep(45000);
console.log(paused ? "" : "no uncaught exception captured");
process.exit(0);
