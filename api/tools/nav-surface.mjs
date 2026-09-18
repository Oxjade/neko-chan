import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const W = "/home/carnage/tradebotpro/frontend/webroot";
const CHUNKS = join(W, "_next/static/chunks");

const navWords = ["trenches", "sniping", "copy", "kol", "referral", "contest", "devSnipe", "skyTrade", "tokenSnipe", "trade/copy"];

const files = readdirSync(CHUNKS).filter((f) => f.endsWith(".js") && statSync(join(CHUNKS, f)).size > 2000 && statSync(join(CHUNKS, f)).size < 3000000);

const scored = [];
for (const f of files) {
  let t;
  try { t = readFileSync(join(CHUNKS, f), "utf8"); } catch { continue; }
  const hits = navWords.filter((w) => t.toLowerCase().includes(w.toLowerCase()));
  if (hits.length >= 3) {
    const routes = (t.match(/["'`]\/[a-z]+\/[a-zA-Z.[\]]*["'`]/g) || []).filter((r) => !r.includes("_next") && !r.includes("static")).slice(0, 10);
    scored.push({ file: f, size: t.length, hits, samplePaths: routes });
  }
}
console.log(JSON.stringify(scored.slice(0, 8), null, 1));

console.log("\n=== buildManifest locations ===");
try {
  const bm = readFileSync(join(W, "_buildManifest.js"), "utf8");
  console.log("root _buildManifest.js found, len", bm.length);
  const routes = bm.match(/"\/(?:[^"]*)"/g) || [];
  console.log("routes count:", routes.length);
  console.log(routes.slice(0, 40).join("\n"));
} catch {
  const f = (await import("node:fs/promises")).readdir(W);
  (await f).forEach((e) => {
    if (e.includes("buildManifest") || e.includes("manifest")) console.log("  cand:", e);
  });
}