import { readFileSync } from "node:fs";
const file = process.argv[2] ?? "frontend/static/config/lpp.json";
const path = file.startsWith("/") ? file : "/home/carnage/tradebotpro/" + file;
const cfg = JSON.parse(readFileSync(path, "utf8"));
const nets = Object.keys(cfg.networks ?? {});
const ok = nets.length === 1 && nets[0] === "Sui";
const rows = cfg.networks?.Sui ?? [];
const sui0 = rows[0];
const zeroOk = sui0 && sui0.filterName === "SuiPump" && sui0.maxValue === undefined;
console.log(`${file}: networks=${JSON.stringify(nets)} rows(Sui)=${rows.length}`);
if (!ok) { console.error("EXPECT: only-Sui — FAILED"); process.exit(1); }
if (rows.length < 1) { console.error("EXPECT: Sui rows present — FAILED"); process.exit(1); }
console.log(`lpp.json OK: only-Sui rows=${rows.length} Sui[0]=${sui0 ? sui0.filterName : "MISSING"}`);
