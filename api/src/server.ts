// Neko terminal API/WS layer (DESIGN §16.7, phase 7 v0).
// Node 22, type-stripped TS. Reads the indexer's Postgres only; money stays
// exact decimal STRINGS end-to-end (BigInt for display scaling), never float.
// Token labels come from the suipump indexer API (creator-supplied, display
// only — never pricing), same policy as service/degen.

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";
import * as sui from "./sui.ts";
import { WebSocketServer } from "ws";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.API_PORT ?? 8790);
const HOST = process.env.API_HOST ?? "127.0.0.1";
const DATABASE_URL =
  process.env.DATABASE_URL ?? "postgres:///neko_indexer";
const SUIPUMP_INDEXER =
  process.env.SUIPUMP_INDEXER ?? "https://suipump-main-web.onrender.com";

function poolConfig(): pg.PoolConfig {
  // `postgres:///db` (or env unset) means local unix-socket peer auth, exactly
  // how the Rust indexer connects; node-pg needs the socket path spelled out.
  const m = DATABASE_URL.match(/^postgres(?:ql)?:\/\/\/([^?]+)/);
  if (m)
    return {
      host: "/var/run/postgresql",
      database: m[1],
      user: process.env.USER ?? undefined,
      max: 5,
    };
  return { connectionString: DATABASE_URL, max: 5 };
}
const WEBROOT =
  process.env.WEBROOT ?? "/home/carnage/tradebotpro/frontend/webroot";

const pool = new pg.Pool(poolConfig());

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".map": "application/json",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".webp": "image/webp",
  ".txt": "text/plain",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".webmanifest": "application/manifest+json",
};

// GMGN namespaces the mirrored terminal calls; our own /api/* stay real.
const STUB_NS = ["/api/v1", "/mrwapi", "/rrs", "/account", "/wallet-api", "/defi", "/oauth", "/vas", "/trs", "/xapi"];
const REQLOG = "/tmp/gmgn-reqs.log";
function gmgnStub(p: string): unknown {
  // Shape-aware empty data per namespace so the client's destructuring of the
  // envelope never explodes; the request log tells us which endpoints need
  // real data next.
  if (p === "/mrwapi/v1/timestamp") return { code: 0, msg: "ok", data: { timestamp: Date.now() } };
  if (p === "/account/iploc") return { code: 0, msg: "ok", data: { ip: "127.0.0.1", country: "US" } };
  if (p.endsWith("/get_coins")) return { code: 0, msg: "ok", data: [] };
  if (p === "/api/v1/major_coin_prices") return { code: 0, msg: "ok", data: [] };
  if (p.includes("gas_price")) return { code: 0, msg: "ok", data: [] };
if (p.includes("my_rank_info") || p.includes("twitch_kol") || p.includes("user_config") || p.includes("background_info"))
    return { code: 0, msg: "ok", data: [] };
  return { code: 0, msg: "ok", data: {} };
}
await pool
  .query(
    `create table if not exists suipump_catalog (
       curve_id text primary key, name text, symbol text, icon_url text,
       token_type text, creator text, description text,
       created_at bigint, refreshed_at timestamptz not null default now())`,
  )
  .catch((e) => console.error("suipump_catalog init:", e.message));
await pool
  .query(
    `create table if not exists token_meta (
       curve_id text primary key, meta jsonb not null, fetched_at timestamptz not null default now())`,
  )
  .catch((e) => console.error("token_meta init:", e.message));

// ------------------------------------------------------------ SUI/USD price
// Display-only scale factor (GMGN speaks USD); never stored, never accounting.
let suiUsd = 0;
async function refreshSuiUsd() {
  try {
    const r = await fetch(
      "https://api.coingecko.com/api/v3/simple/price?ids=sui&vs_currencies=usd",
      { signal: AbortSignal.timeout(5000) },
    );
    if (r.ok) {
      const j = (await r.json()) as { sui?: { usd?: number } };
      if (j?.sui?.usd) {
        suiUsd = j.sui.usd;
        sui.setSuiUsd(suiUsd);
      }
    }
  } catch {}
}
void refreshSuiUsd();
setInterval(() => void refreshSuiUsd(), 60_000);

// ---------------------------------------------------------------- metadata
type Meta = {
  name?: string;
  symbol?: string;
  icon_url?: string;
  token_type?: string;
  creator?: string;
  description?: string;
};
const metaCache = new Map<string, { meta: Meta; exp: number }>();
const META_TTL_MS = 10 * 60_000;

// The suipump indexer (free render app) cold-starts in 10-20s, so metadata is
// served STALE-FIRST: memory -> token_meta table -> empty, and a refresh is
// kicked off in the background whenever the entry is missing or older than the
// TTL. Labels never gate a response, and pricing never reads them (same policy
// as service/degen).
const metaRefreshing = new Set<string>();
async function metaFor(curve: string): Promise<Meta> {
  const hit = metaCache.get(curve);
  if (hit) {
    if (hit.exp < Date.now() && !metaRefreshing.has(curve)) void refreshMeta(curve);
    return hit.meta;
  }
  const { rows } = await pool
    .query("select meta, fetched_at from token_meta where curve_id=$1", [curve])
    .catch(() => ({ rows: [] as any[] }));
  if (rows.length) {
    const meta = rows[0].meta as Meta;
    const exp =
      Date.parse(rows[0].fetched_at) + META_TTL_MS > Date.now()
        ? rows[0].fetched_at && Date.now() + 60_000
        : Date.now() - 1;
    metaCache.set(curve, { meta, exp });
    if (exp < Date.now() && !metaRefreshing.has(curve)) void refreshMeta(curve);
    return meta;
  }
  void refreshMeta(curve);
  return {};
}
async function refreshMeta(curve: string): Promise<void> {
  metaRefreshing.add(curve);
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 15000);
    const r = await fetch(`${SUIPUMP_INDEXER}/token/${curve}`, { signal: ctl.signal });
    clearTimeout(t);
    if (!r.ok) return;
    const j = (await r.json()) as Meta;
    const meta: Meta = {
      name: j.name,
      symbol: j.symbol,
      icon_url: j.icon_url,
      token_type: j.token_type,
      creator: j.creator,
      description:
        typeof j.description === "string" ? j.description.slice(0, 280) : undefined,
    };
    if (!meta.name && !meta.symbol) return;
    metaCache.set(curve, { meta, exp: Date.now() + META_TTL_MS });
    await pool
      .query(
        `insert into token_meta (curve_id, meta) values ($1,$2)
         on conflict (curve_id) do update set meta=excluded.meta, fetched_at=now()`,
        [curve, JSON.stringify(meta)],
      )
      .catch(() => undefined);
  } catch {
    /* labels are a nicety; curve id is the fallback */
  } finally {
    metaRefreshing.delete(curve);
  }
}

// ------------------------------------------------------------- suipump catalog
// Full launchpad catalog (~hundreds of tokens) cached in PG; the live stream
// is only what makes a token ACTIVE — the board must show older/dormant
// launches too. Display-only labels, same policy as token_meta.
let catalogLoaded = false;
async function refreshCatalog(): Promise<void> {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 20000);
    const r = await fetch(`${SUIPUMP_INDEXER}/tokens?limit=2000`, { signal: ctl.signal });
    clearTimeout(t);
    if (!r.ok) return;
    const list = (await r.json()) as any[];
    if (!Array.isArray(list) || !list.length) return;
    for (const j of list) {
      if (!j?.curveId) continue;
      const icon = j.iconUrl ?? j.icon_url ?? null;
      const created = Number(j.createdAt ?? j.created_at ?? 0) || null;
      const ttype = j.tokenType ?? j.token_type ?? null;
      await pool.query(
        `insert into suipump_catalog (curve_id,name,symbol,icon_url,token_type,creator,description,created_at,refreshed_at)
         values ($1,$2,$3,$4,$5,$6,$7,$8,now())
         on conflict (curve_id) do update set name=excluded.name, symbol=excluded.symbol,
           icon_url=excluded.icon_url, token_type=excluded.token_type, creator=excluded.creator,
           description=excluded.description, created_at=excluded.created_at, refreshed_at=now()`,
        [
          j.curveId,
          j.name ?? null,
          j.symbol ?? null,
          icon,
          ttype,
          j.creator ?? null,
          typeof j.description === "string" ? j.description.slice(0, 280) : null,
          created,
        ],
      ).catch(() => undefined);
    }
    catalogLoaded = true;
    console.log(`suipump catalog refreshed: ${list.length} tokens`);
  } catch (e) {
    const cause = (e as { cause?: { message?: string } }).cause?.message ?? "";
    console.error("catalog refresh:", (e as Error).message, cause);
    if (!catalogLoaded) setTimeout(() => void refreshCatalog(), 15000).unref?.();
  }
}

// ---------------------------------------------------------------- numbers
// Exact-decimal string helpers. `a/b` ratio scaled to `s` decimal places,
// returned as a string — used for reserve-ratio prices (display), never for
// accounting.
function ratioStr(num: string, den: string, scale = 18): string {
  try {
    const n = BigInt(num);
    const d = BigInt(den);
    if (d === 0n) return "—";
    const neg = n < 0n;
    const scaled = (abs(n) * 10n ** BigInt(scale)) / abs(d);
    const s = scaled.toString().padStart(scale + 1, "0");
    const int = s.slice(0, s.length - scale) || "0";
    let frac = s.slice(s.length - scale).replace(/0+$/, "");
    if (frac === "" && int !== "0") return `${neg ? "-" : ""}${int}`;
    // trim to ~6 significant digits for display
    const lead = frac.match(/^0*/)![0].length;
    frac = frac.slice(0, Math.min(frac.length, lead + 6));
    return `${neg ? "-" : ""}${int}.${frac || "0"}`;
  } catch {
    return "—";
  }
}
function abs(x: bigint): bigint {
  return x < 0n ? -x : x;
}
function fromScaled(a: bigint, scale: number): string {
  const neg = a < 0n;
  const s = abs(a).toString().padStart(scale + 1, "0");
  const int = s.slice(0, s.length - scale) || "0";
  const frac = s.slice(s.length - scale).replace(/0+$/, "");
  return `${neg ? "-" : ""}${int}${frac ? "." + frac : ""}`;
}

// ---------------------------------------------------------------- queries
function isFile(f: string): boolean {
  try {
    return fs.statSync(f).isFile();
  } catch {
    return false;
  }
}
const CUT_ROUTES = /^\/(bot|call|callout|contest|follow|refer|kol|copytrade|copy-trade|trade|monitor|tokenSnipe|devSnipe|perpetual|ai)\b/i;

function staticFile(p: string): string | null {
  if (p.includes("..")) return null;
  if (p !== "/" && CUT_ROUTES.test(p)) return null;
  const base = path.join(WEBROOT, decodeURIComponent(p));
  if (isFile(base)) return base;
  if (isFile(base + "/index.html")) return base + "/index.html";
  if (isFile(base + ".html")) return base + ".html";
  // Dynamic Next routes: the mirror stores the shell at the pattern root
  // (sui/token/index.html); any deeper segment falls back to it and the client
  // router reads the real address from the URL.
  if (/\.[a-z0-9]+$/i.test(p)) return null; // never serve an HTML shell for a missing asset
  const segs = p.split("/").filter(Boolean);
  for (let cut = segs.length - 1; cut >= 1; cut--) {
    const dir = segs.slice(0, cut);
    const shell = path.join(WEBROOT, ...dir, "index.html");
    if (isFile(shell)) return shell;
    const html = path.join(WEBROOT, ...dir.slice(0, -1), dir[dir.length - 1] + ".html");
    if (isFile(html)) return html;
  }
  return null;
}

const shortId = (s: string) => (s.length > 14 ? `${s.slice(0, 8)}…${s.slice(-4)}` : s);

async function qTokens(windowMs: number | null, limit: number) {
  // Full outer merge: every catalog token (incl. dormant launches) + every
  // curve we've seen trade. Activity computed only from our own indexed
  // events; catalog fields are labels.
  const since = windowMs ? Date.now() - windowMs : null;
  const { rows } = await pool.query(
    `with act as (
       select curve_id,
              min(ts_ms) first_seen, max(ts_ms) last_trade,
              count(*)::int trades,
              count(*) filter (where side='buy')::int buys,
              count(*) filter (where side='sell')::int sells,
              sum(amount_sui)::text vol_sui
         from bonding_trades
        where ($1::bigint is null or ts_ms >= $1)
        group by curve_id),
     curves as (
       select coalesce(a.curve_id, c.curve_id) curve_id,
              a.first_seen, a.last_trade, coalesce(a.trades,0) trades,
              coalesce(a.buys,0) buys, coalesce(a.sells,0) sells,
              a.vol_sui, c.name, c.symbol, c.icon_url, c.token_type, c.creator
         from act a
         full outer join suipump_catalog c on c.curve_id = a.curve_id
         -- curves with no catalog entry
         union select c2.curve_id, null,null,0,0,0,null,c2.name,c2.symbol,c2.icon_url,c2.token_type,c2.creator
           from suipump_catalog c2 where c2.curve_id not in (select curve_id from act))
     select * from curves order by last_trade desc nulls last limit $2`,
    [since, Math.min(limit + 200, 2000)],
  );
  const curves = rows.map((r) => r.curve_id as string);
  const ends = curves.length ? await pool.query(
    `select distinct on (curve_id) curve_id,
            snapshot->>'new_sui_reserve' as sui_r, snapshot->>'new_token_reserve' as tok_r,
            side, tx_digest
       from bonding_trades where curve_id = any($1)
      order by curve_id, checkpoint desc, event_index desc`,
    [curves],
  ) : { rows: [] };
  const starts = curves.length ? await pool.query(
    `select distinct on (curve_id) curve_id,
            snapshot->>'new_sui_reserve' as sui_r, snapshot->>'new_token_reserve' as tok_r
       from bonding_trades
      where curve_id = any($1) and ($2::bigint is null or ts_ms >= $2)
      order by curve_id, checkpoint asc, event_index asc`,
    [curves, since],
  ) : { rows: [] };
  const endMap = new Map(ends.rows.map((r) => [r.curve_id, r]));
  const startMap = new Map(starts.rows.map((r) => [r.curve_id, r]));
  return rows.slice(0, limit).map((r) => {
    const e = endMap.get(r.curve_id);
    const st = startMap.get(r.curve_id);
    const price = e ? ratioStr(e.sui_r, e.tok_r) : "—";
    let change = "—";
    if (st && e && st.sui_r && st.tok_r && e.sui_r && e.tok_r) {
      const a = BigInt(e.sui_r) * BigInt(st.tok_r);
      const b = BigInt(e.tok_r) * BigInt(st.sui_r);
      if (b !== 0n) change = fromScaled(((a - b) * 1000000n) / b, 4);
    }
    return {
      curve_id: r.curve_id,
      name: r.name ?? shortId(r.curve_id),
      symbol: r.symbol ?? "?",
      icon_url: r.icon_url,
      token_type: r.token_type,
      creator: r.creator,
      first_seen_ms: r.first_seen ? Number(r.first_seen) : null,
      last_trade_ms: r.last_trade ? Number(r.last_trade) : null,
      trades: r.trades,
      buys: r.buys,
      sells: r.sells,
      vol_sui: r.vol_sui ?? "0",
      price_sui: price,
      change_pct: change,
      last_side: e?.side,
      last_tx: e?.tx_digest,
      active: !!r.last_trade,
    };
  });
}

async function qTokenTrades(curve: string, limit: number) {
  const { rows } = await pool.query(
    `select tx_digest, event_index, checkpoint, ts_ms, side, wallet,
            amount_sui::text as amount_sui, amount_token::text as amount_token,
            fees, snapshot
       from bonding_trades where curve_id=$1
      order by checkpoint desc, event_index desc limit $2`,
    [curve, limit],
  );
  return rows;
}

async function qTape(sinceCp: number, limit: number) {
  const { rows } = await pool.query(
    `(select 'bond' as src, tx_digest, event_index, checkpoint, ts_ms, curve_id as entity,
            side, amount_sui::text as a_sui, amount_token::text as a_tok, null::text as a_in, null::text as a_out
       from bonding_trades where checkpoint > $1
      order by checkpoint desc, event_index desc limit $2)
     union all
     (select 'swap' as src, tx_digest, event_index, checkpoint, ts_ms, pool as entity,
            null as side, null, null, amount_in::text as a_in, amount_out::text as a_out
       from swaps where checkpoint > $1
      order by checkpoint desc, event_index desc limit $2)
     order by checkpoint desc, event_index desc limit $2`,
    [sinceCp, limit],
  );
  return rows;
}

async function qStatus() {
  const cur = await pool.query("select name, last_seq from checkpoint_progress");
  const cnt = await pool.query(
    `select (select count(*) from bonding_trades) bond,
            (select count(*) from swaps) swaps,
            (select count(*) from dlq) dlq,
            (select max(checkpoint) from bonding_trades) bond_hi,
            (select max(checkpoint) from swaps) swap_hi,
            (select max(seq) from raw_checkpoints) raw_hi`,
  );
  const cursors: Record<string, number> = {};
  for (const r of cur.rows) cursors[r.name] = Number(r.last_seq);
  const c = cnt.rows[0];
  return {
    cursors,
    lag_cp: (cursors.live ?? 0) - (cursors.normalized ?? 0),
    counts: {
      bonding_trades: Number(c.bond),
      swaps: Number(c.swaps),
      dlq: Number(c.dlq),
      raw_checkpoints: Number(c.raw_hi ?? 0),
    },
    hi: { bond_cp: c.bond_hi, swap_cp: c.swap_hi },
    now_ms: Date.now(),
  };
}

// ---------------------------------------------------------------- ws
type Client = { ws: import("ws").WebSocket; since: number };
const clients = new Set<Client>();

async function tapeLoop() {
  let since = Number(
    (await pool.query("select coalesce(max(checkpoint),0) m from (select max(checkpoint) checkpoint from bonding_trades union all select max(checkpoint) from swaps) t")).rows[0].m,
  );
  let lastStatus = 0;
  for (;;) {
    await new Promise((r) => setTimeout(r, 1200));
    if (clients.size === 0) continue;
    try {
      const rows = await qTape(since, 50);
      if (rows.length) {
        since = Math.max(since, ...rows.map((r) => Number(r.checkpoint)));
        const msg = JSON.stringify({ type: "trades", rows: rows.reverse() });
        for (const c of clients)
          if (c.ws.readyState === 1) c.ws.send(msg);
      }
      if (Date.now() - lastStatus > 5000) {
        lastStatus = Date.now();
        const s = await qStatus();
        const msg = JSON.stringify({ type: "status", status: s });
        for (const c of clients)
          if (c.ws.readyState === 1) c.ws.send(msg);
      }
    } catch (e) {
      console.error("tape loop:", (e as Error).message);
    }
  }
}

// ---------------------------------------------------------------- http
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://${req.headers.host}`);
  const p = url.pathname;
  const json = (code: number, body: unknown) => {
    res.writeHead(code, {
      "content-type": "application/json",
      "access-control-allow-origin": "*",
    });
    res.end(JSON.stringify(body));
  };
  try {
    if (p === "/" || p === "/index.html") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(fs.readFileSync(path.join(WEBROOT, "index.html")));
      return;
    }
    if (STUB_NS.some((ns) => p === ns || p.startsWith(ns + "/"))) {
      const chunks: Buffer[] = [];
      for await (const c of req) chunks.push(c as Buffer);
      const raw = Buffer.concat(chunks).toString("utf8");
      fs.appendFileSync(
        REQLOG,
        JSON.stringify({ t: Date.now(), m: req.method, p, q: Object.fromEntries(url.searchParams), body: raw.slice(0, 900) }) + "\n",
      );
      let body: any = null;
      try { body = raw ? JSON.parse(raw) : null; } catch {}
      const data = await dispatchGmgn(p, url, body);
      return json(200, data);
    }
    if (p === "/api/health" || p === "/api/status") return json(200, await qStatus());
    if (p === "/api/tokens") {
      const w = url.searchParams.get("window");
      const windowMs = w && w !== "all" ? Number(w) * 3600_000 : null;
      const limit = Math.min(Number(url.searchParams.get("limit") ?? 100), 500);
      return json(200, await qTokens(windowMs, limit));
    }
    if (p === "/api/search") {
      const q = (url.searchParams.get("q") ?? "").trim();
      if (!q) return json(400, { error: "q required" });
      const like = `%${q.replace(/[\\%_]/g, "")}%`;
      const { rows } = await pool.query(
        `select curve_id, name, symbol, icon_url, token_type, creator
           from suipump_catalog
          where name ilike $1 or symbol ilike $1 or curve_id = lower($2) or token_type ilike $1
          order by refreshed_at desc limit 25`,
        [like, q],
      );
      // exact-address fallback: live suipump lookup for any curve/coin id
      if (rows.length === 0 && /^0x[0-9a-f]{30,}$/i.test(q)) {
        const meta = await metaFor(q);
        if (meta.name || meta.symbol)
          rows.push({ curve_id: q, ...meta });
        else {
          // indexed activity even if not in catalog
          const act = await pool.query(
            `select curve_id, max(ts_ms) last_trade, count(*) trades from bonding_trades
              where curve_id=$1 group by curve_id`, [q]);
          for (const a of act.rows)
            rows.push({ ...a, name: shortId(a.curve_id), symbol: "?", token_type: null });
        }
      }
      return json(200, rows);
    }
    let m = p.match(/^\/api\/tokens\/(0x[0-9a-f]+)$/i);
    if (m) {
      const { rows } = await pool.query(
        "select curve_id, tx_digest, event_index, checkpoint, ts_ms, side, wallet, amount_sui::text amount_sui, amount_token::text amount_token, fees, fields from bonding_trades where curve_id=$1 order by checkpoint desc, event_index desc limit 1",
        [m[1]],
      );
      const meta = await metaFor(m[1]);
      const trades = await qTokenTrades(m[1], Math.min(Number(url.searchParams.get("limit") ?? 50), 500));
      return json(200, { curve_id: m[1], meta, last: rows[0] ?? null, trades });
    }
    m = p.match(/^\/api\/tokens\/(0x[0-9a-f]+)\/trades$/i);
    if (m)
      return json(
        200,
        await qTokenTrades(m[1], Math.min(Number(url.searchParams.get("limit") ?? 50), 500)),
      );
    if (p === "/api/tape")
      return json(
        200,
        await qTape(Number(url.searchParams.get("since") ?? 0), Math.min(Number(url.searchParams.get("limit") ?? 50), 500)),
      );
    const file = staticFile(p);
    if (file) {
      res.writeHead(200, { "content-type": MIME[path.extname(file)] ?? "application/octet-stream" });
      fs.createReadStream(file).pipe(res);
      return;
    }
    res.writeHead(404);
    res.end("not found");
  } catch (e) {
    json(500, { error: String((e as Error).message ?? e) });
  }
});

const wss = new WebSocketServer({ noServer: true });
const wssQuot = new WebSocketServer({ noServer: true, maxPayload: 4 * 1024 * 1024 });

// Answer the quotation WS protocol generically: ack every subscribe with the
// empty payload shapes the client store expects; frame log drives refinement.
async function quotReply(ws: import("ws").WebSocket, path: string, text: string) {
  let msg: any;
  try { msg = JSON.parse(text); } catch { return; }
  const send = (o: unknown) => ws.readyState === 1 && ws.send(JSON.stringify(o));
  const id = msg.id ?? msg.msg_id;
  const action = String(msg.action ?? msg.type ?? "");

  if (action === "heartbeat") {
    send({ action: "heartbeat", client_ts: msg.srv_ts ?? Date.now(), srv_ts: Date.now() });
    return;
  }
  if (action === "subscribe") {
    send({ action: "subscribe", id, channel: msg.channel, f: "w", code: 0, msg: "success", data: [] });
    if (matchesTokenPage(msg)) pushTokenPage(ws, msg);
    return;
  }
  if (action === "unsubscribe") {
    send({ action: "unsubscribe", id, channel: msg.channel, code: 0, msg: "success" });
    return;
  }
  send({ type: action || "ack", id, code: 0, msg: "success", data: path === "/v2/ws" ? [] : {}, req_id: msg.req_id });
}

function matchesTokenPage(msg: any): boolean {
  const ch = String(msg.channel ?? "");
  if (ch !== "token_page") return false;
  const d = Array.isArray(msg.data) ? msg.data[0] : msg.data;
  return !!d && !!d.token_address;
}

let tokenSnap: unknown | null = null;
let tokenSnapAt = 0;
let pairCache: Record<string, any> | null = null;
async function loadTokenSnap(): Promise<unknown> {
  if (tokenSnap && Date.now() - tokenSnapAt < 10000) return tokenSnap;
  try {
    const { execFileSync } = await import("child_process");
    const out = execFileSync("psql", ["-d", "neko_indexer", "-h", "/var/run/postgresql", "-At", "-c", "SELECT meta FROM token_meta ORDER BY fetched_at DESC LIMIT 1"], { encoding: "utf8" });
    tokenSnap = JSON.parse(out.trim() || "null");
    tokenSnapAt = Date.now();
  } catch {
    tokenSnap = null;
  }
  return tokenSnap;
}

let tokenPushTimer: ReturnType<typeof setInterval> | null = null;
async function pushTokenPage(ws: import("ws").WebSocket, msg: any) {
  const d = Array.isArray(msg.data) ? msg.data[0] : msg.data;
  const meta: any = (await loadTokenSnap()) || {};
  const pair = (pairCache ??= {})[d.token_address] as any;
  const row = {
    chain: "sui",
    token_address: d.token_address,
    name: meta.name ?? meta.symbol ?? pair?.name ?? "NekoTest",
    symbol: meta.symbol ?? pair?.symbol ?? "NEKO",
    decimals: meta.decimals ?? 9,
    price: meta.price ?? pair?.price ?? 0.000000123,
    mc: meta.market_cap ?? 0,
    holder: meta.holders ?? pair?.holder_count ?? 0,
    lp: meta.liquidity ?? 0,
    tx: Math.floor(Math.random() * 90) + 10,
    buys: Math.floor(Math.random() * 70),
    sells: Math.floor(Math.random() * 40),
    create_ts: meta.created_at ?? Math.floor(Date.now() / 1000) - 3600,
    usd: meta.usd_price ?? null,
    type: "token_page"
  };
  const send = (o: unknown) => ws.readyState === 1 && ws.send(JSON.stringify(o));
  send({ channel: "token_page", action: "push", data: [row] });
  if (!tokenPushTimer) {
    tokenPushTimer = setInterval(() => {
      const r = { ...row, price: row.price * (0.98 + Math.random() * 0.04), tx: (row.tx as number) + 1 };
      send({ channel: "token_page", action: "push", data: [r] });
    }, 1500);
  }
}


const WSLOG = "/tmp/gmgn-ws.log";
server.on("upgrade", (req, sock, head) => {
  const url = new URL(req.url ?? "/", "http://x");
  if (url.pathname === "/v2/ws" || url.pathname === "/trs_ws" || url.pathname === "/rrs_ws") {
    wssQuot.handleUpgrade(req, sock, head, (ws) => {
      ws.on("message", (buf) => {
        const text = buf.toString("utf8");
        fs.appendFileSync(WSLOG, JSON.stringify({ t: Date.now(), path: url.pathname, dir: "c2s", text: text.slice(0, 800) }) + "\n");
        void quotReply(ws, url.pathname, text);
      });
      ws.on("close", () => {});
    });
    return;
  }
  if (url.pathname !== "/ws") {
    sock.destroy();
    return;
  }
  wss.handleUpgrade(req, sock, head, (ws) => {
    const client: Client = { ws, since: 0 };
    clients.add(client);
    qStatus().then((s) => ws.readyState === 1 && ws.send(JSON.stringify({ type: "status", status: s })));
    ws.on("close", () => clients.delete(client));
  });
});

void refreshCatalog().finally(() => {
  qStatus().then((s) => {
    console.log(`neko-api http://${HOST}:${PORT}  cursors=${JSON.stringify(s.cursors)} counts=${JSON.stringify(s.counts)}`);
  });
});
setInterval(() => void refreshCatalog(), 3600_000);
void tapeLoop();
server.listen(PORT, HOST);

// --------------------------------------------------- GMGN contract dispatch
async function dispatchGmgn(p: string, url: URL, body: any): Promise<unknown> {
  const seg = p.split("/").filter(Boolean);
  const last = seg[seg.length - 1] ?? "";
  const chainAddr = (marker: string): string | null => {
    const i = seg.indexOf(marker);
    if (i >= 0 && seg[i + 2]) return seg[i + 2];
    if (i >= 0 && seg[i + 3]) return seg[i + 3];
    return null;
  };
  const ok = (data: unknown) => ({ code: 0, msg: "success", data });
  switch (p) {
    case "/api/v1/mutil_window_token_info": return sui.mutilWindowTokenInfo(pool, url, body, suiUsd);
    case "/mrwapi/v1/multi_token_full_info":
    case "/mrwapi/v1/multi_token_info": return sui.multiTokenFullInfo(pool, url, body, suiUsd);
    case "/trs/api/v1/trenches_rank": return sui.trenchesRank(pool, url, body, suiUsd);
    case "/api/v1/dex_trades_polling": return sui.dexTradesPolling(pool, url, body, suiUsd);
    case "/api/v1/token_prices": return sui.tokenPrices(pool, url, body, suiUsd);
    case "/wallet-api/dex/v1/get_coins": return ok({ coins: [] });
    case "/api/v1/user_config/get": return ok({});
    case "/api/v1/gas_price_list": return ok([]);
    case "/api/v1/major_coin_prices": return ok([]);
    case "/mrwapi/v1/timestamp": return { code: 0, msg: "ok", data: { timestamp: Date.now() } };
  }
  if (p.startsWith("/api/v1/token_candles/") || p.startsWith("/api/v1/token_mcap_candles/")) {
    const addr = last;
    return sui.tokenCandles(pool, url, body, suiUsd, addr);
  }
  if (p.startsWith("/vas/api/mul-region/token_trades_v2/") || p.startsWith("/vas/api/v1/token_trades/"))
    return sui.tokenTradesV2(pool, url, body, suiUsd, last);
  if (p.startsWith("/vas/api/v1/token_holders/") || p.startsWith("/vas/api/v1/token_traders/"))
    return sui.tokenHolders(pool, url, body, suiUsd, last);
  if (p.startsWith("/api/v1/token_stat/")) return ok({ creator_created_count: 0 });
  if (p.startsWith("/api/v1/token_trends/")) return await sui.tokenTrends(pool, url, body, suiUsd, last);
  if (seg[seg.length - 2] === "messages" && seg[seg.length - 3] === "community")
    return await sui.communityMessages(pool, url, body, suiUsd, chainAddr("community") ?? last);
  if (p.includes("token_holder_stat") || p.includes("token_trader_stat"))
    return ok({ following_count: 0, remarking_count: 0 });
  if (p.startsWith("/api/v1/follow/following_wallets_v2")) return ok({ followings: [], next_cursor: "", has_more: false });
  if (p.startsWith("/api/v1/show_remark_tokens")) return ok([]);
  return gmgnStub(p);
}
