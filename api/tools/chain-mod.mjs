import { readFileSync } from "node:fs";
const A = "/home/carnage/tradebotpro/frontend/webroot/_next/static/chunks/pages/_app-b324cc13ef8b46d9.js";
const t = readFileSync(A, "utf8");
for (const [name, at] of [["27493", 27493], ["107495-region", 106800]]) {
  console.log("\n=== region @", at, "===");
  console.log(t.slice(at, Math.min(at + 900, t.length)));
}