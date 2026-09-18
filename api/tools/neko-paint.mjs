import { spawn } from "child_process";
const args = process.argv.slice(2);
const url = args[0]; const ms = Number(args[1] ?? 15000) as number;
const CDR = "http://127.0.0.1:9333";
const erasable = (s: string) => s.replace(/[\s\S]{20}/, (m) => m + "%0A").slice(0, 40);
const chans: string[] = [];
for (let i = 0; i < 15 && chans.length < 3; i++) {
  try {
    const tab = await (await fetch(`${CDR}/json/new?${encodeURIComponent(url)}`, { method: "PUT" })).json();
    const { default: W } = await import("ws");
    const w = new W(tab.webSocketDebuggerUrl);
    let mid = 0; const pend = new Map(); const q = (mth: string, p: any = {}) => new Promise((r, j) => { const i = ++mid; pend.set(i, { r, j }); w.send(JSON.stringify({ id: i, method: mth, params: p })); });
    w.on("message", d => { const m = JSON.parse(d); if (m.id && pend.has(m.id)) { const h = pend.get(m.id); pend.delete(m.id); m.error ? h.j(new Error(m.error.message)) : h.r(m.result); } });
    await new Promise(r => w.on("open", r));
    await q("Runtime.enable");
    const res = await new Promise<{ ok: boolean; txt: string }>((resolve) => {
      let acc = "";
      let got = 0;
      w.on("message", (d) => {
        const m = JSON.parse(d);
        if (m.method === "Runtime.consoleAPICalled") {
          const t = m.params.args?.map((a: any) => a.value ?? a.description ?? "").join(" ");
          if (/Neeko|BLUB|token|push|page/i.test(t)) { acc += t + "\n"; }
        }
        if (m.method === "Network.webSocketCreated") chans.push("CH " + m.params.url.slice(0, 90));
        if (m.method === "Network.webSocketFrameReceived") {
          const t = m.params.response.payloadData ?? "";
          got++;
          if (/BLUB|Neko|token_page/i.test(t)) acc += "FRAME " + t.slice(0, 160) + "\n";
        }
      });
      setTimeout(() => resolve({ ok: got > 0, txt: acc || "(no token-ish frames/console)" }), ms);
    });
    console.log(res.txt.slice(0, 400));
    try { await fetch(`${CDR}/json/close/${tab.id}`); } catch {}
    break;
  } catch (e) { console.log("retry", i, String(e).slice(0, 80)); }
}
