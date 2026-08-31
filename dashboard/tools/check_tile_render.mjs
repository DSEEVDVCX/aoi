// An executable check of the overview tile rendering: a malicious meta payload
// must stay text. It extracts the functions from the page itself and runs
// renderTiles with no browser and no network.
// Run: node dashboard/tools/check_tile_render.mjs [optional index.html]
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const target = process.argv[2] ? resolve(process.argv[2]) : join(here, "..", "static", "index.html");
const html = readFileSync(target, "utf-8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const utilityStart = script.indexOf("/* ---------- utilities ---------- */");
const utilityEnd = script.indexOf("/* small coin prices", utilityStart);
const utility = script.slice(utilityStart, utilityEnd);
const agoStart = script.indexOf("function ago");
const agoEnd = script.indexOf('// "First catch"', agoStart);
const ago = script.slice(agoStart, agoEnd);
const render = script.slice(
  script.indexOf("function renderTiles"),
  script.indexOf("/* --- signal performance"),
);

const HOST = { innerHTML: "" };
const document = { getElementById: () => HOST };
const { renderTiles } = new Function(
  "document",
  `${utility}\n${ago}\n${render}\nreturn { renderTiles };`,
)(document);

const attack = '<img src=x onerror="globalThis.pwned=1">';
renderTiles(
  {
    active_watch_count: 1,
    evm_admission_networks: { [attack]: { percent: attack, paused: false } },
    evm_admission_paused_networks: [attack],
    cycle_crashes: 0,
  },
  { market_ticks: 1, outcomes: 1 },
  { coverage_pct: 100, with_bars: 1, active: 1, candles: 1 },
  { mb: 1, disk_warning: false, mb_per_day: 0 },
  { latency_ms: 1 },
  null,
);

const out = HOST.innerHTML;
const checks = {
  "no malicious img element": [!out.includes("<img src=x"), true],
  "malicious network name escaped": [out.includes("&lt;img src=x"), true],
  "malicious network description escaped": [(out.match(/&lt;img src=x/g) || []).length >= 3, true],
  "no undefined in the tiles": [!out.includes("undefined"), true],
};

let bad = 0;
for (const [name, [actual, expected]] of Object.entries(checks)) {
  const ok = actual === expected;
  if (!ok) bad += 1;
  console.log(`${ok ? "✓" : "✗"} ${name.padEnd(38)} = ${actual}`);
}
if (bad) process.exit(1);
console.log(`\nAll ${Object.keys(checks).length} tile checks passed.`);
