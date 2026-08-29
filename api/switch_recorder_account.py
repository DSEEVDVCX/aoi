"""يبدّل حسابَ المسجّل إلى الحساب المسجَّل دخولُه في `.chk_profile`.

هذا التوأمُ الخفيّ لزرّ «بدّل الحساب» في اللوحة: نفسُ المسار بالحرف
(`POST /api/fomo-account/switch`) ونفسُ الحرس ونفسُ النسخة الاحتياطيّة، لكن بلا
إنسانٍ يفتح DevTools وينسخ مخزنَ المتصفّح بيده. اللوحةُ للحالة العاديّة، وهذا
لحالةِ أنّ الجلسةَ العاملة موجودةٌ أصلاً في الملفّ الشخصيّ الدائم الذي تستعمله
`check_account_block.py` و`probe_feed_liveness.py`.

ولا تُطبَع قيمةُ توكنٍ ولا كسرٌ منها: المخزنُ يُقرأ في الذاكرة، ويُرسل إلى
اللوحة، ويُطبع منه البصمةُ وآخرُ أربعة أحرف فقط. فلا يبقى سرٌّ في سجلٍّ ولا في
شريط أوامر.

**ولا يُقبل التوكنُ الأوّلُ الذي يظهر**: Privy يكتب `privy:token` لجلسةٍ مجهولةٍ
عند إقلاع الـSDK قبل أن يستعيد الجلسةَ المحفوظة (قِيس مرّتين 2026-08-20)، فمن
قرأ أوّلَ قيمةٍ يجدها كتب هويّةً لا تملك شيئاً وقرأ «تمّ». فالانتظارُ هنا على
البصمة لا على وجود المفتاح.

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

# هويّةُ جلسة Privy المجهولة — نفسُ الثابت في `dashboard/account.py`.
ANON_DID = "did:privy:cmt0phbhk00080dla83dtghph"

_READ_PRIVY = (
    "() => Object.fromEntries("
    "Object.entries(localStorage).filter(([k]) => k.startsWith('privy:')))"
)


def did_of(token: str | None) -> str:
    """بصمةُ الهويّة من حِمل الـJWT بلا تحقّقٍ من التوقيع — للعرض والمقارنة."""
    if not token:
        return ""
    try:
        body = str(token).strip().strip('"').split(".")[1]
        body += "=" * (-len(body) % 4)
        return str(json.loads(base64.urlsafe_b64decode(body)).get("sub") or "")
    except Exception:
        return ""


async def read_store() -> dict[str, str]:
    """يفتح الملفَّ الشخصيّ الدائم ويقرأ مفاتيحَ Privy بعد استعادة الجلسة."""
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
            if did and did != ANON_DID:      # الجلسةُ المحفوظة، لا المجهولة
                break
            await asyncio.sleep(1.0)
        with contextlib.suppress(Exception):
            await ctx.close()
    return store


def post_switch(store: dict[str, str]) -> dict:
    """يرسل المخزنَ إلى اللوحة — نفسُ المسار الذي يستعمله الزرّ، بحراسه كلِّها."""
    page = urllib.request.urlopen(DASHBOARD + "/", timeout=10).read().decode("utf-8")
    match = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', page)
    if not match:
        raise SystemExit(">>> لم أجد رمزَ حماية اللوحة — هل اللوحةُ تعمل على 8090؟")
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
        raise SystemExit(f">>> رفضت اللوحة ({error.code}): {body.get('error')}") from None


async def main() -> None:
    store = await read_store()
    did = did_of(store.get("privy:token"))
    if not did:
        raise SystemExit(
            ">>> لا جلسة في .chk_profile — شغّل check_account_block.py وسجّل دخولاً أوّلاً."
        )
    if did == ANON_DID:
        raise SystemExit(
            ">>> الملفُّ الشخصيّ يحمل جلسةً مجهولةً لا حساباً: انتهى الانتظارُ قبل أن "
            "يستعيد Privy الجلسةَ المحفوظة، أو أنّ الجلسةَ انتهت وتحتاج دخولاً جديداً."
        )
    print(f"وُجد في الملفّ الشخصيّ: {did}")
    print(f"مفاتيحُ Privy المقروءة: {len(store)}")

    result = post_switch(store)
    print(f"\n>>> {result['message']}")
    print(f"الهويّةُ الآن : {result['did']} (…{result['token_tail']})")
    print(f"نسخةُ الرجوع : {result['backup']}")


if __name__ == "__main__":
    asyncio.run(main())
