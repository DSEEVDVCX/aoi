/* Runs the dashboard's refresh loop in node against a fake browser.
 *
 * Why this exists: on 2026-08-31 a panel was added to the `Promise.all` array
 * but not to the destructuring list, and the live page died with
 * `watchMarket is not defined` — while pytest and both render checks stayed
 * green, because neither of them ever *executes* the loop. Only driving the
 * real page caught it. This harness closes that gap: it executes `tick()`.
 *
 * And it pins the load-time fix itself (2026-09-02): the heavy half must not
 * hold the light half. The fake `getJSON` keeps `/api/networks` pending
 * forever, and the check asserts the light panels have already painted — that
 * is the whole property, and a stray `await` in front of `tickHeavy()` breaks
 * this check and nothing else.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const page = readFileSync(join(here, "..", "static", "index.html"), "utf8");
const script = page.slice(page.indexOf("<script>"), page.lastIndexOf("</script>"));

const START = "/* The four cached panels ride their own promise";
/* `lastRows` is injected as a parameter, so the region stops just before the
   page declares it — otherwise `new Function` sees it declared twice. */
const END = "const lastRows = { perf: [], tl: null, wl: null };";
const region = script.slice(script.indexOf(START), script.indexOf(END));
if (!region.includes("async function tick()")) {
  throw new Error("loop region not found — did the marker comment change?");
}

/* --- the fake browser --- */
const painted = [];
const els = new Map();
function el(id) {
  if (!els.has(id)) els.set(id, { id, innerHTML: "", textContent: "", classList: { add() {}, remove() {} } });
  return els.get(id);
}
const document = {
  getElementById: (id) => el(id),
  querySelector: () => ({ classList: { add() {}, remove() {} } }),
};

/* Every panel that resolves is recorded by name, so "did the light half paint
   while the heavy half was still pending" is a plain array membership test. */
function paint(id, fn) {
  fn();
  painted.push(id);
}

/* /api/networks never resolves: the heavy half is permanently in flight. */
let networksAsked = 0;
const PAYLOAD = {
  "/api/status": { recorder: {} },
  "/api/api-health": { checks: [] },
  "/api/signals": { signals: [] },
  "/api/watchlist": { watchlist: [{ token_address: "A", network_id: "1", tick_count: 1 }] },
  "/api/errors": { errors: [] },
  "/api/storage": {},
  "/api/bars": {},
  "/api/performance": { performance: [], summary: {}, comparison: {} },
  "/api/signal-timeline": { series: [] },
  "/api/control-progress": {},
  "/api/provider-keys": { pools: [], keys: [], providers: [] },
  "/api/fomo-account": null,
  "/api/counts": { market_ticks: 1 },
  "/api/labeling": { live: false },
  "/api/watchlist-market": { market: {} },
};
async function getJSON(url) {
  const path = url.split("?")[0];
  if (path === "/api/networks") { networksAsked += 1; return new Promise(() => {}); }
  if (!(path in PAYLOAD)) throw new Error("unstubbed url: " + path);
  return PAYLOAD[path];
}
const getOptionalJSON = async (url, fallback) => { try { return await getJSON(url); } catch { return fallback; } };

const stub = () => {};
const loop = new Function(
  "document", "paint", "getJSON", "getOptionalJSON", "heavy", "lastRows", "esc",
  "paused", "inflight", "heavyTick", "HEAVY_EVERY", "perfLimit", "perfSort",
  "renderHero", "renderTiles", "renderPerf", "renderPerfSummary", "renderComparison",
  "renderControlProgress", "renderTimeline", "renderHealth", "renderWatchlist",
  "renderSignals", "renderErrors", "renderProviderKeys", "renderFomoAccount",
  "renderStatus", "renderApi", "renderNetworks", "renderLabeling",
  `${region}\n return { tick, tickHeavy, heavy, lastLight, get heavyInflight() { return heavyInflight; } };`,
);

const heavy = { counts: null, networks: null, labeling: null, watchMarket: null };
const api = loop(
  document, paint, getJSON, getOptionalJSON, heavy, { perf: [], tl: null, wl: null },
  (s) => String(s), false, false, 0, 6, 12, { key: "peak_pct", dir: "desc" },
  stub, stub, stub, stub, stub, stub, stub, stub, stub, stub, stub, stub, stub,
  stub, stub, stub, stub,
);

/* --- the checks --- */
await api.tick();          // resolves when the LIGHT half is done, not the heavy one

const LIGHT_PANELS = ["perf", "timeline", "health", "watchlist", "signals", "errors"];
const results = [
  ["tick() resolved with /api/networks still pending", networksAsked === 1, true],
  ["light panels painted anyway", LIGHT_PANELS.every(p => painted.includes(p)), true],
  ["heavy panels did NOT paint yet", painted.includes("network-summary"), false],
  ["hero/tiles held back (they need both halves)", painted.includes("hero"), false],
  ["heavy half still in flight", api.heavyInflight, true],
  ["light payload kept for the heavy repaint", Array.isArray(api.lastLight.wl), true],
  ["no panel painted twice in one cycle", painted.length === new Set(painted).size, true],
];

/* A second tick must not stack another heavy fetch on the pending one. */
await api.tick();
results.push(["second tick did not re-ask /api/networks", networksAsked === 1, true]);

let bad = 0;
for (const [label, actual, want] of results) {
  const ok = actual === want;
  if (!ok) bad += 1;
  console.log(`${ok ? "✓" : "✗"} ${label.padEnd(50)} = ${actual}`);
}
console.log(bad === 0 ? `\nAll ${results.length} loop checks passed.` : `\n${bad} loop check(s) FAILED.`);
process.exit(bad === 0 ? 0 : 1);
