import { readFileSync } from "node:fs";
const A = "/home/carnage/tradebotpro/frontend/webroot/_next/static/chunks/pages/_app-b324cc13ef8b46d9.js";
const t = readFileSync(A, "utf8");

// Show the exact bytes where the config table P starts: the M = {supportedChains:N, defaultChains:...}
const marker = "bindingPolicy";
// find the moment M {} default with Global
const iM = t.indexOf("{supportedChains:N,");
console.log("M-table origin @", iM);
console.log("--- 300 bytes from iM ---\n");
console.log(t.slice(iM - 120, iM + 320));