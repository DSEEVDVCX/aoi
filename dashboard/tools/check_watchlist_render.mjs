// An executable check of the watchlist rendering: a malicious payload (symbol,
// address, network, source) must stay text, coins without market data must
// render gracefully, and sort/filter/search must re-render client-side.
// It extracts the functions from the page itself and runs them with no browser
// and no network.
// Run: node dashboard/tools/check_watchlist_render.mjs [optional index.html]
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const target = process.argv[2] ? resolve(process.argv[2]) : join(here, "..", "static", "index.html");
const html = readFileSync(target, "utf-8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

// helpers: TYPE_META → links → utilities → price/pct/signed/shortAddr →
// ago/firstCatch/remaining. Everything before getJSON is pure; cssVar reaches
// getComputedStyle, which the harness stubs.
const helpers = script.slice(
  script.indexOf("/* Series colors are read from CSS"),
  script.indexOf("async function getJSON"),
);
// sparkline + its `el` helper need createElementNS — the harness provides it.
const spark = script.slice(
  script.indexOf("/* ---------- SVG charts"),
  script.indexOf("function stackedColumns"),
);
const networks = script.slice(
  script.indexOf("const NETWORK_META"),
  script.indexOf("function coveragePct"),
);
const wl = script.slice(
  script.indexOf("/* --- watchlist: market view"),
  script.indexOf("/* --- end watchlist --- */") + "/* --- end watchlist --- */".length,
);

// --- the fake browser -----------------------------------------------------
const svgEl = () => ({
  attrs: {}, children: [],
  setAttribute(k, v) { this.attrs[k] = v; },
  append(c) { this.children.push(c); },
  replaceChildren(...c) { this.children = c; },
});
const sparkCell = { dataset: { spark: "0" }, sparks: null, replaceChildren(...c) { this.sparks = c; } };
const hosts = {
  "wl-bar": { innerHTML: "", querySelectorAll: () => [] },
  watchlist: { innerHTML: "", querySelectorAll: (sel) => (sel === "[data-spark]" ? [sparkCell] : []) },
};
const document = {
  getElementById: (id) => hosts[id] || { innerHTML: "" },
  createElementNS: (_ns) => svgEl(),
};
const getComputedStyle = () => ({ getPropertyValue: () => "" });

const { renderWatchlist, wlSort, wlFilter, setWlQuery } = new Function(
  "document", "getComputedStyle", "lastRows",
  `${helpers}\n${spark}\n${networks}\n${wl}\n`
    + "return { renderWatchlist, wlSort, wlFilter, setWlQuery: (v) => { wlQuery = v; } };",
)(document, getComputedStyle, { wl: null });

// --- the payload ----------------------------------------------------------
const attack = '<img src=x onerror="globalThis.pwned=1">';
const inOneHour = new Date(Date.now() + 3600e3).toISOString();
const inForty = new Date(Date.now() + 40 * 3600e3).toISOString();
const rows = [
  { token_address: attack, network_id: "1399811149", source: "multi_user_buy",
    first_seen_at: "2026-08-30T00:00:00Z", watch_until: inForty, tick_count: 41 },
  { token_address: "AddrBBBB1111", network_id: 56, source: "large_buy",
    first_seen_at: "2026-08-30T00:00:00Z", watch_until: inOneHour, tick_count: 5, is_control: 1 },
  { token_address: "AddrCCCC2222", network_id: 9999, source: "mystery_source",
    first_seen_at: "2026-08-29T00:00:00Z", watch_until: inForty, tick_count: 2 },
];
const market = {
  "AddrBBBB1111|56": { symbol: attack, last_px: 0.0000123, change_pct: 55.5, peak_pct: 123.4,
    spark: [1, 2, 3, 2.5] },
  "AddrCCCC2222|9999": { symbol: "CCC", last_px: null, change_pct: null, peak_pct: null, spark: [] },
};

const barHtml = () => hosts["wl-bar"].innerHTML;
const tableHtml = () => hosts.watchlist.innerHTML;
const resetFilters = () => { wlFilter.net = null; wlFilter.source = null; };

renderWatchlist(rows, market);
const priceFirst = tableHtml();
const firstBar = barHtml();

wlSort.key = "last_px"; wlSort.dir = "desc";
renderWatchlist(rows, market);
const sortedByPrice = tableHtml();

resetFilters(); wlSort.key = "first_seen_at"; wlSort.dir = "desc";
wlFilter.net = "56";
renderWatchlist(rows, market);
const filteredByNet = tableHtml();
const netBar = barHtml();

resetFilters();
setWlQuery("ccc");
renderWatchlist(rows, market);
const searched = tableHtml();
const searchBar = barHtml();

setWlQuery("zzz");
renderWatchlist(rows, market);
const noMatch = tableHtml();

setWlQuery("");
wlSort.key = "change_pct"; wlSort.dir = "desc";
renderWatchlist(rows, market);   // the priced, falling coin is row 0 → its spark is painted
const sparkPainted = sparkCell.sparks;

setWlQuery("");
renderWatchlist([], {});
const noRows = tableHtml();

const checks = {
  "no malicious img element": [!tableHtml().includes("<img src=x"), true],
  "malicious address escaped": [priceFirst.includes("&lt;img src=x"), true],
  "malicious symbol escaped": [priceFirst.includes("&lt;img src=x"), true],
  "no undefined in the table": [!priceFirst.includes("undefined"), true],
  "no undefined in the bar": [!firstBar.includes("undefined"), true],
  "missing market renders dash": [priceFirst.includes("—"), true],
  "unknown network gets a fallback pill": [priceFirst.includes("net 9999"), true],
  "known network gets its name": [priceFirst.includes("BNB Smart Chain"), true],
  "control pill shown": [priceFirst.includes(">control<"), true],
  "all coins counted": [firstBar.includes("3 of 3 coins"), true],
  "price sort sinks the unpriced coin": [
    sortedByPrice.indexOf("AddrBBBB1111") < sortedByPrice.indexOf("&lt;img src=x"), true],
  "network filter to 1 of 3": [netBar.includes("1 of 3 coins"), true],
  "network filter keeps the match": [filteredByNet.includes("AddrBBBB1111"), true],
  "search by symbol to 1 of 3": [searchBar.includes("1 of 3 coins"), true],
  "search keeps the match only": [searched.includes("AddrCCCC2222") && !searched.includes("AddrBBBB1111"), true],
  "no search match has an empty state": [noMatch.includes("No coin matches the current filter"), true],
  "no rows has its own empty state": [noRows.includes("No active watched coins"), true],
  "sparkline painted for a priced coin": [
    Array.isArray(sparkPainted) && sparkPainted.length === 1
      && sparkPainted[0].children.length === 2, true],
};

let bad = 0;
for (const [name, [actual, expected]] of Object.entries(checks)) {
  const ok = actual === expected;
  if (!ok) bad += 1;
  console.log(`${ok ? "✓" : "✗"} ${name.padEnd(42)} = ${actual}`);
}
if (bad) process.exit(1);
console.log(`\nAll ${Object.keys(checks).length} watchlist checks passed.`);
