"""هل مِجَسُّ الحياة يصدُق حين يُفتح الطريق؟

`_upstream_alive` يضرب `GET /feed?limit=1` بلا `feedTypes`، و`/feed` يشترطها
شرطاً لازماً، و`_get` يترجم كلَّ ≥400 إلى «غير متاح». فالمِجَسُّ — نظرياً —
يقرأ الطريقَ ميتاً وهو حيّ، فلا يُعاد فتحُ القاطع أبداً بعد رفع الحجب.

هذا يقيسه بحسابٍ عاملٍ من الملفّ الدائم: نداءُ المِجَسّ حرفيّاً، ثمّ النداءُ
نفسه مع `feedTypes` — والفرقُ بينهما هو الحكم.

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
    """يقرأ توكن الجلسة المحفوظة — لا دخولَ بشريّاً، الملفُّ دائم."""
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
        for _ in range(30):          # Privy يكتب التوكن بعد إقلاع الـSDK لا قبله
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
        print(">>> لا جلسة محفوظة في .chk_profile — شغّل check_account_block.py أوّلاً.")
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
    # نداءُ المِجَسّ حرفيّاً، ثمّ نداءُ المسجّل الحقيقيّ — والثالثُ حَكَمٌ محايد
    cases = (
        ("مِجَسُّ الحياة كما هو", {"limit": 1}),
        ("مِجَسٌّ + feedTypes", {"feedTypes": list(FEED_TYPES), "limit": 1}),
        ("نداءُ المسجّل", {"feedTypes": list(FEED_TYPES), "limit": 50}),
    )
    async with AsyncSession(impersonate="chrome124", headers=headers, timeout=20) as s:
        for label, params in cases:
            r = await s.get(BASE + settings.upstream_feed_path, params=params)
            body = r.text[:110].replace("\n", " ")
            print(f"  {label:24s} {json.dumps(params, ensure_ascii=False)[:52]:54s} -> {r.status_code}")
            if r.status_code != 200:
                print(f"    {body}")
    print("\nالحكم: إن ردَّ الأوّلُ 400 والثاني 200، فالمِجَسُّ كاذبٌ بذاته —")
    print("       يقرأ الطريقَ ميتاً وهو حيّ، فلا يُرفع القاطعُ بعد رفع الحجب.")


if __name__ == "__main__":
    asyncio.run(main())
