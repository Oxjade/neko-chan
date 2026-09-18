import { readFileSync, writeFileSync } from "node:fs";
const A = "/home/carnage/tradebotpro/frontend/webroot/_next/static/chunks/pages/_app-b324cc13ef8b46d9.js";
const PRISTINE = "/home/carnage/tradebotpro/api/tools/_app.true-pristine.js";

const t = readFileSync(A, "utf8");

// 1) reverse enum 349289 Sui addition
const enumPatched = '_.Arbitrum="arbitrum",_.Sui="sui",_)';
const enumPristine = '_.Arbitrum="arbitrum",_)';
// 2) reverse store force
const storePatched = 'let N=["sui"],M={supportedChains:N,defaultChains:N}';
const storePristine = 'let N=(0,v.Un)(),M={supportedChains:N,defaultChains:v.Nt.filter(g=>N.includes(g))}';

let pristine = t;
let ok = true;
if (!pristine.includes(enumPatched)) { console.log("ENUM-PATCH missing -> cannot reconstruct pristine"); ok = false; }
if (!pristine.includes(storePatched)) { console.log("STORE-PATCH missing -> cannot reconstruct pristine"); ok = false; }
if (ok) {
  pristine = pristine.replace(enumPatched, enumPristine).replace(storePatched, storePristine);
  writeFileSync(PRISTINE, pristine);
  console.log("PRISTINE RECONSTRUCTED + SAVED:", PRISTINE);
  console.log("  bytes:", pristine.length);
  console.log("  enumSui present:", pristine.includes("_.Sui=\"sui\""), "(should be False)");
  console.log("  storeForced present:", pristine.includes('let N=["sui"]'), "(should be False)");
  console.log("  atom restored:", pristine.includes("let N=(0,v.Un)(),M={supportedChains:N"));
}
console.log("\n-- node --check on reconstructed pristine --");