import { readFile } from "fs/promises";
import { join } from "path";
const B = "/home/carnage/tradebotpro/frontend/webroot";

const idx = await readFile(join(B, "index.html"), "utf8");
const main = (await (await import("fs/promises")).readdir(join(B, "_next/static/chunks"))).find((f) => f.startsWith("main-") && f.endsWith(".js"));
const mainText = await readFile(join(B, "_next/static/chunks", main), "utf8");

console.log(JSON.stringify({
  webroot: B,
  index_html: {
    title: (idx.match(/<title>[^<]*/) || ["<title>?"])[0],
    gmgnWords: (idx.match(/gmgn/gi) || []).length,
    nekoCount: (idx.match(/Neko/gi) || []).length,
    hasNekoTagline: idx.includes("The Fastest") ? "tagline present" : "check"
  },
  patchedMainChunk: {
    file: main,
    bytes: mainText.length,
    has8790WSPatch: mainText.includes("127.0.0.1:8790")
  },
  servedFilesCheck: {
    indexHtml: (await (async () => { try { await (await import("fs/promises")).stat(join(B, "index.html")); return "OK"; } catch { return "MISSING"; } })(),)
  }
}, null, 1));
