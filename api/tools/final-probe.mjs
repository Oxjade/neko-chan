import { readFile, stat } from "fs/promises";
import { join } from "path";
const B = "/home/carnage/tradebotpro/frontend/webroot";
const probe = async (rel) => {
  try {
    const s = await stat(join(B, rel));
    return s.isFile() ? `OK ${s.size}b` : "DIR";
  } catch {
    return "MISSING";
  }
};
const idx = await readFile(join(B, "index.html"), "utf8");
const main = await readFile(join(B, "_next/static/chunks/main-0094871bdfa5d30d.js"), "utf8");
const hasPatch = main.includes("127.0.0.1:8790");
const tokenShell = await (async () => {
  const dirs = ["pages/sol", "webroot/pages/sol"];
  for (const d of dirs) {
    try {
      const list = (await import("fs/promises")).readdir(join(B, d));
      (await list).forEach(async (f) => {});
    } catch {}
  }
  return null;
})();
console.log(JSON.stringify({
  indexHtml: {
    hasNeko: idx.includes("Neko | The Fastest Sui Meme Trading Terminal"),
    gmgnWordCount: (idx.toLowerCase().match(/gmgn/g) || []).length,
    nekoCount: (idx.match(/Neko/gi) || []).length,
    title: (idx.match(/<title>[^<]*/ ) || ["<title>?"])[0]
  },
  patchedMain: { has8790WS: hasPatch, size: main.length },
  bluntProbe: "static-file proofs only (no browser) "
}, null, 1));
