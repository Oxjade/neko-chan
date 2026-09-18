import { readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";

const ROOT = "/home/carnage/tradebotpro/frontend/webroot";

const CHAINHOOD = `<!-- NK-CHAINHOOD -->
<style>
#__next [data-testid="chain-multi-select-trigger"],
#__next [data-testid="chain-switch-current"],
#__next [data-testid="chain-multi-select-panel"],
#__next [data-testid="chain-multi-select-options"],
#__next [data-testid="chain-multi-select-footer"],
#__next [aria-haspopup="dialog"][aria-controls^="radix"]{display:none!important}
</style>`;

const NAVHOOD = `<!-- NK-NAVHOOD -->
<style>
header a[href^="/trade"],header a[href^="/follow"],header a[href^="/perpetual"],header a[href^="/ai"],header a[href^="/copy"],header a[href^="/refer"],header a[href^="/kol"],header a[href^="/contest"],header a[href^="/bot"],header a[href^="/call"],header a[href^="/monitor"],
[data-testid="header-nav"] a[href^="/trade"],[data-testid="header-nav"] a[href^="/follow"],[data-testid="header-nav"] a[href^="/perpetual"],[data-testid="header-nav"] a[href^="/ai"],[data-testid="header-nav"] a[href^="/copy"],[data-testid="header-nav"] a[href^="/refer"],[data-testid="header-nav"] a[href^="/kol"],[data-testid="header-nav"] a[href^="/contest"],[data-testid="header-nav"] a[href^="/bot"],[data-testid="header-nav"] a[href^="/call"],[data-testid="header-nav"] a[href^="/monitor"],
[role="tab"][aria-controls$="-panel-monitor"]{display:none!important}
</style>`;

const TITLEHOOD = `<!-- NK-TITLEHOOD -->
<script>try{var NKT="Neko | The Fastest Sui Meme Trading Terminal";function nk(){if(document.title!==NKT){document.title=NKT;}}new MutationObserver(function(){nk()}).observe(document.head,{subtree:true,childList:true,characterData:true,attributes:true});nk();setInterval(nk,1500);}catch(e){}</script>`;

const LINKHOOD = `<!-- NK-LINKHOOD -->
<script>try{document.addEventListener("click",function(e){var a=e.target&&e.target.closest?e.target.closest("a[href]"):null;if(!a)return;var h=a.getAttribute("href")||"";if(h.indexOf("chain=sol")===-1)return;e.preventDefault();e.stopImmediatePropagation();e.stopPropagation();var u=h.replace("?chain=sol","").replace("&chain=sol","");a.setAttribute("href",u);location.assign(u);},true);}catch(e){}</script>`;

const HOOD = CHAINHOOD + NAVHOOD + TITLEHOOD + LINKHOOD;

function collectHtml(dir, depth) {
  const out = [];
  try {
    for (const e of readdirSync(dir)) {
      if (e === "_next" || e === "static") continue;
      const full = join(dir, e);
      let st;
      try { st = statSync(full); } catch { continue; }
      if (st.isDirectory()) {
        if (depth < 2) out.push(...collectHtml(full, depth + 1));
      } else if (e === "index.html") {
        out.push(full);
      }
    }
  } catch {}
  return out;
}

const files = new Set([join(ROOT, "index.html"), ...collectHtml(ROOT, 0)]);
let injected = 0, skipped = 0, errors = 0;
for (const f of files) {
  let h;
  try { h = readFileSync(f, "utf8"); } catch { errors++; continue; }
  if (!h.includes("</head>")) { skipped++; continue; }
  if (h.includes("NK-CHAINHOOD") && h.includes("NK-NAVHOOD") && h.includes("NK-TITLEHOOD") && h.includes("stopImmediatePropagation") && h.includes('a[href^="/monitor"]') && h.includes('aria-controls$="-panel-monitor"')) { skipped++; continue; }
  // remove any partial markers first, then inject fresh block
  h = h.replace(/<!-- NK-CHAINHOOD -->\s*<style>[\s\S]*?<\/style>\s*/g, "").replace(/<!-- NK-NAVHOOD -->\s*<style>[\s\S]*?<\/style>\s*/g, "").replace(/<!-- NK-TITLEHOOD -->\s*<script>[\s\S]*?<\/script>\s*/g, "").replace(/<!-- NK-LINKHOOD -->\s*<script>[\s\S]*?<\/script>\s*/g, "");
  h = h.replace("</head>", HOOD + "</head>");
  writeFileSync(f, h);
  injected++;
  if (!(injected % 10)) console.log("..." + injected);
}
console.log("scanned:", files.size, "| injected:", injected, "| already-ok:", skipped, "| errors:", errors);