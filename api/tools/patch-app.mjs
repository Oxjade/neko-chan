import { readFileSync, writeFileSync } from "node:fs";

const A = "/home/carnage/tradebotpro/frontend/webroot/_next/static/chunks/pages/_app-b324cc13ef8b46d9.js";
const BACKUP = "/home/carnage/tradebotpro/api/tools/_app.pristine-backup.js";

let t = readFileSync(A, "utf8");
const first = t.slice(0, 3000000);

// The exact atom:  let N=(0,v.Un)(),M={supportedChains:N, ...}
const reAtom = hist => {
  const i = hist.indexOf("let N=(0,v.Un)(),M={");
  return i;
};

const i = first.indexOf("let N=(0,v.Un)(),M={");
if (i === -1) {
  console.log("ATOM-NOT-FOUND");
  process.exit(1);
}
console.log("atom @", i);
console.log("before:", t.slice(i - 40, i + 120));

const patched =
  t.slice(0, i) +
  'let N=["sui"],M={supportedChains:N,defaultChains:["sui"]}' +
  t.slice(i + 'let N=(0,v.Un)(),M={supportedChains:N,defaultChains:v.Nt.filter(g=>N.includes(g))}'.length);

writeFileSync(BACKUP, readFileSync(A)); // pristine backup first (only on success path below)
writeFileSync(A, patched);

// verify
const verify = readFileSync(A, "utf8");
console.log("\npatched region:", verify.slice(i - 30, i + 140));
const stillHas = verify.includes('defaultChains:[A.lg.Sol');
console.log("other scope defaultChains w/ Sol still present (GlobalCallout/Trending):", stillHas);
console.log("backup written:", BACKUP, readFileSync(BACKUP).length, "bytes");