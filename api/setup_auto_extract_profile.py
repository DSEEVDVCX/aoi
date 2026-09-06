"""ONE-TIME setup using YOUR REAL Chrome profile — bypasses Google's
"this browser is not secure" block.

Google blocks logins in fresh automation-driven browsers. This variant instead
launches Chrome with a COPY of your real Chrome profile, where you are already
signed in to Google — so you usually don't need to log in to Google at all, and
fomo.family's Privy session may even still be present.

Run once:

    python setup_auto_extract_profile.py

It copies your profile to a temp dir (your real Chrome is never touched or
locked), opens fomo.family, waits until a Privy session exists, saves the
credentials to .privy_state.json, proves a browserless refresh works, then reads
live leaderboard data. From then on the server auto-refreshes forever with no
browser. Prints only handles/counts — never a token.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

os.environ.setdefault("FOMO_API_UPSTREAM_IMPERSONATE", "true")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

APP = "https://fomo.family"
LOGIN_TIMEOUT = 300

# Your Chrome profile (auto-detected on this machine; override via env vars).
_CHROME_USER_DATA = os.environ.get(
    "CHROME_USER_DATA_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"),
)
_CHROME_PROFILE = os.environ.get("CHROME_PROFILE", "Profile 3")


def _copy_profile() -> str:
    """Copy just the one profile + its 'Local State' into a temp user-data dir,
    so Playwright can open it without fighting a running Chrome for the lock."""
    dst_root = tempfile.mkdtemp(prefix="fomo_chrome_")
    src_profile = os.path.join(_CHROME_USER_DATA, _CHROME_PROFILE)
    if not os.path.isdir(src_profile):
        raise SystemExit(
            f"Chrome profile not found: {src_profile}\n"
            f"Set CHROME_PROFILE / CHROME_USER_DATA_DIR to the correct path."
        )
    # Chrome needs the profile folder named 'Default' inside the fresh user-data dir.
    dst_profile = os.path.join(dst_root, "Default")

    def _ignore(_dir, names):
        # Skip big/locked caches we don't need for an existing login.
        skip = {"Cache", "Code Cache", "GPUCache", "Service Worker", "Application Cache",
                "DawnCache", "GraphiteDawnCache", "ShaderCache", "Media Cache"}
        return [n for n in names if n in skip]

    shutil.copytree(src_profile, dst_profile, ignore=_ignore, dirs_exist_ok=True)
    local_state = os.path.join(_CHROME_USER_DATA, "Local State")
    if os.path.isfile(local_state):
        shutil.copy2(local_state, os.path.join(dst_root, "Local State"))
    return dst_root


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

    print(">>> Copying your Chrome profile (your real browser is never touched)...", flush=True)
    user_data = _copy_profile()

    async with async_playwright() as pw:
        # Persistent context = uses the copied profile, with your Google session.
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data,
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--profile-directory=Default"],
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(APP, wait_until="domcontentloaded")
        print("=" * 70, flush=True)
        print(">>> If you are not signed in to fomo.family, press Login — your Google account is already there.", flush=True)
        print(">>> If you were already signed in, it will be captured automatically within seconds.", flush=True)
        print("=" * 70, flush=True)
        s = await _harvest(page)
        try:
            await context.close()
        except Exception:
            pass

    try:
        shutil.rmtree(user_data, ignore_errors=True)
    except Exception:
        pass

    if not s:
        print(">>> No sign-in token captured within the timeout.", flush=True)
        return

    def _c(v):
        return v.strip().strip('"') if isinstance(v, str) else None

    creds = StoredCredentials(
        access_token=_c(s.get("token")), refresh_token=_c(s.get("refresh")),
        pat=_c(s.get("pat")), app_id=_c(s.get("app_id")),
        client_id=_c(s.get("client_id")), ca_id=_c(s.get("caid")),
    )
    store = CredentialStore(settings.credential_state_file)
    store.save(creds)
    print(f">>> Credentials saved to {store.path} | refreshable: {creds.is_refreshable()}", flush=True)

    from fomo_api.auth.token_refresher import _call_privy_refresh

    try:
        r = await _call_privy_refresh(
            refresh_token=creds.refresh_token, app_id=creds.app_id, pat=creds.pat,
            client_id=creds.client_id, ca_id=creds.ca_id,
            current_access=creds.access_token,
        )
        store.save(StoredCredentials(access_token=r.get("access"), refresh_token=r.get("refresh"), pat=r.get("pat")))
        access = r.get("access")
        print(">>> [OK] Browserless refresh succeeded — the system renews itself automatically from now on.", flush=True)
    except Exception as exc:
        print(f">>> Warning: the immediate refresh failed ({exc}); the captured token will be used.", flush=True)
        access = creds.access_token

    from fomo_api.clients.fomo_client import FomoClient

    client = FomoClient(access)
    try:
        lb = await client.get_leaderboard(page=1, page_size=5, period="all")
        traders = lb["traders"] if isinstance(lb, dict) else lb.traders
        total = lb["total_items"] if isinstance(lb, dict) else lb.total_items
        print(f">>> Live data: {total} traders on the leaderboard", flush=True)
        for t in (traders[:3] if isinstance(traders, list) else traders):
            print(f"      @{t['handle'] if isinstance(t, dict) else t.handle}", flush=True)
        print(">>> Setup complete. Start the server and extraction runs automatically with no intervention.", flush=True)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
