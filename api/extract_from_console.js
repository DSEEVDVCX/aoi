// ============================================================================
//  استخراج أسرار fomo.family — الصقه في Console المتصفح (F12) بعد تسجيل الدخول
// ============================================================================
//  الخطوات:
//   1) افتح https://fomo.family في Chrome العادي وسجّل الدخول بشكل طبيعي.
//   2) اضغط F12  ->  تبويب Console.
//   3) الصق كل هذا الملف واضغط Enter.
//   4) سيُنزَّل ملف باسم  privy_state.json  — انقله إلى مجلد  api/  ثم شغّل:
//        python import_privy_state.py privy_state.json
//  لا يوجد أي تشغيل آلي، لذا Google لا تحظرك.
// ============================================================================
(() => {
  const ls = window.localStorage;
  const get = (k) => { const v = ls.getItem(k); return v ? v.replace(/^"|"$/g, "") : null; };

  let appId = null, clientId = null;
  for (let i = 0; i < ls.length; i++) {
    const k = ls.key(i);
    const m = k && k.match(/^privy:([a-z0-9]{20,}):recent-login-method$/i);
    if (m) appId = m[1];
    const v = ls.getItem(k) || "";
    const c = v.match(/client-[A-Za-z0-9]{20,}/);
    if (c && !clientId) clientId = c[0];
  }

  const creds = {
    access_token: get("privy:token"),
    refresh_token: get("privy:refresh_token"),
    pat: get("privy:pat"),
    app_id: appId,
    client_id: clientId,
    ca_id: get("privy:caid"),
  };

  if (!creds.refresh_token) {
    console.error("لم أجد privy:refresh_token — تأكّد أنك سجّلت الدخول فعلاً في fomo.family.");
    return;
  }

  const blob = new Blob([JSON.stringify(creds, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "privy_state.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  console.log("%c[OK] تم تنزيل privy_state.json — انقله إلى مجلد api/ وشغّل import_privy_state.py",
    "color:#0a0;font-weight:bold");
  console.log("الحقول الملتقطة:", Object.keys(creds).filter((k) => creds[k]).join(", "));
})();
