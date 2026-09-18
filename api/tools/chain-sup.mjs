import { readFileSync } from "node:fs";
const A = "/home/carnage/tradebotpro/frontend/webroot/_next/static/chunks/pages/_app-b324cc13ef8b46d9.js";
const t = readFileSync(A, "utf8");
const i = t.indexOf("supportedChains:[");
console.log("index:", i);
console.log("=== 1100 chars from i-160 ===");
console.log(t.slice(i - 160, i + 940));