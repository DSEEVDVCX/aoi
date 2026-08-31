"""Does the liveness probe tell the truth once the road opens?

`_upstream_alive` hits `GET /feed?limit=1` without `feedTypes`, and `/feed`
requires it, and `_get` translates every ≥400 into "unavailable". So the
probe — in theory — reads the road as dead while it's alive, and the breaker
never reopens after the block is lifted.

This measures it with a working account from the persistent profile: the
probe's call literally, then the same call with `feedTypes` — and the
difference between them is the verdict.

    py probe_feed_liveness.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import sys

for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8", errors="replace")

os.environ["FOMO_API_UPSTREAM_IMPERSONATE"] = "true"
os.environ["FOMO_API_DEV"] = "false"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

APP = "https://fomo.family"
BASE = "https://prod-api.fomo.family"
PROFILE_DIR = pathlib.Path(__file__).with_name(".chk_profile")
FEED_TYPES = ("multi_user_buy", "large_buy", "multi_user_sell", "large_sell")


async def _token() -> str | None:
    """Reads the saved session token — no human sign-in, the profile is persistent."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        launch = dict(user_data_dir=str(PROFILE_DIR), headless=True)
        try:
            ctx = await pw.chromium.launch_persistent_context(channel="chrome", **launch)
        except Exception:
            ctx = await pw.chromium.launch_persistent_context(**launch)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(APP, wait_until="domcontentloaded")
        tok = None
        for _ in range(30):          # Privy writes the token after the SDK boots, not before
            with contextlib.suppress(Exception):
                tok = await page.evaluate("() => localStorage.getItem('privy:token')")
            if tok:
                break
            await asyncio.sleep(1.0)
        with contextlib.suppress(Exception):
            await ctx.close()
    return str(tok).strip().strip('"') if tok else None


async def main() -> None:
    tok = await _token()
    if not tok:
        print(">>> No saved session in .chk_profile — run check_account_block.py first.")
        return

    from curl_cffi.requests import AsyncSession

    from fomo_api.config import settings

    headers = {
        "authorization": f"Bearer {tok}",
        "content-type": "application/json",
        "x-supported-chains": settings.upstream_supported_chains,
        "origin": APP,
        "referer": f"{APP}/",
    }
    # the probe's call literally, then the recorder's real call — and the third is a neutral judge
    cases = (
        ("liveness probe as-is", {"limit": 1}),
        ("probe + feedTypes", {"feedTypes": list(FEED_TYPES), "limit": 1}),
        ("recorder's call", {"feedTypes": list(FEED_TYPES), "limit": 50}),
    )
    async with AsyncSession(impersonate="chrome124", headers=headers, timeout=20) as s:
        for label, params in cases:
            r = await s.get(BASE + settings.upstream_feed_path, params=params)
            body = r.text[:110].replace("\n", " ")
            print(f"  {label:24s} {json.dumps(params, ensure_ascii=False)[:52]:54s} -> {r.status_code}")
            if r.status_code != 200:
                print(f"    {body}")
    print("\nVerdict: if the first answers 400 and the second 200, the probe is false on its own —")
    print("       it reads the road as dead while it's alive, so the breaker never lifts after the block is lifted.")


if __name__ == "__main__":
    asyncio.run(main())
