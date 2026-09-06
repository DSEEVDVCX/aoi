// A browserless check of the key panel rendering: it extracts the functions
// from the page itself and runs them on a payload covering every state
// (healthy/cooling/disabled/off, plus a malicious account name).
// `run_tests.ps1` runs it and it exits 1 on the first failing check — the
// page's rendering is JavaScript that pytest never touches, so without this
// there is no guard on it at all. It can also be run manually:
//     node dashboard/tools/check_key_render.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "static", "index.html"), "utf-8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const slice = script.slice(
  script.indexOf("function keyStateText"),
  script.indexOf("/** Fetches the key panel alone"),
);

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
const num = (n) => String(n);
const ago = () => "just now";
const SELECT = { options: { length: 0 }, innerHTML: "" };
const HOST = { innerHTML: "" };
const MSG = { textContent: "", className: "" };
const document = {
  getElementById: (id) => (id === "key-provider" ? SELECT : id === "key-msg" ? MSG : HOST),
};

const { renderProviderKeys } = new Function(
  "esc", "num", "ago", "document",
  slice + "\nreturn { renderProviderKeys, keyStateText, fillKeyProviders };",
)(esc, num, ago, document);

const payload = {
  min_keys: 2,
  providers: [
    { provider: "helius", title: "Helius · Solana", env: false, env_name: "HELIUS_API_KEY",
      keys: 3, enabled: 2, owners: ["chain"], disabled_by: [] },
    { provider: "goldrush", title: "GoldRush · EVM replay", env: true, env_name: "GOLDRUSH_API_KEY",
      keys: 1, enabled: 1, owners: ["chain", "replay"], disabled_by: ["chain", "replay"] },
  ],
  keys: [
    { provider: "helius", slot: 0, pool_slot: 0, label: "Main account", tail: "wxyz",
      key_id: "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
      enabled: true, env: false, cooled_by: [], in_use: true, provider_disabled: false,
      probe: null, level: "good" },
    { provider: "helius", slot: 1, pool_slot: 1, label: "<img src=x onerror=alert(1)>",
      tail: "ab12", key_id: "112233445566778899aabbccddeeff00",
      enabled: true, env: false, cooled_by: ["chain"], in_use: false,
      provider_disabled: false,
      probe: { level: "warn", detail: "temporarily rate-limited", status: 429, at: "2026-08-18T15:00:00+00:00" },
      level: "warn" },
    { provider: "helius", slot: 2, pool_slot: null, label: "", tail: "", enabled: false,
      key_id: "aabbccddeeff00112233445566778899",
      env: false, cooled_by: [], in_use: false, provider_disabled: false, probe: null, level: "off" },
    { provider: "goldrush", slot: 0, pool_slot: 0, label: "GoldRush account", tail: "q9q9",
      key_id: "99887766554433221100ffeeddccbbaa",
      enabled: true, env: true, cooled_by: [], in_use: true, provider_disabled: true,
      probe: { level: "bad", detail: "out of credits — HTTP 402", status: 402, at: "2026-08-18T15:00:00+00:00" },
      level: "bad" },
  ],
  pools: [
    { owner: "chain", provider: "goldrush", keys: 1, blocked: 0, available: 1, index: 0,
      blocked_index: [], rotations: 4, cooldown_seconds: 60, disabled: true,
      at: "2026-08-18T15:00:00+00:00", age_seconds: 12, stale: false, level: "bad" },
  ],
};

renderProviderKeys(payload);
const out = HOST.innerHTML;

// Every check is a pair [actual, expected]: before this, the tool printed
// values without judging, so whoever ran it read the ten with their own eyes
// and decided — and wiring it into `run_tests.ps1` in that shape would have
// turned it into theater: "false" gets printed, the exit code stays zero, and
// the check goes green.
const checks = {
  "dots in the right order": [(out.match(/keydot (good|bad|warn|off)/g) || []).join(" | "),
    "keydot good | keydot warn | keydot off | keydot bad"],
  "malicious account name escaped": [!out.includes("<img src=x") && out.includes("&lt;img"), true],
  "no account name": [out.includes("no account name"), true],
  "environment warning": [out.includes("set in the environment"), true],
  "below-minimum warning": [out.includes("fewer than 2"), true],
  "button count (3 per row)": [(out.match(/data-act="\w+"/g) || []).length, 12],
  "rows": [(out.match(/class="keyrow/g) || []).length, 4],
  "provider list filled": [SELECT.innerHTML.includes("Helius"), true],
  "disabled pool row": [out.includes("out of credits"), true],
  "no full value in the output": [!out.includes("AAAABBBB"), true],
  // The identity travels with the button (one per button, i.e. three per row):
  // without this guard a `undefined` passes as text, every edit becomes a 409
  // — and the page looks healthy.
  "identity on every button": [(out.match(/data-key-id="[0-9a-f]{32}"/g) || []).length, 12],
  "no 'undefined' in the output": [!out.includes("undefined"), true],
};

let bad = 0;
for (const [name, [actual, expected]] of Object.entries(checks)) {
  const ok = actual === expected;
  if (!ok) bad += 1;
  console.log(`${ok ? "✓" : "✗"} ${name.padEnd(34)} = ${actual}${ok ? "" : `   (expected: ${expected})`}`);
}
if (bad > 0) {
  console.error(`\n${bad} of ${Object.keys(checks).length} checks failed.`);
  process.exit(1);
}
console.log(`\nAll ${Object.keys(checks).length} checks passed.`);
