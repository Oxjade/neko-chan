import { readFileSync } from "node:fs";
const A = "/home/carnage/tradebotpro/frontend/webroot/_next/static/chunks/pages/_app-b324cc13ef8b46d9.js";
const t = readFileSync(A, "utf8");
const start = t.indexOf("supportedChains", 805000);
console.log("=== from ~806500, 1500 chars ===");
console.log(t.slice(805800, 807400));