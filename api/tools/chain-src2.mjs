import { readFileSync } from "node:fs";
const A = "/home/carnage/tradebotpro/frontend/webroot/_next/static/chunks/pages/_app-b324cc13ef8b46d9.js";
const t = readFileSync(A, "utf8");
const iAll = t.indexOf("allValues", 3100000);
const start = t.lastIndexOf("enabledChains", iAll);
console.log("enabledChains @", start, "| allValues @", iAll);
console.log("\n=== window: enabledChains ... allValues (first 1400 chars of it) ===");
console.log(t.slice(start - 120, Math.min(start + 1400, iAll + 300)));