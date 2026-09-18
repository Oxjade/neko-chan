import { readFileSync, writeFileSync } from "node:fs";

const SUIPUMP_ENTRY = {
  launchpad_platform: "suipump",
  tags: ["suipump", "s_anchor", "biz_SuiPump", "total_tax"],
  defaultValue: true,
  name: "SuiPump",
  filterName: "SuiPump",
  href: "https://suipump.org/token/${address}",
  colorLight: "#84CC16",
  colorDark: "#BEF264",
  cardBgColorDark: "#1A2411",
  cardBgColorLight: "#F2FBE5",
  tipsInfo: "SuiPump",
  logoSrcDark: "/static/lpp/suipump_16px_bold_s.svg",
  logoSrcLight: "/static/lpp/suipump_16px_bold_s.svg",
  migrationNote: "Graduates to Cetus CLMM",
};

const REMOVE_SOL = new Set([
  "Pump.fun",
  "pump_agent",
  "pump_mayhem",
  "pump_mayhem_agent",
  "pool_pump_amm",
]);

const paths = [
  "/home/carnage/tradebotpro/frontend/static/config/lpp.json",
  "/home/carnage/tradebotpro/frontend/static/config/lpp-test.json",
];

for (const p of paths) {
  const data = JSON.parse(readFileSync(p, "utf8"));
  const sui = data.networks.Sui;
  const sol = data.networks.sol;

  if (sui.some((x) => x.launchpad_platform === "suipump")) {
    console.log(`${p}: suipump entry already present, skipping add`);
  } else {
    sui.unshift(SUIPUMP_ENTRY);
    console.log(`${p}: added suipump entry (Sui[0])`);
  }

  const before = sol.length;
  const filtered = sol.filter((x) => !REMOVE_SOL.has(x.launchpad_platform));
  const removed = sol.filter((x) => REMOVE_SOL.has(x.launchpad_platform));
  data.networks.sol = filtered;
  console.log(`${p}: sol ${before} -> ${filtered.length}, removed: ${removed.map((r) => r.launchpad_platform).join(", ")}`);

  writeFileSync(p, JSON.stringify(data, null, 1) + "\n", "utf8");
  console.log(`${p}: written (${data.version})`);
}