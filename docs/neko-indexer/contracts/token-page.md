# Token page (/solana/token/[address]) — client contract (STATIC analysis)

Root `D=/home/carnage/rouge-kali-state/gmgn.ai/deobfuscated`. All citations file:line under D.
Envelope `{code:0,msg:"success",data}` everywhere unless noted (raw axios paths).

## 1. Transport layers (decides auth + error behavior)

**A) `Network` (obs+axios)** — `gmgn-vendors/433496.js` (`C.Ay` re-export `902286.js:6,31-45`)
- Envelope unwrap `433496.js:23-34`: response has `code` → return `data.data` if `code===0`, else **throw** (react-query error). `extractData:false` (only `hU`) returns FULL envelope (`953004.js:11020-11030` checks `t.code!==0 || !t.data`).
- GET/DELETE: `data` object → **query params** (`433496.js:169-173`); POST/PUT: → JSON body.
- Common params merged into every request `433496.js:175-180` + `main-410221.js:140-157`: `device_id, tab_id, fp_did, client_id (=gmgn_web_20260912-4388-7792ac2), from_app, app_ver, tz_name, tz_offset, app_lang, os, worker`.
- **authType:Access when logged out** `433496.js:57-91`: resolver needs `accountId`; logged out ⇒ throw `{code:-1,"accountId required"}` **BEFORE axios** ⇒ **request NOT sent, no Authorization header**. Exception `enforeRequest:true` ⇒ authType downgraded to None ⇒ sent **without** Authorization (only `hU` uses this, `120048.js:12-18`).
- Logged in ⇒ `Authorization: Bearer <getAccessToken(accountId)>` (`433496.js:153-161`). 401 ⇒ refresh+1 retry (`902286.js:10-25`); 429/403-CF special-cased (`433496.js:192-209`).
- `Network.None` (default) ⇒ header block skipped (`433496.js:149-151`) — always sent, anonymous.

**B) `pX`/`fi` axios helpers** — `454358.js:320-404` (`_app/454358.js`)
- `pX`=GET, `fi`=POST (args named `params` are the **body** for `fi`). Envelope: resolve `data.data` if `code===0`, else resolve **envelope object**; network error resolves **`false`** (never rejects ⇒ degrades, no crash) `454358.js:343-355`.
- `needToken:true` request interceptor `454358.js:262-279`: if a wallet is CONNECTED for the URL's chain (`YK` = connected address, `835495.js:425-434`) ⇒ attach `authorization: Bearer <token>`, reject (skip) if token missing. Logged out ⇒ `YK=""` ⇒ **request sent anonymously, no header**.

**C) `E.j`** — `378733.js:10-67` — plain axios obs, token resolver hardcoded `of({})` ⇒ never sends auth; NO envelope unwrap (callers read `resp.status`, `resp.data.code`, `resp.data.data`) (`645286.js:1862-1867`). Used by `Oi,pZ,YY,LF(→fi actually)`.

## 2. Bootstrap — identity/overview (page shell `chunks__pages___chain___token___token_-90d11d1f905178f6/200251.js`)

### B1 POST /mrwapi/v1/multi_token_full_info  ← hard dependency
`Network.post`, authType none. Body `{chain, addresses:[addr]}` → `data:[obj]`, client takes `[0]` (`200251.js:4714-4728`).
Reads: `creator_created_count`(num), `creator_address`, `creator_stat.{from_address,amount,transfer_ts,nickname,fund_tx_hash}`, `link{gmgn,...}`(social), `rug`(obj), `security`(obj, + `lock_info`→`lockInfo` fix-up), `launchpad`, `launchpad_platform`, `launchpad_status`(int), `launchpad_progress`, `migrated_pool_exchange`, `launch_quote_address`, `holder_count`, `top_10_holder_rate`, `top_rat_trader_percentage`, `top_bundler_trader_percentage`, `top_entrapment_trader_percentage`, `bot_degen_rate`, `creator_hold_rate`, `creator_token_balance`, `private_vault_hold_rate`, `fresh_wallet_rate`, `dev_team_hold_rate`, `top70_sniper_hold_rate`, `bluechip_owner_count`, `bluechip_owner_percentage` (`200251.js:4660-4679,4738-4809`). Feeds header/price panel/security via socket-store merge (`mergeTokenInfoAfterBaseInfoLoaded`, `200251.js:4600-4628`). Minimum logged-out shape: `data:[{}]` OK — every field optional-chained; `security||={}` (`200251.js:4747`).
Also `addressInfoApi` `ie(creator,chain)` (vendor 87453, missing) reads `eth_balance` (`200251.js:4784-4790`).

### B2 GET /api/v1/token_stat/{chain|eth}/{addr} (`120048.js:167-169` via pX)
`queryKey:["token-counts",...]`, **refetchInterval 5000**, enabled after B1 (`200251.js:4729-4737`). Reads `creator_created_count` (num) only (`200251.js:4738`). `{}` safe.

### B3 GET /api/v1/token_verify/{chain}/{addr} (`200251.js:6415` via pX, params `{}`)
Reads `list` (verification records) → state map (`200251.js:6417-6422`). `{}`/false safe.

### B4 GET /api/v1/token_candles/{chain}/{addr} (`120048.js:65-68`, E.j — anonymous)
Chart history. Params (`645286.js:1855-1861`): `resolution` (1s/1m/…/1D per TradingView), `from=0`, `to=Date.now()`, `limit=1000`, `pool_type="tpool"` + common. Response checked `status===200 && data.code==0` then `data.data.{list[], total_supply_missing(bool)}`; bar fields `time(ms),open,high,low,close,volume` (strings→Number) (`645286.js:1862-1881`). Live bars via WS `getKlineObservable` (`645286.js:1961`). `list:[]` → empty chart, no crash.

### B5 GET /api/v1/token_mcap_candles/{chain}/{addr} — same params/response, used when chart type ≠ price (`120048.js:69-72`, `645286.js:1851-1854`).

### B6 GET /api/v1/mutil_window_token_link_rug_vote/{chain}/{addr} (`120048.js:19-21`, E.j)
Social/rug fallback merge into store: reads `data.data.{link:{gmgn,...rest}, rug}` (`358724.js:349-365`).

### B7 GET /api/v1/token_prices — POST via `fi` (`120048.js:22-25`): body `{chain, addresses[]}`
Orders/Strategy tab: `queryKey:["get_strategy_token_prices"]`, **refetchInterval 10000**, reads `data.list[].{address, price}` (`904536.js:4138-4156`).

### B8 POST /api/v1/mutil_window_token_info (`120048.js:12-18`, Access+**enforeRequest**+extractData:false)
Orders-tab enrichment of open positions (`953004.js:11059-11068`): body `{chain, addresses:[signal_list[0]…]}`; reads envelope `data[].{address, symbol, logo}` (`953004.js:11020-11035`). Logged out: still sent (downgraded to None).

## 3. Activity tab (default) — `chunks__5425/168262.js` + `chunks__18262/718262.js`
### A1 GET /vas/api/mul-region/token_trades_v2/{chain}/{addr} (FM, `120048.js:119-128`)
`limit=50` forced by FM; actual builder `168262.js:174-205`: `{limit(100), tag?, following?, remarks?, min_amount_usd, max_amount_usd, from?, to?, revert?, needToken?, cursor}` (`24826.js:88-114` yC; usd = k*1000) + URL suffix `?event=buy&event=sell…` (`W0`, `24826.js:115-146`; events: buy,sell,add,remove,transfer,mint,burn,claim_fee,pump_social_claim_fee,event_fomo; `tag=fomo→event_fomo` on sol, `168262.js:186`). Reads `data.{history[], next}` (`168262.js:205-223`). Row fields (`718262.js:600-627`): `chain, token_address, wallet_address/maker, from_address, to_address, event, token_amount, price_usd, amount_usd, timestamp, tx_hash, gas_native, gas_usd`, plus `maker_info/maker_token_tags/balance/history_sold_amount/history_bought_amount` (`161995.js:4-22`). Initial fetch + WS live (`getActivityAllInfo`), no polling; refresh on tab focus.
### A2 GET /vas/api/v1/token_trades/{chain}/{addr} (qf, `120048.js:101-110`) same params; makers-remark/following variants redirect to `/vas/api/v1/follow/token_trades/...` or `{vasApi}/remark/token_trades/...` (`120048.js:83-100`). Bags/claim sub-tab (`528819.js:152-181`, events=claim_fee only) + per-maker lookup `qf(chain,token,{maker:wallet})` reads `history[]` (`200251.js:2582-2607`, staleTime 15000).
### A3 GET {vasApi}/token_fomo_events/{chain}/{addr} (yb, `120048.js:129-133`) — FOMO tag: `{limit:50, min/max_amount_usd, from, to, maker, revert, cursor}` (`718262.js:1570-1600`) → `history/next`.
### A4 Chart mark feeds (GET, Network, params `period`): `/api/v1/tg_calls/klines/{chain}/{addr}` → `tg_calls[].{timestamp,tg_calls[].{name,url,avatar}}`; `/api/v1/discord_calls/klines/...` → `items[].{timestamp,items[].{guild_name,message_send_at,url,guild_avatar,content}}` (`708523.js:7966-8000, 8127-8152`); `/vas/api/v1/follow/agged_token_trades/...` (KZ, Access) `708523.js:10307`; `{vasApi}/remark/agged_token_trades/...` (TX, Access) `708523.js:10723`; `{vasApi}/fomo/top_wallet_agg/{chain}/{token}` (Bk) `708523.js:11686`; `{vasApi}/agged_token_transfers/{chain}` (Hw) params `{token_address,wallet_address,period,min_amount_usd}` reads `transfers[]` `708523.js:13129-13145`.

## 4. Holders tab
### H1 GET /vas/api/v1/token_holder_stat/{chain|eth}/{addr} (p2, `120048.js:174-178`) params `{needToken:isSign}` + AbortSignal
Tab-count query `["tokenHolderTabCount",...]`, **refetchInterval 30000** (`99039/790169.js:10,37-95`). Reads `following_count`, `remarking_count` (`790169.js:60-68`).
### H2 GET /vas/api/v1/token_holders/{chain}/{addr} (`400710.js:5-14`, pX, arrayFormat repeat)
List feed (`534947.js:180` → vendor 369874): params `{limit:100, cost:20, tag, wallet_address?, orderby, direction, following?, remarking?, needToken?}` (app deobfuscated.js:325369-325385). Reads `data.list[]`; row fields rendered: `address, balance, percentage, tag/nickname, realized_profit, unrealized_profit, total_profit, last_active` (`59046/959046.js:253,410,467-471`). One-shot + WS merge, 30s load-timeout guard (`534947.js:170-179`).
### H3 GET {vasApi}/token_holder_extra_info/{chain}/{addr} (`400710.js:15-21`) params `wallet_addresses[]` (repeat).
### H4 GET /api/v1/tokens/top_buyers/{chain}/{addr} (dB) — `["get-top-holder-70"]`, **30000**, reads `.holders` map incl. `top70_sniper_hold_rate` (`35744/354915.js:2334-2341`).
### H5 GET /vas/api/v1/token_watched_wallet_stat/{chain}/{addr} (Xf, `120048.js:155-160`, params `needToken:true`) — **enabled only when signed** (`153682.js:55-95`, 30000) + WS `getTokenStatObservable` merge; reads count fields (`remarking_count` `790169.js:64`).

## 5. Traders tab
### T1 GET /vas/api/v1/token_trader_stat/{chain|eth}/{addr} (fT, `120048.js:170-173`) params `{needToken:isSign}` — `["tokenTradeTabCount"]`, **30000** (`14710/14710.js:407-459`); reads `following_count, remarking_count` (`14710.js:426-431,482-485`).
### T2 GET /vas/api/v1/token_traders/{chain}/{addr} (Cw, `400710.js:22-31`) — list (`534947.js:308-317`): params `{limit:100, orderby, direction, wallet_address?, tag?, following?, remarking?, needToken?}`; reads `data.list[]`, merged with WS (rows carry maker/event/amount fields like A1).

## 6. Security tab (panels in `35744/354915.js`, hooks `352024.js`)
### S1 GET /api/v1/token_security_sol/{chain}/{addr} (cm; tron→`/defi/quotation/v1/tokens/security`, evm→`token_security_evm`; `120048.js:46-60`)
Hook `["securityStatus",chain,addr]`, staleTime 60000, retry 1, `enabled=third arg` (`352024.js:39-73`). Response normalised by `Iz` (`120048.js:213-269`): sol: `renounced_mint, renounced_freeze_account, top_10_holder_rate, burn_ratio, dev_token_burn_amount, dev_token_burn_ratio`; evm also `buy_tax, is_honeypot, is_open_source, sell_tax, is_renounced, can_sell, can_not_sell, average_tax, high_tax, lock_summary.{lock_tags,is_locked,lock_percent,left_lock_percent,lock_detail[].{percent,pool,is_blackhole}}`. UI: renounced/lock/honeypot/tax chips (`354915.js:26-34,4223,4804`). All coercions null-safe ⇒ `{}` OK.
### S2 GET /api/v1/token_trends/{chain}/{addr}?trends_type=…&trends_type=… (pc, `120048.js:191-200`)
**refetchInterval 60000**; types = `avg_holding_balance,holder_count,top10_holder_percent,top100_holder_percent,bundler_percent,insider_percent,bot_degen_percent,entrapment_percent` (`120048.js:182-189`, `206013.js:206`). Reads `data.trends.{holder_count[], top10_holder_percent[], top100_holder_percent[], avg_holding_balance[], insider_percent[], entrapment_percent[]}` sparkline arrays (`6013/206013.js:620-705`; guards `!ee.data.trends` → placeholder).
### S3 GET /api/v1/tokens/rug_history/{chain|eth}/{addr} (Eo) — `["get-token-rug-his"]`, reads `{history[], holder_rugged_num, holder_token_num}`; history items `{amount, price, timestamp}` (`354915.js:3711-3776, 2586,2939-2976`).
### S4 GET /api/v1/token_bundler_stat/{chain}/{addr} (aO) — `get_token_bundler_stat-${chain}-${addr}` (`6013/126395.js:37-48`); reads `bundler_count, bundler_max_hold_ratio, bundler_swap_quote_amount, bundler_swap_ratio`.
### S5 GET /api/v1/token_lock_info/sol/{addr} (Fz) — `["lockInfo", addr]` (`TokenCenter/778301.js:1085-1090`).

## 7. Comments / community tab — token page chunk `5568.js`
### C1 GET /api/v1/token/{chain}/{addr}/community/messages (`704108.js:1-16`)
`queryKey ["community-messages",chain,addr]`, params `{limit:50, cursor?}`, **refetchInterval 10000** (paused while input focused) (`5568.js:3641-3660`). Reads `messages[] (ulid, content, created_at, platform, user.{screen_name,avatar,twitter_user_id}), top_message, vip_message, has_more, next_cursor` (`5568.js:3659-3662, 3745-3760, 989,1205-1223`). `messages:[]` safe. X-feed filter by watch list (`5568.js:776`).
### C2 Callout (writes; Access; user-gated) `95753.js`/`5568.js`:
`POST /api/v1/notification/call_out` `{chain,call_wallet,call_token,call_thesis,callback_to_ulid/callback_to_chain/callback_to_wallet?}` (`95753.js:470-490`); `GET /api/v1/notification/call_out/check` `{chain,call_wallet,call_token}` reads `code,data.next_available_at` (`95753.js:20-36`); `POST /api/v1/notification/call_out/declaration` (submit; reads `order_id`, `5568.js:1503-1515`) + `GET` same URL params `{chain,token_ca,order_id}` reads `status=="success|failed"`, **setInterval 3000** while pending (`5568.js:1179, 3694-3743`); `POST /api/v1/wallets/twitter_info` `{chain, addresses[]}` staleTime 43200000 (`95753.js:131-147`).

## 8. Other tabs
- **Liquidity**: GET `/vas/api/v1/token_liquidity/{chain}/{addr}` params `{limit:100, following?, needToken?, event?, tag?(array repeat), revert?, cursor}` → `{history[], next}`, self-revalidate **10000** (`10625/200549.js:26, 2560-2578, 2591-2601`); following→`/vas/api/v1/follow/token_liquidity/...` (`200549.js:30`); `GET /vas/api/v1/token_liquidity_detail/{chain}/{addr}` (`200549.js:872`); `GET /api/v1/token_liquidity_stats/{chain}/{addr}?needToken=true` (V) **10000** (`200549.js:2536-2542`), feeds tag bar (`gA`: `smart_wallets, fresh_wallets, renowned_wallets, creator_wallets, sniper_wallets, following_wallets, following_count, remarking_count, whale_wallets, top_wallets, rat_trader_wallets, bundler_wallets, bot_degen_wallets` `24826.js:31-45`); `GET /vas/api/v1/token_liquidity_trend/{chain}/{addr}` (C3) **30000** (`6013/863641.js:8-38`).
- **Fee panels**: GET `/api/v1/token_fee_info/{chain}/{addr}` (bO) `["tokenFeeInfo"]` staleTime 5000 + optional refetchInterval (`677401.js:5-48`), reads `pool_fee_info_list[].pool_type, security.tax_allocation, buy_tax, sell_tax, total_buy_tax, total_sell_tax, fee_params, fee_ratio, meteora_virtual_curve_fee_config, meteora_damm_v2_base_fee_config` (`119775.js:96-158`, `698445.js:341-389`); GET `/api/v1/token_pool_fee_info/{chain}/{addr}?needToken=true` (l_) cached 300s stream, reads `list[]` (`358724.js:417-437`).
- **DevToken tab**: GET `/api/v1/dev_created_tokens/{chain}/{addr}` (N3) params `{order_by?, direction?, migrate_state}` (`807741.js:844-880`); GET `/api/v1/native_transfer/{chain}/{wallet}` (Z$) (`120048.js:309-318`).
- **DCA tab**: GET `/vas/api/v1/dca_orders/{chain}/{token}` ($D, Access) params `{order_by, desc, limit:50, page_token}` reads `list, page_token` (`469744.js:1835-1852`).
- **Orders/Strategy/Holding tabs**: order queries via `mrtapi` (vendor) + B7 token_prices + B8 enrichment; `GET /api/v1/crypto_kline/crypto/{chain}` (Rj, GET-with-data `120048.js:61-64`) params `{interval(1m|1h|1d|4h), end(ms), limit:60}` → `list[].{timestamp,open,high,low,close}` (`161930.js:1645-1652, 1079-1120`), staleTime per interval.
- **Info panel**: `GET /api/v1/website_info/{chain}/{addr}` (QH) reads `website` (+links) (`750657.js:1318-1345`); `GET /api/v1/token_dev_info/{chain}/{addr}` (A3) `["get-twitter-his"]` reads `twitter_name_change_history[]` (`389665.js:40-70`); `GET /api/v1/token_banner/{chain}/{addr}` (OO) reads `banner` url (`422333.js:39-66`); `GET /vas/api/v1/similar_coin` (id) params `{chain,symbol,name,order_by,cur_address}` (`TokenDetail/957164.js:4387-4400`); `POST /api/v1/logo/logo_dup_detail` (O4) `{chain,logo,token_address}` fallback `{count:0,tokens:[]}` (`957164.js:3744-3756`).
- **Global ticker (layout, on token page)**: GET `/api/v1/dex_trades_polling` params `{chain, window:"24h"}` **60000** (`161930.js:3388-3426`); reads `allAggregationResult, topLaunchpads, topProtocols` (`161930.js:3350-3358`). Not user-gated.
- **Excluded**: `degencall` (`120048.js:73-82`) only used by /bot & /call pages (`chunks__pages__call/430417.js:314-344` params `{limit:5|10, cursor}` → reads `next`). `token_fee_info_polling` = search dropdown (`440.js:5839`). `token_holder_counts` = rank pages (app deobfuscated.js:137637).

## 9. User-gated calls (skipped when logged out, no header sent — §1A)
`$D` dca_orders, `KZ/TX` agged trades, `call_out*`, `wallets/twitter_info`, `user_config/get`, `remark_tokens(iF)`, `following_wallets_v2(qE)`, `show_remark_tokens(oK)` (pX needToken — sent anonymously logged-out, server-side empty), `Xf` (also `enabled:isSign` client-side), `fT/p2/Cw/ez/qf/FM` with `needToken/following/remarks` tags. Logged-out minimum: omit call entirely — every consumer guards (`!!isSign` / `enabled:` / `x?.[... ]||0`); UI shows 0/hidden tags (`14710.js:422-424`, `790169.js:55-59`, `153682.js:85 T=S!==null&&j`).

## 10. App bootstrap on page load (layout)
- `POST /api/v1/user_config/get` (Lg, Access) body `{modules:[m]}` or `{module_query:[{module, sub_modules:[…]}]}`, m ∈ `sniper_api|sound_preferences|custom_sounds|blacklist` (`510762.js:4-16`); response keyed `<module>[_<sub>]` → `{update_time, cfg_data}` (`484966.js:115-150`); **logged out → skipped before send** (`484966.js:208-212`). `{}` safe.
- `GET /api/v1/show_remark_tokens/{chain}?needToken=true` after sign-in; reads array `{token_address, rename}[]` into map (`161930.js:10165-10177`); non-array guarded.
- `GET /api/v1/follow/following_wallets_v2` (qE, Access) params `{chain(=network), limit, cursor, search_text?}` reads `{followings[], next_cursor, has_more}` (`582551.js:43-50`, `864638.js:72-97` — callout wallet picker).
- `POST /api/v1/remark_tokens` (iF, Access) write `{chain, tokens:[{token_address, rename}]}`; error `code 40002307` handled (`23926.js:1053-1066`).
- `GET /wallet-api/dex/v1/get_coins` authType **None** ⇒ always sent, no header (`999869.js:1159-1163`); reads `data.coins[].{id, tradeToken, displayName, chain, chainId, contractAddress, decimals, depositEnable, withdrawEnable, min/maxDeposit, min/maxWithdraw, fee, withdrawPrecision}`, staleTime 300000 (`999869.js:1165-1180,1243-1246`); minimum `{coins:[]}`.
- WS bootstrap `wss…/v2/ws` (`410221.js:67-88`) carries live trades/klines/stats — mirror must stub or it will spam reconnects.

## 11. JSON skeletons (required-by-code R / optional O)
```jsonc
// POST /mrwapi/v1/multi_token_full_info  {chain:"solana", addresses:["<addr>"]}
{"code":0,"msg":"success","data":[{ "security":{}, "link":{"website":"","twitter":"","telegram":""},
  "rug":{}, "creator_address":"", "creator_stat":{"from_address":"","amount":"0","transfer_ts":0,"nickname":"","fund_tx_hash":""},
  "creator_created_count":0, "holder_count":0, "top_10_holder_rate":"", "bot_degen_rate":"", "creator_hold_rate":"",
  "top70_sniper_hold_rate":"", "launchpad_status":0, "launchpad_progress":"" /* R: data[] array; rest O */ }]}
// GET /api/v1/token_stat/{chain}/{addr}          → {"creator_created_count":0}
// GET /api/v1/token_candles/{chain}/{addr}?resolution=1m&from=0&to=<ms>&limit=1000&pool_type=tpool
//   (raw envelope)                               → {"list":[],"total_supply_missing":false}
// GET /vas/api/mul-region/token_trades_v2/{chain}/{addr}?event=buy&event=sell&limit=100&…
//                                                 → {"history":[],"next":""}
// GET /vas/api/v1/token_holders/{chain}/{addr}?limit=100&cost=20&tag=All&orderby=…&direction=desc
//                                                 → {"list":[]}
// GET /vas/api/v1/token_traders/{chain}/{addr}?limit=100&orderby=…&direction=… → {"list":[]}
// GET /vas/api/v1/token_trader_stat|token_holder_stat/{chain}/{addr} → {"following_count":0,"remarking_count":0}
// GET /api/v1/token_security_sol/solana/{addr}    → {"renounced_mint":0,"renounced_freeze_account":0,
//   "top_10_holder_rate":"","burn_ratio":"","dev_token_burn_amount":"","dev_token_burn_ratio":""}
// GET /api/v1/token_trends/{chain}/{addr}?trends_type=…(x8) → {"trends":{"holder_count":[],"top10_holder_percent":[],
//   "top100_holder_percent":[],"avg_holding_balance":[],"insider_percent":[],"entrapment_percent":[]}}
// GET /api/v1/token/{chain}/{addr}/community/messages?limit=50 → {"messages":[],"top_message":null,
//   "vip_message":null,"has_more":false,"next_cursor":""}
// POST /api/v1/token_prices {chain,addresses[]}    → {"list":[{"address":"","price":"0"}]}
// POST /api/v1/mutil_window_token_info {chain,addresses[]} → {"code":0,"msg":"","data":[{"address":"","symbol":"","logo":""}]}
// GET /wallet-api/dex/v1/get_coins                 → {"coins":[]}
// GET /api/v1/dex_trades_polling?chain=solana&window=24h → {"allAggregationResult":{},"topLaunchpads":[],"topProtocols":[]}
// GET /api/v1/user_config/get(body modules[])      → {}            // keyed cfg map, {} fine
// GET /api/v1/follow/following_wallets_v2?chain&limit&cursor → {"followings":[],"next_cursor":"","has_more":false}
// GET /api/v1/show_remark_tokens/{chain}?needToken=true → []
```

## 12. Hard-fail vs degrade
- **Hard (page unusable/skeleton forever)**: `multi_token_full_info` — nothing else can name the token; all tab queries enable after it (`200251.js:4726, 4734`). `token_candles` empty ⇒ chart blank but rest alive (semi-hard for overview).
- **Degrade (guard in code)**: every `pX/fi` endpoint (never rejects; `false`→empty list/`{}`); `token_stat` typeof-guard; trends `!ee.data.trends` placeholder; messages `?? []`; security `{}` via `Iz`; Access-gated endpoints simply not sent while logged out; 429/5xx ⇒ react-query error state + existing cache.
- **Write calls** (`call_out`, `remark_tokens`, `follow/unfollow`) fail → toast only.
