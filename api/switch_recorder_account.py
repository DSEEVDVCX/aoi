"""Switches the recorder's account to the one signed in inside `.chk_profile`.

This is the invisible twin of the "Switch account" button in the dashboard:
the same path to the letter (`POST /api/fomo-account/switch`), the same guards
and the same backup, but without a human opening DevTools and copying the
browser store by hand. The dashboard is for the normal case; this is for the
case where a working session already lives in the persistent profile that
`check_account_block.py` and `probe_feed_liveness.py` use.

No token value, nor any fragment of one, is ever printed: the store is read
in memory, sent to the dashboard, and only its fingerprint and last four
characters are printed. So no secret stays in a log or on a command line.

**The first token that appears is not accepted**: Privy writes `privy:token`
for an anonymous session when the SDK boots, before it restores the saved
session (measured twice on 2026-08-20) — whoever reads the first value they
find writes down an identity that owns nothing and reads "done". So the wait
here is on the fingerprint, not on the key's mere existence.

    py switch_recorder_account.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")

APP = "https://fomo.family"
PROFILE_DIR = pathlib.Path(__file__).with_name(".chk_profile")
DASHBOARD = os.environ.get("AOI_DASHBOARD", "http://127.0.0.1:8090")

# The anonymous Privy session identity — the same constant as in `dashboard/account.py`.
ANON_DID = "did:privy:cmt0phbhk00080dla83dtghph"

_READ_PRIVY = (
    "() => Object.fromEntries("
    "Object.entries(localStorage).filter(([k]) => k.startsWith('privy:')))"
)


def did_of(token: str | None) -> str:
    """The identity's fingerprint from the JWT payload, no signature check — for display and comparison."""
    if not token:
        return ""
    try:
        body = str(token).strip().strip('"').split(".")[1]
        body += "=" * (-len(body) % 4)
        return str(json.loads(base64.urlsafe_b64decode(body)).get("sub") or "")
    except Exception:
        return ""


async def read_store() -> dict[str, str]:
    """Opens the persistent profile and reads the Privy keys after the session is restored."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        launch = {"user_data_dir": str(PROFILE_DIR), "headless": True}
        try:
            ctx = await pw.chromium.launch_persistent_context(channel="chrome", **launch)
        except Exception:
            ctx = await pw.chromium.launch_persistent_context(**launch)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(APP, wait_until="domcontentloaded")
        store: dict[str, str] = {}
        for _ in range(45):
            with contextlib.suppress(Exception):
                store = await page.evaluate(_READ_PRIVY) or {}
            did = did_of(store.get("privy:token"))
            if did and did != ANON_DID:      # the saved session, not the anonymous one
                break
            await asyncio.sleep(1.0)
        with contextlib.suppress(Exception):
            await ctx.close()
    return store


def post_switch(store: dict[str, str]) -> dict:
    """Sends the store to the dashboard — the same path the button uses, with all its guards."""
    page = urllib.request.urlopen(DASHBOARD + "/", timeout=10).read().decode("utf-8")
    match = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', page)
    if not match:
        raise SystemExit(">>> Dashboard token not found — is the dashboard running on 8090?")
    request = urllib.request.Request(
        DASHBOARD + "/api/fomo-account/switch",
        data=json.dumps(store).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Origin": DASHBOARD,
            "X-Dashboard-Token": match.group(1),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = json.loads(error.read() or b"{}")
        raise SystemExit(f">>> The dashboard refused ({error.code}): {body.get('error')}") from None


async def main() -> None:
    store = await read_store()
    did = did_of(store.get("privy:token"))
    if not did:
        raise SystemExit(
            ">>> No session in .chk_profile — run check_account_block.py and sign in first."
        )
    if did == ANON_DID:
        raise SystemExit(
            ">>> The profile holds an anonymous session, not an account: the wait ended "
            "before Privy restored the saved session, or the session expired and needs a fresh sign-in."
        )
    print(f"Found in the profile: {did}")
    print(f"Privy keys read: {len(store)}")

    result = post_switch(store)
    print(f"\n>>> {result['message']}")
    print(f"Identity now : {result['did']} (…{result['token_tail']})")
    print(f"Rollback copy: {result['backup']}")


if __name__ == "__main__":
    asyncio.run(main())
