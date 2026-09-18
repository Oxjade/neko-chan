import http from "http";
import { readFile, stat } from "fs/promises";
import { join, extname } from "path";

const ROOT = "/home/carnage/tradebotpro/frontend/webroot";
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".webp": "image/webp",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".ico": "image/x-icon",
  ".txt": "text/plain"
};

const isFile = async (f) => { try { return (await stat(f)).isFile(); } catch { return false; } };

http.createServer(async (req, res) => {
  try {
    const u = new URL(req.url, "http://x");
    const p = decodeURIComponent(u.pathname);
    const isAsset = p.startsWith("/_next/") || p.startsWith("/static/") || extname(p) !== "";
    let file = join(ROOT, p);
    if (!isAsset && !(await isFile(file))) file = join(ROOT, "index.html");
    const body = await readFile(file);
    res.writeHead(200, {
      "content-type": MIME[extname(file)] || "application/octet-stream",
      "cache-control": isAsset ? "public, max-age=3600" : "no-cache",
      "x-neko-doc": "served from " + file
    });
    res.end(body);
  } catch (e) {
    res.writeHead(500); res.end("err " + e.message);
  }
}).listen(8899, "127.0.0.1", () => console.log("SPA :8899 ROOT=" + ROOT));
