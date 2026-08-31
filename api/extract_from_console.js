// ============================================================================
//  Extract fomo.family secrets — paste it into the browser Console (F12) after signing in
// ============================================================================
//  Steps:
//   1) Open https://fomo.family in ordinary Chrome and sign in normally.
//   2) Press F12  ->  the Console tab.
//   3) Paste this whole file and press Enter.
//   4) A file named privy_state.json will be downloaded — move it to the api/
//      folder, then run:
//        python import_privy_state.py privy_state.json
//  There is no automation of any kind, so Google doesn't block you.
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
    console.error("Did not find privy:refresh_token — make sure you are actually signed in to fomo.family.");
    return;
  }

  const blob = new Blob([JSON.stringify(creds, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "privy_state.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  console.log("%c[OK] privy_state.json downloaded — move it to the api/ folder and run import_privy_state.py",
    "color:#0a0;font-weight:bold");
  console.log("Captured fields:", Object.keys(creds).filter((k) => creds[k]).join(", "));
})();
