import http from "node:http";
import { readFile } from "node:fs/promises";
import { join, extname } from "node:path";
const ROOT = "/home/carnage/tradebotpro/frontend/webroot";
const MIME = { ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".mjs": "application/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".ico": "image/x-icon", ".woff2": "font/woff2", ".woff": "font/woff", ".txt": "text/plain", ".map": "application/json" };
const ok = async (p) => { try { return (await import("node:fs/promises")).stat(p).then((s) => s.isFile()); } catch { return false; } };
http.createServer(async (req, res) => {
  try {
    const u = new URL(req.url, "http://x");
    const path = decodeURIComponent(u.pathname);
    const asset = path.startsWith("/_next/") || path.startsWith("/static/") || path === "/favicon.ico" || path === "/robots.txt" || path === "/sitemap.xml" || path === "/llms.txt";
    let file = join(ROOT, path);
    if (!asset && !(await ok(file))) return serve(res, join(ROOT, "index.html"));
    if (!(await ok(file))) { res.writeHead(404); return res.end("404"); }
    return serve(res, file);
  } catch (e) { res.writeHead(500); res.end("err " + (e && e.message)); }
  function serve(res, file) {
    const body = readFile(file);
    body.then((b) => { res.writeHead(200, { "content-type": MIME[extname(file)] || "application/octet-stream", "cache-control": file.endsWith("index.html") ? "no-cache" : "public, max-age=31536000, immutable" }); res.end(b); })
       .catch(() => { res.writeHead(500); res.end("spa err"); });
  }
}).listen(8899, "127.0.0.1", () => console.log("SPA :8899 -> " + ROOT));
