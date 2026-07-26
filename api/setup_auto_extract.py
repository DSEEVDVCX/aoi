"""ONE-TIME setup for unattended auto-extraction.

Run this exactly once. It opens Chrome, you log in to fomo.family, and it writes
the Privy credentials to the persistent state file (.privy_state.json). From then
on the API server refreshes the token automatically every minute with no browser
and no further logins — as long as the server (or a restart) runs within the
refresh-token validity window, which each successful renewal extends.

    python setup_auto_extract.py

After it finishes, just run the server normally:

    uvicorn fomo_api.main:app    (auto_bootstrap is on by default)

To confirm without the server, this script also does one immediate refresh + a
live leaderboard read so you can see real data flowing before you walk away.
Prints only handles/counts — never a token.
"""
from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("FOMO_API_UPSTREAM_IMPERSONATE", "true")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

APP = "https://fomo.family"
LOGIN_TIMEOUT = 300


async def _harvest(page):
    read = (
        "() => {"
        "  const out = {};"
        "  out.token = localStorage.getItem('privy:token');"
        "  out.refresh = localStorage.getItem('privy:refresh_token');"
        "  out.pat = localStorage.getItem('privy:pat');"
        "  out.caid = localStorage.getItem('privy:caid');"
        "  for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i);"
        "    const m=k&&k.match(/^privy:([a-z0-9]{20,}):recent-login-method$/i);"
        "    if(m){out.app_id=m[1];}"
        "    const v=localStorage.getItem(k)||'';const c=v.match(/client-[A-Za-z0-9]{20,}/);"
        "    if(c&&!out.client_id){out.client_id=c[0];}}"
        "  return out;}"
    )
    for _ in range(LOGIN_TIMEOUT):
        try:
            s = await page.evaluate(read)
        except Exception:
            await asyncio.sleep(1.0)
            continue
        if s and s.get("token") and s.get("refresh"):
            return s
        await asyncio.sleep(1.0)
    return None


async def main() -> None:
    from playwright.async_api import async_playwright

    from fomo_api.auth.credential_store import CredentialStore, StoredCredentials
    from fomo_api.config import settings

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(channel="chrome", headless=False)
        except Exception:
            browser = await pw.chromium.launch(headless=False)
        page = await (await browser.new_context()).new_page()
        await page.goto(APP, wait_until="domcontentloaded")
        print("=" * 70, flush=True)
        print(">>> سجّل دخولك مرة واحدة فقط في نافذة المتصفح. هذه آخر مرة تسجّل فيها يدوياً.", flush=True)
        print("=" * 70, flush=True)
        s = await _harvest(page)
        try:
            await browser.close()
        except Exception:
            pass

    if not s:
        print(">>> لم يُلتقط تسجيل دخول ضمن المهلة.", flush=True)
        return

    def _c(v):
        return v.strip().strip('"') if isinstance(v, str) else None

    creds = StoredCredentials(
        access_token=_c(s.get("token")),
        refresh_token=_c(s.get("refresh")),
        pat=_c(s.get("pat")),
        app_id=_c(s.get("app_id")),
        client_id=_c(s.get("client_id")),
        ca_id=_c(s.get("caid")),
    )
    store = CredentialStore(settings.credential_state_file)
    store.save(creds)
    print(f">>> حُفظت بيانات الاعتماد في {store.path}", flush=True)
    print(f">>> قابلة للتجديد تلقائياً: {creds.is_refreshable()}", flush=True)

    # Prove the browserless refresh + live read work right now.
    from fomo_api.auth.token_refresher import _call_privy_refresh

    try:
        r = await _call_privy_refresh(
            refresh_token=creds.refresh_token,
            app_id=creds.app_id,
            pat=creds.pat,
            client_id=creds.client_id,
            ca_id=creds.ca_id,
        )
        store.save(StoredCredentials(access_token=r.get("access"), refresh_token=r.get("refresh"), pat=r.get("pat")))
        access = r.get("access")
        print(">>> [OK] تجديد بلا متصفح نجح - النظام سيجدّد نفسه تلقائياً من الآن.", flush=True)
    except Exception as exc:
        print(f">>> تحذير: التجديد الفوري فشل ({exc}); سيُستخدم الرمز الملتقط.", flush=True)
        access = creds.access_token

    from fomo_api.clients.fomo_client import FomoClient

    client = FomoClient(access)
    try:
        lb = await client.get_leaderboard(page=1, page_size=5, period="all")
        traders = lb["traders"] if isinstance(lb, dict) else lb.traders
        total = lb["total_items"] if isinstance(lb, dict) else lb.total_items
        print(f">>> بيانات حيّة: {total} متداولاً في المتصدّرين", flush=True)
        for t in (traders[:3] if isinstance(traders, list) else traders):
            h = t["handle"] if isinstance(t, dict) else t.handle
            print(f"      @{h}", flush=True)
        print(">>> اكتمل الإعداد. شغّل الخادم وسيعمل الاستخراج تلقائياً بلا تدخّل.", flush=True)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
