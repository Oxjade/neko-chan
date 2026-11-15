// Two static pages (the terminal and the week's calls) and nothing else.
// During local dev and preview, /api/fx is answered by the same handler that
// Vercel runs as a function, so the forex board can be tried without deploying.

import { resolve } from "node:path";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

type Next = () => void;

function localFunctions(): Plugin {
  const handle = async (req: IncomingMessage, res: ServerResponse, next: Next) => {
    if (!req.url || req.url.split("?")[0] !== "/api/fx") return next();
    try {
      const { GET } = await import("./api/fx.ts");
      const response: Response = await GET(new Request(`http://localhost${req.url}`));
      res.statusCode = response.status;
      response.headers.forEach((value, key) => res.setHeader(key, value));
      res.end(Buffer.from(await response.arrayBuffer()));
    } catch (error) {
      res.statusCode = 500;
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ error: String(error) }));
    }
  };
  return {
    name: "neko-local-functions",
    configureServer(server) { server.middlewares.use(handle); },
    configurePreviewServer(server) { server.middlewares.use(handle); },
  };
}

export default defineConfig({
  plugins: [localFunctions()],
  build: {
    target: "es2022",
    rollupOptions: {
      input: {
        terminal: resolve(import.meta.dirname, "index.html"),
        week: resolve(import.meta.dirname, "week.html"),
      },
    },
  },
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});
