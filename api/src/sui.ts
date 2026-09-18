// GMGN-protocol handlers backed by OUR indexer Postgres (Sui data).
// Contract: docs/neko-indexer/contracts/token-page.md (traced from the
// de-minified client). Envelope {code:0,msg:"success",data}. Money stays
// exact-decimal strings; SUI/USD only scales display fields (never stored).

import type { Pool } from "pg";

const shortId = (s: string) => (s.length > 14 ? `${s.slice(0, 8)}…${s.slice(-4)}` : s);

export type Handler = (pool: Pool, url: URL, body: any, suiUsd: number, pathAddr?: string) => Promise<unknown>;

// ---------------------------------------------------------------- identity
// GMGN "token ca" for a SuiPump launch = the coin PACKAGE (the token_type
// prefix). A curve id or full coin type must also resolve.
async function resolveToken(pool: Pool, addr: string): Promise<any | null> {
  const { rows } = await pool.query(
    `select curve_id, name, symbol, icon_url, token_type, creator, created_at,
            split_part(token_type, '::', 1) as pkg
       from suipump_catalog
      where curve_id = $1 or token_type = $1 or split_part(token_type,'::',1) = $1
      limit 1`,
    [addr],
  );
  return rows[0] ?? null;
}

// Fallback identity from indexed trades when the catalog lacks the token.
async function resolveFromTrades(pool: Pool, addr: string): Promise<any | null> {
  const { rows } = await pool.query(
    `select curve_id, min(ts_ms) first_seen from bonding_trades
      where curve_id=$1 or $1 = any (select distinct t from bonding_trades b, unnest(array[b.fields->>'token_type']) t)
      group by curve_id limit 1`,
    [addr],
  );
  if (rows[0]) return { ...rows[0], name: shortId(rows[0].curve_id), symbol: "?" };
  return null;
}

async function lastReserves(pool: Pool, curve: string): Promise<{ sr: string; tr: string } | null> {
  const { rows } = await pool.query(
    `select snapshot->>'new_sui_reserve' sr, snapshot->>'new_token_reserve' tr
       from bonding_trades where curve_id=$1
      order by checkpoint desc, event_index desc limit 1`,
    [curve],
  );
  return rows[0] ?? null;
}

function priceSui(sr: string | null, tr: string | null): number {
  if (!sr || !tr || BigInt(tr) === 0n) return 0;
  return Number(BigInt(sr) * 10n ** 12n / BigInt(tr)) / 1e12;
}

// ---------------------------------------------------------------- boards
export const mutilWindowTokenInfo: Handler = async (pool, _u, body) => {
  const addrs: string[] = (body?.addresses ?? []).slice(0, 500);
  const out: unknown[] = [];
  for (const a of addrs) {
    const t = (await resolveToken(pool, a)) ?? (await resolveFromTrades(pool, a));
    if (!t) continue;
    const lr = await lastReserves(pool, t.curve_id);
    out.push({
      address: a,
      chain: "sui",
      name: t.name,
      symbol: t.symbol,
      logo: t.icon_url,
      price: priceSui(lr?.sr ?? null, lr?.tr ?? null) * (suiUsdOf() || 1),
      total_supply: 0,
    });
  }
  return { code: 0, msg: "success", data: out };
};

let _suiUsd = 0;
export const setSuiUsd = (v: number) => (_suiUsd = v);
const suiUsdOf = () => _suiUsd;

export const trenchesRank: Handler = async (pool, _u, body) => {
  // /trs/api/v1/trenches_rank seed: newest ACTIVE curves by last trade.
  const limit = Math.min(Number(body?.limit ?? body?.size ?? 100), 300);
  const { rows } = await pool.query(
    `select curve_id, max(ts_ms) last_trade, count(*)::int trades,
            sum(amount_sui)::text vol_sui
       from bonding_trades
      group by curve_id order by max(ts_ms) desc limit $1`,
    [limit],
  );
  const list = [];
  for (const r of rows) {
    const t = await resolveToken(pool, r.curve_id);
    const lr = await lastReserves(pool, r.curve_id);
    const p = priceSui(lr?.sr ?? null, lr?.tr ?? null);
    list.push({
      address: t?.pkg ?? t?.curve_id ?? r.curve_id,
      chain: "sui",
      name: t?.name ?? shortId(r.curve_id),
      symbol: t?.symbol ?? "?",
      logo: t?.icon_url,
      price: p * _suiUsd,
      price_1m: p * _suiUsd,
      price_5m: p * _suiUsd,
      price_1h: p * _suiUsd,
      price_24h: p * _suiUsd,
      volume: Number(r.vol_sui) / 1e9 * _suiUsd,
      swaps: r.trades,
      created_at: Math.floor(Number(t?.created_at ?? r.last_trade) / 1000),
      last_trade: Math.floor(Number(r.last_trade) / 1000),
      total_supply: 0,
      initial_liquidity: 0,
    });
  }
  return { code: 0, msg: "success", data: list };
};

// ---------------------------------------------------------------- token page
export const multiTokenFullInfo: Handler = async (pool, _u, body) => {
  const addrs: string[] = (body?.addresses ?? []).slice(0, 200);
  const data = [];
  for (const a of addrs) {
    const t = (await resolveToken(pool, a)) ?? (await resolveFromTrades(pool, a));
    if (!t) { data.push({ address: a }); continue; }
    const lr = await lastReserves(pool, t.curve_id);
    const holders = await pool.query(
      "select count(distinct wallet) n from bonding_trades where curve_id=$1", [t.curve_id]);
    data.push({
      address: a,
      chain: "sui",
      name: t.name,
      symbol: t.symbol,
      logo: t.icon_url,
      decimals: 9,
      total_supply: lr ? lr.tr : "0",
      token_max_supply: lr ? lr.tr : "0",
      liquidity: lr ? Number(lr.sr) / 1e9 * _suiUsd * 2 : 0,
      fdv: lr ? priceSui(lr.sr, lr.tr) * _suiUsd * Number(lr.tr) / 1e9 : 0,
      creator_address: t.creator,
      creator_created_count: 0,
      holder_count: Number(holders.rows[0].n),
      top_10_holder_rate: "0",
      launchpad: "suipump",
      launchpad_platform: "suipump",
      launchpad_status: 0,
      launchpad_progress: "0",
      pool: { address: t.curve_id, exchange: "suipump" },
      creation_timestamp: t.created_at ? Math.floor(Number(t.created_at) / 1000) : 0,
      security: {
        renounced_mint: 1,
        renounced_freeze_account: 1,
        burn_ratio: "0",
        dev_token_burn_amount: "0",
        dev_token_burn_ratio: "0",
        top_10_holder_rate: "0",
      },
    });
  }
  return { code: 0, msg: "success", data };
};

export const tokenCandles: Handler = async (pool, url, _b, suiUsd, pathAddr) => {
  const resolution = url.searchParams.get("resolution") ?? "1m";
  const bucket = { "1s": "second", "1m": "minute", "5m": "minute", "15m": "minute", "1h": "hour", "4h": "hour", "1d": "day", "1D": "day" }[resolution] ?? "minute";
  const width = { "1s": 10, "5m": 300, "15m": 900, "4h": 14400 }[resolution] ?? 0;
  const { rows } = await pool.query(
    `with px as (
       select amount_sui::numeric / nullif(amount_token::numeric,0) as p,
              amount_sui::numeric as sui, ts_ms,
              to_timestamp(floor(extract(epoch from to_timestamp(ts_ms/1000.0)) / $2::int) * $2::int) at time zone 'utc' as b
         from bonding_trades where curve_id = (select curve_id from suipump_catalog
              where curve_id=$1 or split_part(token_type,'::',1)=$1 or token_type=$1 limit 1)
     )
     select floor(extract(epoch from b))*1000 t,
            (array_agg(p order by ts_ms asc))[1]::text open,
            max(p)::text high, min(p)::text low,
            (array_agg(p order by ts_ms desc))[1]::text close,
            coalesce(sum(sui),0)::text volume
       from px group by b having count(*) > 0 order by b asc limit 3000`,
    [pathAddr, width || 60],
  );
  const usd = (v: string) => (Number(v) * suiUsd).toString();
  return {
    code: 0, msg: "success",
    data: {
      list: rows.map((r) => ({
        time: Number(r.t), open: usd(r.open), high: usd(r.high), low: usd(r.low),
        close: usd(r.close), volume: (Number(r.volume) / 1e9).toString(),
      })),
      total_supply_missing: false,
    },
  };
};

export const tokenTradesV2: Handler = async (pool, _u, _b, suiUsd, pathAddr) => {
  // /vas/api/mul-region/token_trades_v2/{chain}/{addr}
  const { rows } = await pool.query(
    `select b.tx_digest, b.event_index, b.side, b.wallet, b.amount_sui::text a_sui,
            b.amount_token::text a_tok, b.ts_ms, c.token_type
       from bonding_trades b
       left join suipump_catalog c on c.curve_id = b.curve_id
      where b.curve_id = (select curve_id from suipump_catalog
            where curve_id=$1 or split_part(token_type,'::',1)=$1 or token_type=$1 limit 1)
         or b.curve_id = $1
      order by b.checkpoint desc, b.event_index desc limit 100`,
    [pathAddr],
  );
  const history = rows.map((r) => {
    const sui = Number(r.a_sui) / 1e9;
    const tok = Number(r.a_tok) / 1e9;
    return {
      chain: "sui",
      token_address: pathAddr,
      wallet_address: r.wallet,
      maker: r.wallet,
      event: r.side,
      side: r.side,
      token_amount: r.a_tok,
      price: sui / (tok || 1),
      price_usd: (sui / (tok || 1)) * suiUsd,
      amount: sui,
      amount_usd: sui * suiUsd,
      timestamp: Math.floor(r.ts_ms / 1000),
      tx_hash: r.tx_digest,
      gas_native: "0",
      gas_usd: "0",
      type: r.side === "buy" ? 1 : 2,
    };
  });
  return { code: 0, msg: "success", data: { history, next: "" } };
};

export const tokenHolders: Handler = async (pool, _u, _b, suiUsd, pathAddr) => {
  const { rows } = await pool.query(
    `select wallet,
            sum(case when side='buy' then amount_token::numeric else -amount_token::numeric end)::text balance,
            max(ts_ms) last_active, count(*) n
       from bonding_trades
      where curve_id = (select curve_id from suipump_catalog
            where curve_id=$1 or split_part(token_type,'::',1)=$1 or token_type=$1 limit 1)
         or curve_id=$1
      group by wallet order by sum(case when side='buy' then amount_token::numeric else -amount_token::numeric end) desc
      limit 100`,
    [pathAddr],
  );
  const total = rows.reduce((a, r) => a + (Number(r.balance) || 0), 0) || 1;
  return {
    code: 0, msg: "success",
    data: {
      list: rows.map((r, i) => ({
        address: r.wallet,
        wallet_address: r.wallet,
        balance: r.balance,
        percentage: ((Number(r.balance) || 0) / total).toFixed(4),
        amount_usd: ((Number(r.balance) || 0) / 1e9) * 0,
        realized_profit: "0", unrealized_profit: "0", total_profit: "0",
        last_active: Math.floor(r.last_active / 1000),
        tag: i === 0 ? "creator" : "",
      })),
    },
  };
};

export const tokenStat: Handler = async (_p, _u, _b, _v, pathAddr) =>
  ({ code: 0, msg: "success", data: { creator_created_count: 0 } });

export const tokenTrends: Handler = async (_p, _u) => ({
  code: 0, msg: "success",
  data: { trends: { holder_count: [], top10_holder_percent: [], top100_holder_percent: [], avg_holding_balance: [], insider_percent: [], entrapment_percent: [], bundler_percent: [], bot_degen_percent: [] } },
});

export const communityMessages: Handler = async (pool, _u, _b, _v, pathAddr) => {
  const { rows } = await pool.query(
    `select tx_digest, event_index, fields, raw, ts_ms from bonding_trades
      where curve_id=$1 order by checkpoint desc limit 1`, [pathAddr],
  );
  void rows;
  return { code: 0, msg: "success", data: { messages: [], top_message: null, vip_message: null, has_more: false, next_cursor: "" } };
};

export const dexTradesPolling: Handler = async (pool) => {
  const { rows } = await pool.query(
    `select count(*) n, coalesce(sum(amount_sui),0)::text v from bonding_trades
      where ts_ms > (extract(epoch from now())*1000)::bigint - 86400000`,
  );
  return {
    code: 0, msg: "success",
    data: {
      allAggregationResult: {
        token_count: Number(rows[0].n),
        swap_count: Number(rows[0].n),
        liquidity: Number(rows[0].v) / 1e9 * _suiUsd,
        volume: Number(rows[0].v) / 1e9 * _suiUsd,
      },
      topLaunchpads: [{ name: "suipump", url: "", token_count: Number(rows[0].n), liquidity: 0, volume: 0 }],
      topProtocols: [{ name: "cetus", url: "", liquidity: 0, volume: 0 }],
    },
  };
};

export const tokenPrices: Handler = async (pool, _u, body) => {
  const addrs: string[] = (body?.addresses ?? []).slice(0, 200);
  const list = [];
  for (const a of addrs) {
    const t = await resolveToken(pool, a);
    const lr = t ? await lastReserves(pool, t.curve_id) : null;
    list.push({ address: a, price: lr ? priceSui(lr.sr, lr.tr) * _suiUsd : 0 });
  }
  return { code: 0, msg: "success", data: { list } };
};
