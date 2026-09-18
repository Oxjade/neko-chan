import { readFileSync, writeFileSync } from "node:fs";
const A = "/home/carnage/tradebotpro/frontend/webroot/_next/static/chunks/pages/_app-b324cc13ef8b46d9.js";
const MODE = process.argv[2] || "snapshot";

if (MODE === "snapshot") {
  writeFileSync("/home/carnage/tradebotpro/api/tools/_app.true-pristine.js", readFileSync(A));
  console.log("snapshot of current (patched) saved -> tools/_app.true-pristine.js");
} else if (MODE === "restore-pristine") {
  let t = readFileSync(A, "utf8");
  const s1 = '_.Arbitrum="arbitrum",_.Sui="sui",_)';
  const s2 = 'let N=["sui"],M={supportedChains:N,defaultChains:N}';
  let ok = true;
  if (t.includes(s1)) t = t.replace(s1, '_.Arbitrum="arbitrum",_)'); else { console.log("s1 not present (maybe already pristine)"); ok = false; }
  if (t.includes(s2)) t = t.replace(s2, 'let N=(0,v.Un)(),M={supportedChains:N,defaultChains:v.Nt.filter(g=>N.includes(g))}'); else { console.log("s2 not present (maybe already pristine)"); ok = false; }
  writeFileSync(A, t);
  const chk = readFileSync(A, "utf8");
  console.log("restore done. enumSui:", chk.includes('_.Sui="sui"'), "| storeForced:", chk.includes('let N=["sui"]'));
} else if (MODE === "verify") {
  const t = readFileSync(A, "utf8");
  console.log("enumSui:", t.includes('_.Sui="sui"'), "| storeForced:", t.includes('let N=["sui"]'), "| defaultChainsN:", t.includes('defaultChains:N'));
}