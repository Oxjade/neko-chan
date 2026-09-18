import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";

const targets = [
  "/home/carnage/tradebotpro/frontend/static/config/lpp.json",
  "/home/carnage/tradebotpro/frontend/static/config/lpp-test.json",
];

for (const file of targets) {
  const config = JSON.parse(readFileSync(file, "utf8"));
  const kept = { Sui: config.networks.Sui };
  const dropped = Object.keys(config.networks).filter((k) => k !== "Sui");
  config.networks = kept;
  const out = JSON.stringify(config, null, 2) + "\n";
  mkdirSync(dirname(file), { recursive: true });
  const tmp = file + ".tmp";
  writeFileSync(tmp, out);
  writeFileSync(file, out);
  try { mkdirSync("/tmp/nk-gates"); } catch {}
  writeFileSync("/tmp/nk-gates/last-strip.json", JSON.stringify({ file, keptSui: kept.Sui.length, dropped }, null, 2));
  console.log(`OK ${file}  kept=Sui(${kept.Sui.length})  dropped=${JSON.stringify(dropped)}`);
}
