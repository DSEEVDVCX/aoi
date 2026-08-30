// فحصٌ تنفيذيّ لرسم بلاطات النظرة العامة: حمولة meta خبيثة يجب أن تبقى نصّاً.
// يستخرج الدوالّ من الصفحة نفسها ويشغّل renderTiles بلا متصفّح أو شبكة.
// التشغيل: node dashboard/tools/check_tile_render.mjs [index.html اختياري]
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const target = process.argv[2] ? resolve(process.argv[2]) : join(here, "..", "static", "index.html");
const html = readFileSync(target, "utf-8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const utilityStart = script.indexOf("/* ---------- أدوات ---------- */");
const utilityEnd = script.indexOf("/* أسعار العملات الصغيرة", utilityStart);
const utility = script.slice(utilityStart, utilityEnd);
const agoStart = script.indexOf("function ago");
const agoEnd = script.indexOf("// «أوّل التقاط»", agoStart);
const ago = script.slice(agoStart, agoEnd);
const render = script.slice(
  script.indexOf("function renderTiles"),
  script.indexOf("/* --- أداء الإشارات"),
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
  "لا عنصر img خبيث": [!out.includes("<img src=x"), true],
  "اسم الشبكة الخبيث مُهرَّب": [out.includes("&lt;img src=x"), true],
  "وصف الشبكات الخبيث مُهرَّب": [(out.match(/&lt;img src=x/g) || []).length >= 3, true],
  "لا undefined في البلاطات": [!out.includes("undefined"), true],
};

let bad = 0;
for (const [name, [actual, expected]] of Object.entries(checks)) {
  const ok = actual === expected;
  if (!ok) bad += 1;
  console.log(`${ok ? "✓" : "✗"} ${name.padEnd(32)} = ${actual}`);
}
if (bad) process.exit(1);
console.log(`\n${Object.keys(checks).length} فحوص بلاطات سليمة.`);
