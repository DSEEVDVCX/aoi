"""تجربةُ فصل: هل الحجبُ على الحساب أم على شيءٍ آخر؟

يفتح نافذة Chrome حقيقيّة على fomo.family بملفٍّ خاصٍّ بالتجربة (لا كوكيز
الحساب الحاليّ، والجلسةُ تبقى بين التشغيلات)، فتسجّل دخولك **بحسابٍ آخر**، ثمّ يضرب المسارات الأربعة نفسها التي
ضُربت بالحساب المحجوب ويطبع رمز الحالة صريحاً لكلٍّ منها.

المنطقُ الذي يجعل التجربة حاسمة: الـIP والجهاز والموقع والبصمة كلُّها ثابتة —
المتغيّرُ الوحيد هو الهويّة. فإن ردّ الحساب الجديد 200 فالحجبُ على الحساب
الأوّل وحده؛ وإن ردّ 403 أيضاً فالحجبُ على شيءٍ لا يتغيّر بتغيير الحساب.

ولا يكتب هذا السكربت في `.privy_state.json` إطلاقاً: الحساب العامل يبقى كما هو،
والتجربةُ قراءةٌ محضة. ولا يطبع التوكن — بصمةَ DID وحدها ليُعلَم أنّ الحساب تغيّر.

    py check_account_block.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import pathlib
import sys

# طرفيّةُ ويندوز العربيّة cp1256 لا تعرف U+2192، فرفعت UnicodeEncodeError
# **بعد** التقاط التوكن فأسقطت التجربة عند أوّل سطر نتيجة. الترميزُ صريحٌ هنا
# و`errors="replace"` يمنع أيَّ محرفٍ آخر من أن يُسقِط قياساً بعد إتمامه.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")

os.environ["FOMO_API_UPSTREAM_IMPERSONATE"] = "true"
os.environ["FOMO_API_DEV"] = "false"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

APP = "https://fomo.family"
BASE = "https://prod-api.fomo.family"
LOGIN_TIMEOUT = 600

# هويّةُ جلسة Privy المجهولة — قِيست هي نفسُها في تشغيلتين متتاليتين
# (2026-08-20T00:11Z و00:14Z)، فهي ثابتُ التطبيق لا مستخدم. وتوثيقُ المشروع
# يقول إنّ `privy:refresh_token` لا يُكتب إلّا بعد دخولٍ ناجح (مؤكَّد
# 2026-07-25) — وقد بطل ذلك: المجهولةُ تكتبه أيضاً. فالتمييزُ بالهويّة لا
# بحضور المفتاح.
ANON_DID = "did:privy:cmt0phbhk00080dla83dtghph"

# ملفُّ متصفّحٍ دائم: الدخولُ اليدويّ أغلى ما في التجربة، فلا يُهدر إن
# احتاجت إعادةَ تشغيل — الجلسةُ تبقى في هذا المجلّد.
PROFILE_DIR = pathlib.Path(__file__).with_name(".chk_profile")

# المسارات الأربعة نفسها التي قِيست على الحساب المحجوب — المقارنةُ لا تصحّ
# إلّا على المسار عينِه بالطريقة عينِها.
PROBES: tuple[tuple[str, str, dict | None], ...] = (
    ("GET", "/v2/leaderboard?limit=3", None),
    ("GET", "/proxy/verifiedTokens", None),
    ("POST", "/proxy/trendingTokens", {}),
    ("GET", "/feed", None),
)


def _did_of(token: str) -> str:
    """بصمةُ الهويّة من حِمل الـJWT — لا التوكن نفسه (FR-013)."""
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return str(json.loads(base64.urlsafe_b64decode(body)).get("sub") or "?")
    except Exception:  # بصمةٌ للعرض لا للتحقّق
        return "?"


def _blocked_did() -> str:
    """هويّةُ الحساب المحجوب من ملف الحالة — لنتأكّد أنّ الحساب تغيّر فعلاً."""
    p = pathlib.Path(__file__).with_name(".privy_state.json")
    try:
        return _did_of(json.loads(p.read_text(encoding="utf-8"))["access_token"])
    except Exception:  # غيابُ الملف لا يُفشل التجربة
        return "?"


async def _harvest_token(context) -> str | None:
    """ينتظر هويّةً ليست المجهولة، ويقرأ **كلَّ** صفحات السياق لا واحدة.

    قِيس 2026-08-20T00:16Z مرّتين متتاليتين:

    أوّلاً — حضورُ `privy:token` مع `privy:refresh_token` أطلق الالتقاط بعد
    ثوانٍ بهويّة `ANON_DID`، فأُغلق المتصفّحُ قبل أن يسجّل أحدٌ دخوله. فصار
    الشرطُ هويّةً معلومةً بعينها لا حضورَ مفتاح.

    وثانياً — بعد دخولٍ بشريّ فعليّ صمت المِجَسُّ تماماً: `page.evaluate` بدأ
    يرمي كلَّ ثانية لأنّ تدفّقَ Privy ينقل الصفحة أو يفتح نافذةً أخرى، وقُيِّد
    القياسُ بصفحةٍ واحدة أُخِذت عند الإقلاع. وكان `continue` يسبق سطرَ التكّة
    فانقطع الخبرُ عند الفشل بعينه — أي أنّ التشخيصَ عَمِيَ حين احتيج إليه.
    فالآن: كلُّ صفحاتِ السياق تُقرأ، وسببُ آخرِ فشلٍ يُطبع، والتكّةُ لا تُقفَز.
    """
    read_state = (
        "() => ({"
        "  token: localStorage.getItem('privy:token'),"
        "  refresh: localStorage.getItem('privy:refresh_token')"
        "})"
    )
    last_err = ""
    for tick in range(LOGIN_TIMEOUT):
        seen: list[str] = []
        for page in list(context.pages):
            url = ""
            try:
                url = page.url or ""
                if "fomo.family" not in url:
                    continue                      # أصلٌ آخر ⇒ مخزنٌ آخر
                state = await page.evaluate(read_state)
            except Exception as exc:  # الصفحةُ تُنقل أو تُغلق أثناء الدخول
                last_err = f"{type(exc).__name__} @ {url[:40]}"
                continue
            token = (state or {}).get("token")
            if not token or not (state or {}).get("refresh"):
                continue
            token = str(token).strip().strip('"')
            did = _did_of(token)
            seen.append(did)
            if did != ANON_DID:
                print(f">>> دخولٌ بشريّ بعد {tick}ث — الهويّة {did}", flush=True)
                return token
        if tick and tick % 20 == 0:
            state_txt = "مجهولة فقط" if seen else f"لا مخزن مقروء ({last_err or '—'})"
            print(f">>> بانتظار الدخول… ({tick}/{LOGIN_TIMEOUT}ث) "
                  f"صفحات={len(context.pages)} {state_txt}", flush=True)
        await asyncio.sleep(1.0)
    return None


async def _probe(token: str) -> list[int | str]:
    """يضرب المسارات برؤوس FomoClient نفسها ويعيد رموز الحالة الخام.

    نتجاوز FomoClient هنا عن قصد: هو يترجم الرموز استثناءاتٍ برسالةٍ واحدة
    للحجب وللانقطاع، والمطلوبُ في التجربة الرمزُ نفسه لا ترجمتُه.
    """
    from curl_cffi.requests import AsyncSession

    from fomo_api.config import settings

    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "x-supported-chains": settings.upstream_supported_chains,
        "origin": APP,
        "referer": f"{APP}/",
    }
    out: list[int | str] = []
    async with AsyncSession(impersonate="chrome124", headers=headers, timeout=20) as s:
        for method, path, body in PROBES:
            try:
                r = await (
                    s.get(BASE + path) if method == "GET"
                    else s.post(BASE + path, json=body)
                )
                out.append(r.status_code)
                msg = ""
                if r.status_code != 200:
                    msg = "  " + r.text[:90].replace("\n", " ")
                print(f"    {method:4s} {path:28s} -> {r.status_code}{msg}", flush=True)
            except Exception as exc:  # فشلُ النقل جوابٌ أيضاً
                out.append(type(exc).__name__)
                print(f"    {method:4s} {path:28s} -> {type(exc).__name__}: {exc}", flush=True)
    return out


def _verdict(codes: list[int | str], same_account: bool) -> None:
    ok = sum(1 for c in codes if c == 200)
    forbidden = sum(1 for c in codes if c == 403)
    print("\n" + "=" * 70, flush=True)
    if same_account:
        print("!! الحسابُ لم يتغيّر — هذه هويّةُ الحساب المحجوب نفسها.", flush=True)
        print("   أعِد التشغيل وسجّل دخولاً بحسابٍ مختلف لتصحّ المقارنة.", flush=True)
    elif ok == len(codes):
        print(f"الحكم: الحسابُ الجديد يعمل ({ok}/{len(codes)} ردّت 200).", flush=True)
        print("⇒ الحجبُ على الحساب الأوّل وحده — هويّةٌ موقوفة، لا IP ولا شبكة.", flush=True)
        print("  والعلاج: بدّل اعتماد المسجّل إلى هذا الحساب، وأنزِل معدّل الطلب", flush=True)
        print("  قبل تشغيل الجمع وإلّا لحِق الحجبُ الحساب الجديد كما لحِق الأوّل.", flush=True)
    elif forbidden == len(codes):
        print(f"الحكم: الحسابُ الجديد محجوبٌ أيضاً ({forbidden}/{len(codes)} ردّت 403).", flush=True)
        print("⇒ ليس الحساب. المتغيّرُ الوحيد كان الهويّة وقد بقي الجواب 403،", flush=True)
        print("  فالحجبُ على ما لم يتغيّر: الـIP أو الموقع أو المنصّة كلّها.", flush=True)
        print("  والفحصُ التالي: جرّب شبكةً أخرى (هاتفاً مثلاً) بالحساب نفسه.", flush=True)
    else:
        print(f"الحكم: مختلط — 200:{ok} 403:{forbidden} من {len(codes)}.", flush=True)
        print("⇒ حجبٌ على مستوى المسار لا الحساب؛ اقرأ الجدول أعلاه مساراً مساراً.", flush=True)
    print("=" * 70, flush=True)


async def main() -> None:
    from playwright.async_api import async_playwright

    blocked = _blocked_did()
    print("=" * 70, flush=True)
    print(f"الحساب المحجوب: {blocked}", flush=True)
    print(">>> ستُفتح نافذة Chrome بملفٍّ خاصّ بالتجربة. سجّل دخولك بحسابٍ **آخر**.",
          flush=True)
    print(">>> (الجلسةُ تبقى محفوظة، فلا يُعاد الدخولُ إن احتاجت التجربةُ تكراراً)",
          flush=True)
    print(f">>> (مهلة {LOGIN_TIMEOUT // 60} دقائق؛ الفحصُ يبدأ تلقائياً بعد الدخول)", flush=True)
    print("=" * 70, flush=True)

    async with async_playwright() as pw:
        PROFILE_DIR.mkdir(exist_ok=True)
        launch = dict(
            user_data_dir=str(PROFILE_DIR), headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = await pw.chromium.launch_persistent_context(channel="chrome", **launch)
        except Exception:  # Chrome المثبّت أوّلاً ثمّ chromium
            context = await pw.chromium.launch_persistent_context(**launch)
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(APP, wait_until="domcontentloaded")
        token = await _harvest_token(context)
        with contextlib.suppress(Exception):   # الإغلاقُ لا يُفشل التجربة
            await context.close()

    if not token:
        print(">>> لم يُلتقط رمزُ دخولٍ ضمن المهلة — لم تُجرَ التجربة.", flush=True)
        return

    new_did = _did_of(token)
    print(f"\n>>> التُقط رمزُ دخول. الهويّة: {new_did}", flush=True)
    print(">>> ضربُ المسارات نفسها بالحساب الجديد:\n", flush=True)
    codes = await _probe(token)
    _verdict(codes, same_account=(new_did == blocked and new_did != "?"))


if __name__ == "__main__":
    asyncio.run(main())
