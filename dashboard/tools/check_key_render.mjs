// فحصٌ بلا متصفّح لرسم لوحة المفاتيح: يستخرج الدوالّ من الصفحة نفسها ويشغّلها
// على حمولةٍ تغطّي كلّ حالة (سليم/مبرَّد/موقوف/معطَّل، واسمُ حسابٍ خبيث).
// أداةُ تطوير — تُشغَّل يدوياً: node dashboard/tools/check_key_render.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "static", "index.html"), "utf-8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const slice = script.slice(
  script.indexOf("function keyStateText"),
  script.indexOf("/** يجلب لوحة المفاتيح"),
);

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
const num = (n) => String(n);
const ago = () => "قبل قليل";
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
    { provider: "helius", title: "Helius · سولانا", env: false, env_name: "HELIUS_API_KEY",
      keys: 3, enabled: 2, owners: ["chain"], disabled_by: [] },
    { provider: "goldrush", title: "GoldRush · إعادة EVM", env: true, env_name: "GOLDRUSH_API_KEY",
      keys: 1, enabled: 1, owners: ["chain", "replay"], disabled_by: ["chain", "replay"] },
  ],
  keys: [
    { provider: "helius", slot: 0, pool_slot: 0, label: "الحساب الرئيسي", tail: "wxyz",
      enabled: true, env: false, cooled_by: [], in_use: true, provider_disabled: false,
      probe: null, level: "good" },
    { provider: "helius", slot: 1, pool_slot: 1, label: "<img src=x onerror=alert(1)>",
      tail: "ab12", enabled: true, env: false, cooled_by: ["chain"], in_use: false,
      provider_disabled: false,
      probe: { level: "warn", detail: "محدود مؤقّتاً", status: 429, at: "2026-08-18T15:00:00+00:00" },
      level: "warn" },
    { provider: "helius", slot: 2, pool_slot: null, label: "", tail: "", enabled: false,
      env: false, cooled_by: [], in_use: false, provider_disabled: false, probe: null, level: "off" },
    { provider: "goldrush", slot: 0, pool_slot: 0, label: "حساب GoldRush", tail: "q9q9",
      enabled: true, env: true, cooled_by: [], in_use: true, provider_disabled: true,
      probe: { level: "bad", detail: "نفد الرصيد — HTTP 402", status: 402, at: "2026-08-18T15:00:00+00:00" },
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

const checks = {
  "نقاط بالترتيب الصحيح": (out.match(/keydot (good|bad|warn|off)/g) || []).join(" | "),
  "اسم حساب خبيث مُهرَّب": !out.includes("<img src=x") && out.includes("&lt;img"),
  "بلا اسم حساب": out.includes("بلا اسم حساب"),
  "تحذير البيئة": out.includes("مضبوط في البيئة"),
  "تحذير أقلّ من الحدّ": out.includes("أقلّ من 2"),
  "عدد الأزرار (3 لكلّ سطر)": (out.match(/data-act="\w+"/g) || []).length,
  "أسطر": (out.match(/class="keyrow/g) || []).length,
  "قائمة المزوّدين مُلئت": SELECT.innerHTML.includes("Helius"),
  "سطر الحوض المعطَّل": out.includes("نفد رصيد"),
  "لا قيمة كاملة في المخرَج": !out.includes("AAAABBBB"),
};
for (const [name, value] of Object.entries(checks)) console.log(name.padEnd(28), "=", value);
