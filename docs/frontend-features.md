# Frontend Feature Inventory — Memecoin Trading Terminal (`frontend/`)

> Source of truth: the mirrored frontend under `frontend/` — a static mirror of **GMGN.AI**, "the fastest multi-chain meme trading terminal". The mirror contains the prerendered HTML shell for every route, plus images, fonts, and the JSON configs. **The JavaScript bundles are NOT mirrored** (the `/_next/static/chunks/*.js` files referenced by the HTML are absent; `webroot/_next` is a dangling symlink to a missing `_next/`), so page bodies that are client-hydrated are reconstructed here from (a) the route structure, (b) the prerendered DOM that IS present (landing, /ai, /about, /app), (c) the JSON config assets, and (d) asset filenames that encode features.
>
> Mirror metadata: `asset_index.json` (`generated_by: mirror_assets.py`, generated 2026-09-14, 410 mirrored assets, 7 404s, 5 S3-access-denied). Referenced subdomains seen in JS literals: `card.gmgn.ai`, `docs.gmgn.ai`, `link.gmgn.ai`.

---

## Route map — every mirrored route (87)

### Landing & marketing
- `/` — index / landing page: full product pitch + the 11 core-feature blurbs
- `about` — brand story, "Discover faster, Trading in seconds", TG bot / mobile app / earn CTAs, cross-chain swap (up to 50% fee savings), TradingView embed (RSI/MACD)
- `app` — GMGN mobile app download hub (App Store / TestFlight / Google Play / APK + QR); feature promos: real-time alerts, live K-line + holder insights, auto buy/sell following smart money, wallet PnL
- `promo/app-upgrade` — app upgrade promo page

### Main terminal (the trading dashboard frames)
- `main` — the terminal's primary shell/landing view
- `trade` — token trading page
- `trade/copy` — copy-trading view of the terminal
- `tgtrade` — trading page (TG-linked context)
- `discover` — token discovery hub
- `new-pair` — new token pair board
- `monitor` — wallet/token monitor (the header "Monitor" nav target)

### Solana-specific screens
- `snipex` — the automatic sniper terminal (launch sniping engine)
- `sol/trade/So11111111111111111111111111111111111111112` — per-token trade terminal (seed = WSOL; dynamic per token address)
- `sol/token` — token detail page (generic)
- `sol/token/So11111111111111111111111111111111111111112` — per-token detail (price, liquidity, holders, top-10, dev, socials)
- `sol/address/So11111111111111111111111111111111111111112` — wallet address page
- `sol/snipex/So11111111111111111111111111111111111111112` — sniping terminal bound to a token address
- `sol/devSnipe/x` — "dev snipe": auto-buy when the dev wallet buys the newly-launched token
- `sol/tokenSnipe/x` — token snipe config view
- `sol/skyTrade/So11111111111111111111111111111111111111112` — "sky trade" bound to a token (streamlined quick-trade UI)

### Discovery & rankings
- `trenches` — the meme "trenches" token feed (raw launchpad degen flow)
- `trend` — trending tokens board
- `trend/HotSearchRank` — hot-search (attention) ranking
- `trend/NewTrendRank` — new trend ranking
- `trend/TrendingMixRank` — blended trending ranking
- `trend/WorldCupRank` — World Cup 2026 prediction leaderboard
- `pump` — Pump.fun launchpad feed
- `rank` — leaderboard/ranking page
- `ranking` — rankings page (alias/companion)
- `worldcup-2026` — FIFA World Cup 2026 prediction game (player cards, quiz, score predictions, prizes)
- `xstocks` — tokenized stock exchange ("xStocks") feed
- `xstocks/XStocksRank` — xStocks rankings
- `callout` — Callout social feed (community callouts with ranking crowns)
- `time` — "time" market view
- `time/TimeAuto` — auto-timeframe market view

### Follow & copy trading
- `follow` — follow feed (wallet/KOL follow stream — header nav target)
- `follow/Watchlist` — followed-wallet watchlist within follow
- `strategy` — copy-trading strategy hub (choose wallets to copy)
- `strategy/FilterToken` — strategy token filtering
- `watchlist` — watchlist hub (groups, tokens)
- `watchlists` — watchlists list
- `watchlist/FollowRank` — followed-wallet ranking within watchlist
- `watchlist/FollowWallet` — individual followed wallet within watchlist
- `holding` — current holdings view

### Portfolio, orders, points, referral
- `portfolio` — full P&L portfolio (realized/unrealized, win rate, positions) — header nav target
- `order` — open orders (limit + TP/SL strategy orders) & history
- `level` — account level / privileges
- `rewards` — points & rewards program — header nav target
- `referral` — referral program
- `referral/MainContent` — referral main panel
- `referral/x` — per-referral-code page

### Perpetuals
- `perpetual/x` — per-token perpetual (perps) trading
- `perpetual/earn` — perp earn / yield on collateral

### Contests
- `contest` — trading contests hub
- `contest/x` — per-contest page
- `contest/ContentRankListV6` — content/rank list (V6 layout)
- `rank` / `ranking` — see Discovery & rankings

### Cooking (token launch / bundling / sniping tool)
- `cooking` — the "Cooking" panel (create tokens, configure bundling wallets)
- `cooking/x/callback` — mint/cooking callback landing

### AI platform
- `ai` — AI Agent skills market hub ("Best Crypto Trading Skills, Built for Agents")
- `ai/skills/x` — individual skill page (install docs, examples)
- `ai/generateapi` — generate your free GMGN API key

### Wallet / RPC provider bridges
- `WalletProvider` — wallet-connect provider mount
- `SolProvider` — Solana wallet provider mount
- `RainbowkitProvider` — RainbowKit (EVM) provider mount

### Telegram bot
- `bot` — Telegram bot product page
- `tglogin` — Telegram login (sign-in via TG)
- `tgtrade` — trade-through-Telegram view

### Social identity & OAuth
- `twitterbind` — bind X/Twitter account
- `twittercallback` — X/Twitter OAuth callback
- `oauth/authorize` — OAuth authorize screen

### Security & account protection
- `security` — security center hub
- `security/bind` — bind account method
- `security/bind/x` — per-method bind flow
- `security/google` — Google Authenticator 2FA
- `security/google/x`
- `security/metamask` — MetaMask binding
- `security/metamask/x`
- `security/passkeys` — passkeys / WebAuthn
- `security/passkeys/x`
- `security/phantom` — Phantom binding
- `security/phantom/x`

### Share / referral redirects
- `share` — share card generator (token/kline share images)
- `r/x` — short-link redirect

### Infra / utility
- `404` — not found
- `_error` — generic error page
- `network-check` — network/RPC health check
- `match-console` — match result console
- `call` — call (signal) page
- `callout` — see Discovery & rankings
- (additional flat `pages/*.html` copies of the same routes exist without trailing index — same features)

---

## Feature areas

### 1. Multi-chain token trading terminal (core)
- **Zero-latency swap terminal** — buy/sell at market price with anti-MEV (sandwich) protection; shows actual fill price, tokens received, and gas fee.
- **Multi-chain support** — SOL, BSC, Base, ETH listed outright; config layer adds Arbitrum, Monad, Tron, HyperEVM, MegaETH, XLayer, Robinhood, ARC, and "stable" networks. Network logos include `blast` and `robinhood`.
- **Per-token trade screens** (`/sol/trade/[addr]`, `/trade`, `/tgtrade`) with a "skyTrade" streamlined quick-trade variant.
- **Fiat/RWA equity quotes** — pinned token quote boards per chain (SOL/BSC/Base/Robinhood/XLayer), e.g. tokenized stocks (RDDT, wTSLAx, MRNAB) tagged `rwa`.
- **Perp-swap alt market** — `perp-wallet` wallets (MetaMask, Rabby, OKX, Bitget, Binance, TokenPocket) indicate a dedicated EVM-perps product.
- **External swapper interoperability** — "trade via" partners: BullX, Photon, Trojan, Axiom, Dropsbot, Pepeboost, GMGN.
- **Cross-chain swap & bridge** — "save up to 50% on fees"; bridge providers: LiFi, DeBridge, CCTP, Relay, Bridgers.

### 2. Token discovery & the "Trenches"
- **Trenches feed** — the raw launchpad degen stream of newly minted tokens.
- **Trending board** with multiple lenses: blended (`TrendingMixRank`), hot-search/attention (`HotSearchRank`), and new-trend (`NewTrendRank`), refreshed every minute by buy/sell count, volume, price change, holder growth.
- **New pair / new token boards** — `new-pair`, `pump` (Pump.fun feed), "newly created tokens".
- **Launchpad platform registry** — `lpp.json` covers 13 networks with their launch platforms, e.g. SOL (41: Pump.fun, Mayhem, Bonk, Bags, Tren.ch, Stonk.fun, Moonshot, Wen.dev, xStocks, Launchlab, Raydium, Meteora, Orca, …), BSC (25: FourMeme, Flap, OpenFour, …), Base (18: Clanker, Flaunch, Zora, …), Robinhood (68), plus TRON/SunPump, Monad/Nadfun, etc.
- **Launchpad bonding-curve lifecycle tracking** — `migrateConfigs`/`outDexKey` per platform = watch tokens fill their bonding curve and graduate to a DEX.

### 3. Token analytics & security
- **Token overview** — price, market cap, liquidity, holder count, launchpad progress, top-10 concentration, dev holding %, sniper wallet %, bundle %, KOL buyer count, socials (Twitter/website/Telegram).
- **Automated security audit** — honeypot detection, contract open-source status, renounced mint/freeze authority, buy/sell tax rates, top-10 concentration risk (Go+ Security integrated).
- **Liquidity pool analysis** — DEX, pool depth, base/quote reserves, pool age.
- **Holder structure analysis** — live insider/rat-wallet ratio, bundle-buy ratio, dev-holdings %, top-10 concentration.
- **Kline chart** — OHLCV across 30s/1m/5m/15m/1h/4h/1d, plus TradingView pro-charts (RSI/MACD).

### 4. Wallet analytics & labeling
- **Wallet page** (`/sol/address/[addr]`) — P&L analytics: realized/unrealized PnL, win rate, risk/reward, holdings valuation, trade history, dev token-creation records with all-time-high (ATH) cap.
- **Wallet labels** — smart money, KOL, sniper, rat warehouse, bundle, bot, builder, contract, de-fi OG, meme OG, fresh, scam, VC, holder — surfaced per wallet/token.
- **Funding source** — CEX deposit / new wallet / cross-chain origin per wallet.
- **PnL extras** — PnL calendar, PnL share cards, per-sound toggle.

### 5. Smart money & KOL tracking
- **Wallet monitor** (`monitor`) and **follow** streams for followed wallets and KOLs.
- **Watchlists** with groups, per-token stats, `FollowRank`/`FollowWallet` views.
- **Real-time alerts** — buy/sell/position-add/full-exit push notifications (app + TS alerts).
- **KOL recommendations** (`recommand_kol`) and KOL holder/trade views.

### 6. Sniping engines
- **Snipex** — battle-tested auto-snipe buy on token launch (per-token bind: `/sol/snipex/[addr]`).
- **Token snipe** (`tokenSnipe`) — configure snipe amount/fees/time.
- **Dev snipe** (`devSnipe`) — auto-buy the instant the dev wallet buys the new token.
- **Sky trade** — fast manual entry for sniping momentum picks.

### 7. Order engine & automated strategies
- **Market buy / sell** (with % or close-full).
- **Limit buy / limit sell** — trigger on target price, optional expiry.
- **Buy with TP/SL** — attach multiple take-profit and stop-loss levels in one transaction.
- **Trailing take-profit / trailing stop-loss** — price trails the market, triggers on a set pullback from the peak.
- **Multi-wallet buy** — same token from up to 100 wallets, independent amount per wallet, isolated failures.
- **Open orders + cancel** — view/cancel pending limits and TP/SL strategy orders.
- **Gas price query** — low/avg/high tiers per chain to time trades.

### 8. Copy trading & strategies
- **Copy-trade assessment** — judge a wallet's win rate, PnL multiplier, position structure, trade frequency, style, and a copy-tradeability score with latency/slippage/gas backtest.
- **Wallet Address Score** — 0-100 track-record score, copy-tradeability score, dev-reputation score + style tags.
- **Follow/strategy hub** — pick smart-money wallets, auto-mirror size-relative buys/sells (`/trade/copy`, `strategy/FilterToken`).

### 9. Portfolio & positions
- **Portfolio** — aggregate balances, realized/unrealized PnL, win rate, risk/reward, per-chain.
- **Holdings, orders, levels** (`holding`, `order`, `level`) — position list, open orders, account level/privileges.

### 10. Contests, rankings & games
- **Scheduled trading contests** with seasonal theming (assets for seasons s4, s7-s12), rank masks, top-N crowns, content rank list (`ContentRankListV6`).
- **Rank/leaderboards** (`rank`, `ranking`) with up/down movement arrows.
- **Callout rankings** — community callout crowns (1st/2nd/3rd) and declarations.
- **World Cup 2026 prediction game** (`worldcup-2026`) — player cards (Messi, Ronaldo, Mbappé, …), quiz quotes, score-meme & quiz activities, prize pool, app CTAs.
- **xStocks** — tokenized equity exchange with its own rankings.

### 11. Perpetuals
- **Perp trading** (`perpetual/x`) — EVM perps with perp-wallet connectors.
- **Perp earn** (`perpetual/earn`) — yield on lending collateral.

### 12. Cooking — token LAUNCH & bundling toolkit
- **One-click token creation** — name, symbol, logo, description, socials, initial buy; auto symbol from name; 13-char pump names; fee lock; fee-split presets.
- **Bundling** — up to 12 wallets (PumpFun) bundled buys; snipe + bundle simultaneously.
- **Fee/tax token config** — buy/sell tax split across recipient/burn/dividend/liquidity (FourMeme, Flap), route recipient cut to X handle or wallet(s); quick-fill X account.
- **Agent-mode cooking** — AI-keyword one-click cooking from monitored tweets (incl. quoted keywords), keyboard shortcut, draggable/resizable panel, clipboard-CA auto-refresh.
- **Launch targets** — Pump.fun (incl. Cashback & Agent Auto-Buyback modes), FourMeme, Clanker, Flap; launchpad stats counter.
- 17-item changelog (`static/cooking/feature_updates_test.json`) records these capabilities with dates (2026-02/03).

### 13. AI agent platform (`/ai`)
- **Skills market** — 60 skills (v8) across categories: Trading, Data Analytics, Monitor, News & Media, Cooking.
- **Skill install via gmgn-cli** — `npm install -g gmgn-cli; npx skills add GMGNAI/gmgn-skills`; usage via `GMGN_API_KEY`.
- **AI API keys** — free key generation at `/ai/generateapi`.
- Covered in full in the *AI skills catalog* section below.

### 14. Telegram bot
- **Product pages** (`/bot`, `/tglogin`, `/tgtrade`) — trade, monitor, and get alerts from Telegram; TS login.
- **Alert/signal categories** (from bot icons): LP burn alerts, new LP, Pump.fun alerts, smart-money alerts, degen calls, Solana signal alerts, ETH alerts, cross-chain swap (SOL/BSC/Base/ETH/TRON).
- **Bot actions** — login, search, signal alerts toggle.

### 15. Rewards, points & loyalty
- **Points program** (`rewards`, `level`) with tier backgrounds (N/R/SR/SSR), rank banners, point-vs-cash slot pools.
- **Prize catalog (points gacha/pool)** — AirPods, iPhones, iPhone 17 Pro Max, MacBook, Apple Watch, gaming chair, keyboard, mouse, knitwear/shirt/outfit sets, suitcase, electric vehicle, BMW, Rolex, LV wallet, backpack, Zowie monitor.
- **Points API card** (`openapi_card`) — points via GMGN OpenAPI.

### 16. Referral program
- **Referral invites** (`referral`, `referral/[code]`, `referral/MainContent`), reward tracking, share-refund card.

### 17. Account security
- **Security center** — bind/manage login methods.
- **2FA & wallets** — Google Authenticator, MetaMask, Phantom, passkeys (WebAuthn).
- **Social binds** — X/Twitter bind + OAuth (`oauth/authorize`), Telegram login.
- **Account-level risk flags** — honeypot warnings, scam labels.

### 18. Sharing & gamified sharing
- **Share cards** — kline share image, user/portfolio share card, refund-share card, portrait/landscape layouts, PnL cards.

### 19. Wallet connectivity & providers
- **Solana wallets** — Phantom, Solflare, OKX, Backpack, Bitget, TokenPocket, TronLink; provider bridges `SolProvider`, `WalletProvider`, `RainbowkitProvider`.

### 20. Mobile apps
- **iOS / Android / TestFlight / APK / QR install**, "V3.0" with **online Callout** (real-time multi-chain wallet tracking + smart-money/KOL buy-signal aggregation), App alerts (Global News, Trending Movers, Smart Money and KOL actions, watchlist updates).

---

## AI skills catalog — all 60 skills (skills.json v8)

Details per skill: `installation` (gmgn-cli commands), `capabilities`, example `prompts`. Categories: `trading`, `data-analytics`, `monitor`, `media`, `cooking`.

- `5min-trending-tokens` — scan highest-volume tokens over past 5m; numeric filters (MC, LP, vol, price Δ, smart-money count, bundle %)
- `pumpfun-trending-tokens` — hottest Pump.fun tokens over past hour
- `token-basic-info` — price, market cap, liquidity, holders, top-10, dev %, sniper %, bundle %, KOL buyers
- `pumpfun-newly-created-tokens` — freshly created Pump.fun tokens only, filterable
- `newly-created-tokens` — fresh tokens across major launchpads
- `kol-bought-new-tokens` — new trench tokens KOLs already bought
- `bankr-newly-created-tokens` — Bankr (Base) new tokens
- `fourmeme-newly-created-tokens` — FourMeme (BSC) new tokens
- `migrated-tokens` — launchpad→DEX graduates
- `migrated-token-quality-screener` — MC $50-200K, LP>10K, top10<20%, bundle<20%, fresh-wallet<20%
- `near-completion-tokens` — bonding curves near 100%
- `query-5m-hot-search-tokens` — most-searched token ranks, 1m/5m/1h/6h/24h windows
- `dev-info-analysis` — dev holding, launch history, ATH, funding source, DEX ads, X renames, CTO flag
- `dev-created-tokens` — all tokens a dev wallet created (ATH/current MC, migration rate)
- `token-security-check` — honeypot, open-source, renounced, taxes, top-10
- `liquidity-pool-analysis` — LP depth, DEX, reserves, age
- `token-kline-data` — OHLCV 30s..1d, custom ranges
- `top100-holders-analysis` — 100 holder wallets w/ share, cost basis, P&L, labels, funding source
- `top100-traders-analysis` — 100 most-active traders w/ buy/sell volume, realized/unrealized P&L
- `smart-money-holders-analysis` — smart-money wallets holding a token
- `kol-holders-analysis` — KOL wallets holding a token
- `wallet-profit-loss-stats` — win rate, PnL multiplier, spent, trade count, 7d/30d, batch compare
- `wallet-holdings` — current positions + cost basis + P&L
- `wallet-trade-history` — historical buys/sells/transfers, filterable
- `wallet-token-balance` — balance of a specific token
- `wallet-copy-trade-assessment` — worth copy-trading? style classification
- `wallet-address-score` — track-record / copy-tradeability / dev-reputation scores + tags
- `market-buy` — instant market buy, anti-MEV, fill/gas details
- `market-sell` — sell % or 100%
- `multi-wallet-buy` — buy from up to 100 wallets concurrently
- `limit-buy` — auto-trigger on price drop, optional expiry
- `limit-sell` — auto-trigger on rise, % selling
- `limit-buy-with-tp-sl-strategy` — TP/SL at buy time incl. trailing modes, multi-tier
- `buy-with-take-profit-stop-loss` — multiple TP/SL levels in one step
- `trailing-take-profit` — trails peak, sells on pullback %
- `trailing-stop-loss` — rises with price, triggers on pullback %
- `query-open-orders` — pending limits + TP/SL strategies
- `cancel-strategy-order` — cancel by order ID
- `query-real-time-gas-price` — low/avg/high gas per chain
- `smart-money-buy-signal` — cluster buys by smart money
- `smart-money-exit-signal` — simultaneous dumps by smart money
- `smart-money-trades` — real-time smart-money trades, buy/sell filter
- `kol-trades` — real-time KOL trades
- `tracked-wallet-trades` — trades from wallets you follow
- `kol-call-signal` — KOL calls with trigger MC
- `pump-claim-signal` — Pump.fun claim activity
- `price-surge-signal` — sudden price spikes
- `price-surge-signal-token-screening` — surge scan + rug filter + per-token health
- `query-watchlist-tokens` — your GMGN watchlist w/ group filters
- `scan-watchlist-price-swing` — watchlist moves over custom threshold + security checks
- `opennews` — OpenNews MCP: 84+ news sources, AI impact scores, signals, listings from 9 exchanges, Hyperliquid whale/perp OI
- `opentwitter` — OpenTwitter MCP: profiles, search, follower changes, KOL followers, deleted tweets, real-time subscriptions (15 tools)
- `get-twitter-following-posts` — reverse-chronological timeline from authorized follow list (paid API)
- `query-launchpad-stats` — tokens created on each launchpad via GMGN
- `launch-on-pumpfun` — one-click deploy + initial buy
- `launch-on-fourmeme` — one-click deploy (BSC)
- `launch-on-clanker` — one-click deploy (Base)
- `launch-on-fourmeme-tax` — fee % split across recipient/burn/dividend/liquidity
- `launch-on-flap-tax` — separate buy/sell tax; recipient route to X handle or wallets
- `launch-on-pumpfun-mode` — Cashback or Agent Auto-Buyback modes

---

## Data & config layer

- **Launchpad registry** `static/config/lpp.json` — versioned ($schema), 13 networks: arbitrum, arc, base, bsc, eth, hyperevm, megaeth, monad, robinhood, sol, stable, tron, xlayer. Each platform: name, default flag, colors, logo (light/dark), launchpad href, out-DEX key, bonding-curve migration configs, tips.
- **Pinned quote boards** `static/config/quotes.json` (+ `quotes-test.json`) — 5 chains: base, bsc, robinhood, sol, xlayer. RWA-tagged pinned tokens, e.g. bsc (557), sol (519), base (361), robinhood (193), xlayer (94) with per-token CA, title, dark/light icon, tags.
- **AI skill catalog** `static/opstatic/skills.json` (+ zh-CN/zh-TW) — version 8, 60 skills, categories + source colors.
- **Feature announcements** `static/opstatic/updatepopup.json` — version 79; latest: Clean Layout Mode, GMGN App V3.0 Online Callout, Agent Skills Market, Fusion (multi-chain) Wallet Tracker.
- **Cooking changelog** `static/cooking/feature_updates_test.json` — 17 entries (en-US/zh-CN/zh-TW) covering bundling, sniping, fee-lock, agent mode, etc.
- **Asset mirror** `asset_index.json` — 410 mirrored assets; CDN hosts sampled (Raydium, raw.githubusercontent, Alchemy, WalletConnect, DexTools); subdomains `card.gmgn.ai`, `docs.gmgn.ai`, `link.gmgn.ai`.

## Assets that encode features

- **User/wallet tags** (icons): bot, builder, contract, de-fi OG, fresh, holder, KOL, meme OG, scam, smart money, sniper, VC.
- **Signal glyphs** (icons): ATH alert, bar (kline), boost, MCP, price-up, update, ad.
- **Points prize pool** (icons): rolex_watch, bwm, lv_wallet, iphone17_pro_max, macbook, apple_watch, zowie_monitor, backpack, knitwear, electric_vehicle, gaming_chair, airpods, suitcase, shirt/outfit sets.
- **Chain logos** (img): sol→solana, bsc, base, ether, tron, arbitrum, monad, megaeth, hyperEvm, xlayer, robinhood, arc, blast, stable.
- **App alerts** (download-app): alerts-1..5, follow smart money, wallet analysis, real-time trading.
- **Share cards**: kline (landscape/portrait), user card, refund share.

---

# Part 2 — Code & architecture deep-dive

The mirror is a **static export of a Next.js Pages Router SPA** (no server runtime). Every HTML is a hydration shell: the app itself runs client-side, so there is no JS source to read — but the HTML embeds the full **chunk manifest, CSS loader fingerprint, inline design-system CSS, `__NEXT_DATA__` shell, SEO/OG layer, and third-party telemetry**, and the 15 JSON configs encode the actual engineering rules of the product. Everything below is recovered from the mirror byte-for-byte.

## Framework & runtime

- Next.js Pages Router, fully static export — `__NEXT_DATA__` on every page: `"nextExport":true,"autoExport":true,"isFallback":false,"buildId":"esIrmR3hLDfwoCyQ5aqFa"`. `pageProps` is `{}` everywhere; all data is fetched client-side at runtime (the terminal is an API-backed SPA).
- Renderer fingerprints: React, `jsx-<hash>` class annotations, `data-sentry-element`/`data-sentry-source-file` on every component, prefetch of a service worker at `/_next/static/workers/gmgn.0713414d.js`.
- Per-route `<head>` shell: two viewports (`width=device-width` + app-cover `user-scalable=no, viewport-fit=cover`), `<meta name="google" content="notranslate">`, preloads of `/static/GMGNLogoDark.svg` and `/static/logo_small2.svg` as image, font preload of `/static/font/Switzer/variable.woff2` (crossorigin).
- In-page `scriptLoader`: Google Analytics 4 `G-0XM0LYXGC8` (lazyOnload; gtag config with `page_path`), Cloudflare Turnstile `https://challenges.cloudflare.com/turnstile/v0/api.js` (lazyOnload).

## Module graph (recovered from 172 page manifests; chunk files not mirrored)

- **190 distinct JS chunk URLs** are referenced from the `<script>` tags across all pages.
- **84 are Next page-routes** (`/_next/static/chunks/pages/<route>-<hash>.js`) — one per app screen, including everything in the route map plus screens never crawled as HTML: `_chain_/trade/_address_`, `_chain_/token/_token_`, `_chain_/snipex/_address_`, `_chain_/devSnipe/_order_id_`, `_chain_/tokenSnipe/_order_id_`, `_chain_/skyTrade/_address_`, `oauth/authorize`, `cooking/x/callback`, `twittercallback`, `twitterbind`, `tglogin`, `tgtrade`, `r/_code_`, `share`, `referral/_code_`, `promo/app-upgrade`, `match-console`, `network-check`, `time/TimeAuto`, `worldcup-2026`, `xstocks/XStocksRank`, `contest/ContentRankListV6`, `perpetual/_pair_` + `perpetual/earn`, all `security/*` sub-routes.
- Runtime split: `polyfills`, `webpack`, `gmgn-vendors`, `util-vendors`, `sentry-bundle`, `core-vendors`, `main`, `blockchain-vendors`, `pages/_app`, then numbered feature chunks (20909, 71547, 17697, 29386, 3139, 75861, …) and a `gmgn.<hash>.js` web worker.

## CSS loader fingerprint per route (7 distinct sets)

`<link rel=preload>`-listed global CSS is `/_next/static/css/24dd3b29aa5afbb5.css` (core) + `d7b28a431bbb0a0c.css` (shell); route groups add one more:

- **default (2 css)** — 119 routes (all app screens + 404/_error/provider shells)
- **+`2bce940a00e9b86d.css`** — 36 routes: index, portfolio, holding, trenches, trend+rank variants, follow/watchlist, pump, new-pair/x, referral+trade pages — the "trading desktop" skin
- **+`613d298b8d391533.css`** — 7 routes: discover, rank, **ranking**, trade
- **+`22c0c3ed7c96969f.css`** — 4: contest, contest/[aid]
- **+`b501d8c4072540a2.css`** — 2: rewards (points/drops)
- **+`d269b1b9b408141e.css`** — 2: monitor (alerts centre)
- **+`70ae04393d7a2688.css`** — 2: worldcup-2026

## Design system (8 themes, inline)

A single 1,094-line inline `<style>` block (present on all 172 pages) defines every theme as CSS custom properties using raw RGB triplets (`--color-primary: 134 217 159`) with semantic alias chains (`--color-btn-primary → --color-primary`, `--color-green → var(--color-green-100)`) covering button/toggle/input/line/card/bg/mask/tooltip/scrollbar and per-family scales (green/red/blue/yellow/beige, increase/decrease, warning/error).

Eight selectable themes via `[custom-theme="…"]` / `[custom-preset-theme="…"]`:

| theme | primary | bg | text | red | note |
|---|---|---|---|---|---|
| `dark` (default) | 134 217 159 (green) | 17 18 20 | 240 245 245 | 242 102 130 | full token family |
| `light` | 21 178 95 | 255 255 255 | 10 10 10 | 240 65 71 | white mode |
| `ax` | 82 111 255 (indigo) | 16 17 20 | 252 252 252 | 242 84 97 | AX |
| `ph` | 106 96 232 (violet) | 25 25 33 | 242 245 249 | 255 75 146 | PH |
| `bx` | 74 168 119 (green) | 13 13 16 | 250 250 250 | 207 87 97 | BX |
| `hyper` | 79 210 193 (teal) | 15 26 31 | 246 254 253 | 237 112 136 | Hyper |
| `giga` | 223 82 150 (pink) | 255 250 253 | 20 12 16 | 223 82 150 | light bg |
| `pro` | 105 207 141 (green) | 18 18 18 | 245 245 245 | 222 87 89 | PRO |

Plus per-page static-export style chunks: `.ai-static.jsx-4c2eb0618287cfbc`, `.static-ssg-shell.jsx-1ccaaed71a3e97a3`, `.about-static.jsx-2f597f42f4afc279`, `.app-static.jsx-f8f4777d35135b75`.

## Third-party & telemetry

- **GA4** `G-0XM0LYXGC8` (lazyOnload + page_path), preconnects to googletagmanager.com / google-analytics.com.
- **Sentry** ingest `o4505147559706624.ingest.us.sentry.io` — every React element tagged via `data-sentry-element`.
- **Cloudflare Turnstile** CAPTCHA on auth/security flows.
- **WalletConnect** relay `pulse.walletconnect.org`.
- **SEO/OG**: og:image `https://gmgn.ai/static/GMGN.png` (680×340 png), `og:local en`, `og:site_name gmgn`, twitter `@gmgnai` summary_large_image, Facebook `base:app_id 699d1c3db4708d1c5a86ca2b`, favicon `https://gmgn.ai/static/favicon2.ico`, apple-touch-icon.

## SEO / crawl surface

- **robots.txt** — opts INTO AI training: `Content-Signal: ai-train=yes, search=yes, ai-input=yes` for 35 AI/social crawlers (GPTBot, ClaudeBot, PerplexityBot, Grok, Applebot, …). Allows `/llms.txt`, `/trend`, `/trade`, `/rewards`, `/app`, `/ai`, `/about`, `/static/`, and `/sol|bsc|base|eth|monad|tron/token/`; blocks `/monitor /follow /portfolio /holding /watchlists /security /account /r/` and all `/…/address/` paths.
- **sitemap.xml** — index of 4 sitemaps: `gmgn.ai/static/sitemap-pages.xml`, `gmgn.ai/blog/sitemap.xml`, `docs.gmgn.ai/index/sitemap-pages.xml`, `memecoin.gmgn.ai/sitemap.xml`. Reveals the live estate: main site + blog + docs + memecoin subdomain.

## Config registry internals

### swap-vendor.json — cross-chain swap routing engine

5 vendors: **Relay** (anyPair; NATIVE on 12 chains; USDC bsc/hyperevm/monad/sol; USDT base/bsc/sol; USD1 bsc/sol; USDG robinhood), **deBridge** (NATIVE bsc/sol), **Bridgers** (directed, only XLayer OKB ↔ sol/bsc/robinhood — 10 explicit routes), **CCTP** (directed, USDC→ARC; ARC's native IS USDC, so arc side is always NATIVE), **LiFi** (anyPair).

Per-chain vendor priority + **reserve floors**: default `0.0005` cross / `0.0002` same; sol & hyperevm `0.008/0.005`; monad `60/50` (native MON); arc & stable `0.5/0.5`; xlayer `0.05/0.05`. Vendor list order = default pick (relay first; on base CCTP sits before LiFi so eth USDC→base USDC defaults to the Circle native bridge). Bridgers is present on bsc/robinhood chains only so the xlayer pairing resolves (fail-closed: unlisted combos are disabled).

- `hopVia: USDG` for robinhood (RH-stable as hop asset).
- `pairWhitelist`: stable (robinhood/base/bsc/sol/eth, 7 pairs) and xlayer (5 pairs) as a second line of defense behind directed routes.
- **USD amount cap**: every cross-chain swap 5–20,000 USD (fail-open if price unavailable; `FALLBACK_USD_RANGE` hard-coded in frontend `parse.ts` so a broken config can't silently lift the cap). Bridgers overrides min to 20. Same-chain swaps are uncapped.
- **No-gas USDC path**: same-chain + USDC→native ≥ 2 USDC, GMGN pays gas, skips balance check — deliberately lower threshold than normal swaps.

### global_config.json — gray-release (A/B) ownership

- `perpGray`: 46 user UUIDs + 50% of devices.
- `trendingMixGray`: 5 users + 100%.
- `trenchesProgressScaleV2Gray`: 70% of devices.
Per-user-ID AND device-percent rollout model.

### lpp.json v80 — launchpad registry (211 platform entries / 13 networks)

| network | platforms | headline launchpads |
|---|---|---|
| robinhood | 68 | Pons, Long.xyz, Bankr, Trench, Flap*, Pools*, Flat*, stonks, Let'sCash, Bow, Varo |
| sol | 41 | Pump.fun, Mayhem, Maxipad-style RWA (Bags RWA, Stoxes RWA), Moonshot, Bankr, Zora, Tren.ch, Stonk.fun, Dynamic BC, xStocks, Raydium/Meteora/Orca (pools) |
| bsc | 25 | FourMeme, Cubepeg, Likwid, GoPlus Creator, OpenFour, X Mode, Flap*, Brew, Stoxes, Memecoin.fun V2, Printr, Cheesepad, Luna.fun, Pancake/Uniswap (pools) |
| base | 18 | Flap, Clanker, Bankr, o1 RWA, Fl Launch, Zora Content/Creator, Virtuals Unicorn, Klik, Aerodrome/Uniswap/Pancake/Sushi (pools) |
| xlayer | 8 | Flap, Ignix, Eulr, Dyor V2/V3, Uni V2/V3/V4 pools |
| arbitrum | 8 | Odys, Uni V2/V3/V4, Pancake V2/V3, Camelot V2/V3 pools |
| eth | 10 | Trench, Clanker, Klik, Livo, Stroid, Printr, Programmable, Uni V2/V3/V4 pools |
| others | 33 | tron SunPump; monad Nadfun/Kuru/Bonad.fun; hyperevm alt.fun/motion.meme/Liquid Launch; megaeth Trench/Kumbaya; arc 19 (Warp/Argus/RadarDEX/Dyor/o1/Trench/…) |

Pool-style platforms (Uniswap/Pancake/Camelot pools) set `disablePages: [Completing, Completed]` and `noInternalExternal`. `outDexKey` maps each platform to its swap-DEX (pumpamm, raydium, meteora, uniswapV2/3/4, pancake, sushiSwap, dyorswap, hyperswap, likwid, liquid, kumbaya, potatoswap, trends, sunswap, vertigo).

### quotes.json v31 — RWA quote matrix (1,724 pinned rows / 5 chains)

- **robinhood 193** — spot US equities: RDDT, NOW, SPY, AMZN, AVGO, UMC, QCOM, XLK, ASTS, GME…
- **sol 519** — international equity tokens (`-ON/-OX/-AX`: AAPLON, NVAX… GLWON) + tokenized: OUSG, ZBTC, TOPENAI, w-X wraps.
- **bsc 557** — US-wrapped `-B` (NVDAB, GMEB, …) + international `-ON`.
- **base 361** — tokenized US equities + RWA funds (DGLD, EQTY, ESX, GB, ANT, …).
- **xlayer 94** — `w-prefixed` wrapped equities (wTSLAx, wNVDAx, wAAPLx, wQQQx, wGOOGLx…).
Every row: ca, title, icon dark/light, `tags:["rwa"]`, tipsKey.

### CLI contract (recovered from skills.json install blocks)

The terminal's bot surface is a typed CLI. Commands captured from the install text of all 60 skills:

- `gmgn-cli market trending|trenches|hot-searches|kline|signal` (`--chain`, `--interval`, `--platform Pump.fun/bankr/fourmeme`, `--type new_creation|completed|near_completion`, `--min-renowned-count`, `--mc-min/max`, `--min-liquidity`, `--max-top-holder-rate`, `--max-bundler-rate`, `--max-fresh-wallet-rate`, `--signal-type 1..18`, `--raw`, `--resolution`)
- `gmgn-cli token info|security|pool|holders|traders` (`--order-by amount_percentage|profit`, `--tag smart_degen|renowned`)
- `gmgn-cli portfolio stats|holdings|activity|token-balance|created-tokens` (`--period 7d/30d`, `--order-by token_ath_mc`)
- `gmgn-cli swap|multi-swap` (anti-MEV, `--slippage`, `--percent`, per-wallet `--input-amount` JSON, up to 100 accounts)
- `gmgn-cli order strategy create|list|cancel` (`--order-type limit_order|smart_trade`, `--sub-order-type buy_low|take_profit|mix_trade`, `--condition-orders` JSON: profit_stop/loss_stop/profit_stop_trace with `price_scale/sell_ratio/drawdown_rate`, `--group-tag LimitOrder|STMix`)
- `gmgn-cli track follow-wallet|kol|smartmoney|follow-tokens` (`--side buy|sell`)
- `gmgn-cli gas-price --chain …`
- `gmgn-cli cooking stats|create` (`--dex pump|fourmeme|clanker|flap`, `--buy-amt`, `--auto-slippage`, `--is-cashback`, `--fourmeme-rate-conf` {fee_rate, recipient_rate, burn_rate, divide_rate, liquidity_rate, min_sharing, recipient_address}, `--flap-rate-conf` {buy_tax_rate, sell_tax_rate, mkt_bps, deflation_bps, dividend_bps, lp_bps, minimum_share_balance, recipient_type gift, twitter_account, split_conf})
- Media skills (`opentwitter`, `opennews`, `get-twitter-following-posts`) install **MCP servers** instead of CLI (news MCP: 84+ sources; X MCP: 15 tools; paid follow-timeline API).

## Crawl / export manifests

- **`pages/_index.json`** — 85-entry crawl manifest (route, live URL, file, HTTP status, cf_challenge, next_data flag, title, bytes): status 200×83 / 404×2 (/404, /_error), zero Cloudflare-challenged, all pages carry `__NEXT_DATA__`.
- **`asset_index.json`** (`mirror_assets.py`, generated 2026-09-14T03:28+0100) — budget 300 MB, spent ~37 MB: **435 tracked** (410 ok + 13 preexisting + 12 failed: 7×404 incl. browserconfig.xml, 5×S3 403). External image CDNs discovered but sample-only: `img-v1.raydium.io`, `raw.githubusercontent.com`, `static.alchemyapi.io`, `walletconnect.org`, `www.dextools.io`.

## Fonts (22 files)

MiSans (8), Geist-Lite (2), Switzer (2), plus one each Poppins, Inter, Mona_Sans, E1234, IBM_Plex_Sans, HarmonyOS_Sans, Roboto, PublicSans, Geist, Manrope.

## File inventory (611 files total)

- **172 HTML** = 88 `webroot/` route tree + 84 `pages/` flat crawl (same app, two representations; dynamic routes expanded to literal addresses in webroot, `[x]` placeholders in pages).
- **422 images+fonts** = 231 png, 89 webp, 72 svg, 4 jpg, 2 gif, 2 ico (400) + 22 font files.
- **15 JSON** configs (see Data & config layer + registry internals above).
- **2 SEO files**: robots.txt (AI-in) + sitemap.xml (4-sitemap index) + 2 manifests (asset_index.json, pages/_index.json).

## Static content inventory (the only server-rendered text)

Only 5 pages carry static text; everything else hydrates from APIs:

- `/` (index) — terminal landing: new-token monitoring (Pump.fun/FourMeme/Raydium/letsbonk filters), 1-min-updated trending rankings, holder-structure analytics (insider/rat ratio, bundle %, DEV %, Top-10), auto security audit (LP burned/honeypot/renounced/mintable), smart-money copy trading, KOL & wallet real-time watchlist, zero-latency execution + TP/SL/trailing strategies + multi-wallet batch, wallet P&L analytics (realized/unrealized, win rate, risk/reward, history, dev ATH), 5-category Trading API + AI skills (Claude/GPT callable).
- `/ai` + `/ai/skills/x` — the Skills Market hub: searchable catalog (`gmgn.ai/static/opstatic/skills.json` as discovery source), category tabs (All/Trading/Data Analytics/Monitor/News & Media/Cooking), per-skill Install with hot-level (🔥🔥🔥 = Top, 🔥🔥 = Popular, 🔥 = new).
- `/about` — marketing: trade-in-seconds copy, "Follow to Earn" (up to 80% success by tracking experts), 100+ daily opportunities w/ >70% yielding >120% ROI, cross-chain swap saves up to 50% fees, brand story ("Good Morning, Good Night"), TradingView ETH USD chart embed (RSI/MACD).
- `/app` — mobile app: TestFlight/App Store/Google Play/APK download + QR, feature bullets (real-time alerts, live k-lines, smart-money auto buy/sell, wallet P&L on mobile).
- Global static header/nav (from the ai page shell): **Trenches · Trending · Wallet · Copy · Monitor · Follow · Portfolio · Rewards · Up/Down**.
- **Extra dex/bot logos**: raydium, dexscreener, coinmarketcap, honeypot link (via Go+), bullx/photon/trojan (external terminals).