"""تبديلُ حساب fomo من اللوحة، ومعرفةُ هل هو محجوب.

هذا مسارُ الكتابة **الثاني** الذي تملكه اللوحة بعد `keystore.py`، وحدودُه هي
حدودُه نفسها: القاعدةُ تبقى `mode=ro` بلا سطرِ كتابةٍ واحد، والهدفُ ملفٌّ واحد
هو `api/.privy_state.json`.

ولمّا كان المكتوبُ هنا أخطرَ ممّا في مخزن المفاتيح — إنّه هويّةُ الحساب كلُّها،
لا مفتاحَ مزوّدٍ يُستبدل بضغطة — زِيدت ثلاثةُ قيودٍ لا توجد هناك:

1. **نسخةٌ قبل كلِّ كتابة.** الملفُّ القديم يُنسخ إلى `.privy_state.json.bak-*`
   قبل أن يُلمس، والاستعادةُ زرٌّ واحد. إبطالُ حسابٍ عاملٍ بلصقةٍ خاطئة كان
   سيعني توقّفَ الجمع كلَّه بلا طريقٍ للرجوع.
2. **الهويّةُ تُقرأ من التوكن لا من المستخدم.** لا حقلَ اسمٍ يُكتب بيدٍ: البصمةُ
   `did:privy:…` تُستخرج من حِمل الـJWT، فلا يُخطئ أحدٌ في تسمية ما بدّل.
3. **رفضُ الجلسة المجهولة.** Privy يكتب `privy:token` لجلسةٍ مجهولةٍ عند إقلاع
   الـSDK قبل أيّ دخول (قِيس 2026-08-20؛ ووثيقةُ `privy_login.py` تقول غيرَ
   ذلك وقد بطل قولُها). فلصقُ مخزنٍ قبل الدخول كان يكتب هويّةً لا تملك شيئاً،
   ويقرأ المستخدمُ «تمّ» ثمّ يجد الجمعَ ميتاً.

ولا تُعاد القيمةُ أبداً: ما يخرج من هنا بصمةُ الهويّة، وآخرُ أربعة أحرف من
التوكن للتمييز، ووقتُ الانتهاء، ورموزُ حالة المِجَسّ. ولا تُسجَّل قيمةٌ في سجلٍّ
ولا في رسالة خطأ.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import UTC, datetime
from typing import Any

import config

# الحقولُ الستّة التي يقرأها `CredentialStore`. `access_token` و`refresh_token`
# و`pat` أسرار، والثلاثةُ الباقية معرّفاتُ تطبيقٍ لا سرّ فيها.
_SECRET_FIELDS = ("access_token", "refresh_token", "pat")
_PLAIN_FIELDS = ("app_id", "client_id", "ca_id")
_FIELDS = _SECRET_FIELDS + _PLAIN_FIELDS

# ونقصُ حقلٍ ليس درجةً واحدة: هذه الأربعةُ وحدَها لا بديلَ لها، والحقلانِ
# الباقيان لهما بديل — `client_id` معرّفٌ ثابتٌ للتطبيق يضعه `api/config.py`
# افتراضيّاً حين لا يُلتقط، و`ca_id` لا يُرسَل أصلاً إن غاب (`if ca_id`).
# وكان الحكمُ يقول «ناقص» بالأحمر على أيٍّ منها، فأشعل اللوحةَ يوم 2026-08-20
# على نظامٍ يجدّد توكنَه كلَّ دقيقة بلا `client_id` في الملفّ: إنذارٌ كاذبٌ
# يُعلّم المستخدمَ أن يتجاهل الأحمر، وذلك أسوأُ من ألّا يكون هناك حكم.
_REQUIRED_FIELDS = ("access_token", "refresh_token", "pat", "app_id")

# مفاتيحُ localStorage التي يكتبها Privy — نفسُ أسماء `credential_store.py`.
_LS_TOKEN = "privy:token"
_LS_REFRESH = "privy:refresh_token"
_LS_PAT = "privy:pat"
# **الاسمُ الحقيقيّ `privy:caid` لا `privy:ca_id`** — قِيس على مخزنٍ حيّ
# 2026-08-20: مفاتيحُ Privy السبعة فيه `privy:caid`، فالثابتُ الأوّل لم يطابق
# شيئاً قطّ، و`ca_id` كان يُورَث بصمتٍ من الملفّ القديم في كلّ تبديل: أي أنّ
# التبديلَ كان يكتب معرّفَ حسابٍ سابقٍ مع توكنِ حسابٍ جديد. والاسمان معاً
# لأنّ `credential_store` يعرف الأوّل، ولا يُعرَف أيَّ نسخةٍ من الـSDK يقرأ
# المستخدم — فأيُّهما وُجد أُخِذ.
_LS_CAID = ("privy:caid", "privy:ca_id")
_APP_ID_RE = re.compile(r"^privy:([a-z0-9]{20,30}):")
_CLIENT_ID_RE = re.compile(r"client-[A-Za-z0-9]{10,}")

# هويّةُ جلسة Privy المجهولة — تُكتب عند إقلاع الـSDK قبل أيّ دخول، فقياسُها
# مرّتين متتاليتين (2026-08-20T00:11Z و00:14Z) أعطى الثابتَ نفسه.
_ANON_DID = "did:privy:cmt0phbhk00080dla83dtghph"

_BAK_PREFIX = ".privy_state.json.bak-"
_BAK_KEEP = 5          # نسخٌ محفوظة؛ ما زاد يُحذف أقدمَه أوّلاً

# قفلٌ لكلّ تعديل: التبديلُ قراءةٌ فنسخٌ فكتابة، وطلبان متزامنان كانا سيتشابكا.
_LOCK = threading.Lock()

# نتيجةُ آخر مِجَسّ — في الذاكرة لا في القاعدة ولا في الملفّ، مثلُ `_PROBES`
# في مخزن المفاتيح: نتيجةُ فحصٍ لحظيّة، وكتابتُها في القاعدة تُدخلها في النسخ
# الاحتياطيّ بلا داعٍ. تُفقد بإعادة تشغيل اللوحة، ويُعاد الفحصُ بضغطة.
_LAST_PROBE: dict[str, Any] = {}


class AccountError(RuntimeError):
    """طلبٌ مرفوض بسببٍ يُعرض للمستخدم كما هو."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _path() -> str:
    return config.PRIVY_STATE_PATH


def _tail(value: str | None) -> str:
    """آخرُ أربعة أحرف — للتمييز بين توكنين، وهو كلُّ ما يُعرض من القيمة.

    نفسُ استثناء FR-013 المسموح في `key_file.tail`: يُحسب لحظةَ الطلب ولا
    يُكتب في سجلٍّ ولا في meta.
    """
    text = str(value or "")
    return text[-4:] if len(text) >= 8 else ""


def _claims(token: str | None) -> dict[str, Any]:
    """حِملُ الـJWT بلا تحقّقٍ من التوقيع — للعرض لا للتصريح.

    ولا يجوز أن يُتحقّق: هذا توكنُ خدمةٍ أخرى، ولا نملك مفتاحَها، والغرضُ
    إظهارُ الهويّة ووقت الانتهاء للمستخدم لا السماحُ بشيء.
    """
    try:
        body = str(token).split(".")[1]
        body += "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body))
    except Exception:  # noqa: BLE001 — توكنٌ غيرُ مقروء ⇒ لا مطالبات، لا انهيار
        return {}
    return data if isinstance(data, dict) else {}


def _did(token: str | None) -> str:
    return str(_claims(token).get("sub") or "")


def _expiry(token: str | None) -> dict[str, Any]:
    """وقتُ انتهاء التوكن وهل انتهى — التوكن عمرُه ساعة، والفرقُ يفيد التشخيص."""
    exp = _claims(token).get("exp")
    if not isinstance(exp, (int, float)):
        return {"expires_at": None, "expired": None, "seconds_left": None}
    when = datetime.fromtimestamp(float(exp), UTC)
    left = (when - datetime.now(UTC)).total_seconds()
    return {
        "expires_at": when.isoformat(),
        "expired": left <= 0,
        "seconds_left": int(left),
    }


def _written_seconds_ago() -> int | None:
    """عمرُ آخرِ كتابةٍ للملفّ بالثواني، أو None إن لم يوجد.

    وهذا هو الفارقُ بين تشخيصين تُخلَط بينهما اللوحةُ وهما نقيضان: توكنٌ منتهٍ
    وملفٌّ لم يُمسّ منذ ساعات ⇒ **لا أحدَ يجدّد** (خادمُ الـapi ميت). توكنٌ
    منتهٍ وملفٌّ يُكتب كلَّ دقيقة ⇒ **التجديدُ يعمل ويُرفَض** (جلسةُ Privy
    انتهت ولا تُمدَّد، ولا يُنجيها إلّا دخولٌ جديد). وقول «هل خادمُ الـapi
    يعمل؟» في الحالة الثانية يرسل المستخدمَ إلى الجهة الخاطئة تماماً — وهو
    ما حدث 2026-08-20: كان الخادمُ يعمل ويجدّد كلَّ 60ث ويُرفَض بصمت.
    """
    with contextlib.suppress(OSError):
        return max(0, int(time.time() - os.path.getmtime(_path())))
    return None


def _read() -> dict[str, Any]:
    path = _path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise AccountError(f"تعذّرت قراءة ملفّ الاعتماد: {type(exc).__name__}", 500) from exc
    return data if isinstance(data, dict) else {}


def _backups() -> list[dict[str, Any]]:
    """النسخُ المحفوظة، الأحدثُ أوّلاً — بالبصمة لا بالقيمة."""
    folder = os.path.dirname(_path())
    out: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for name in os.listdir(folder):
            if not name.startswith(_BAK_PREFIX):
                continue
            full = os.path.join(folder, name)
            try:
                with open(full, encoding="utf-8") as fh:
                    raw = json.load(fh)
                did = _did(raw.get("access_token"))
            except (OSError, ValueError):
                did = ""
            out.append({
                "name": name,
                "did": did,
                "saved_at": datetime.fromtimestamp(
                    os.path.getmtime(full), UTC
                ).isoformat(),
            })
    out.sort(key=lambda row: row["saved_at"], reverse=True)
    return out


def _backup_now(reason: str) -> str | None:
    """ينسخ الملفَّ الحاليّ قبل المساس به، ويُبقي آخرَ `_BAK_KEEP` نسخاً."""
    path = _path()
    if not os.path.exists(path):
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^a-z0-9-]+", "", reason.lower())[:12] or "switch"
    name = f"{_BAK_PREFIX}{stamp}-{safe}"
    target = os.path.join(os.path.dirname(path), name)
    try:
        shutil.copy2(path, target)
    except OSError as exc:
        raise AccountError(f"تعذّر حفظ نسخة احتياطيّة: {type(exc).__name__}", 500) from exc
    for old in _backups()[_BAK_KEEP:]:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(os.path.dirname(path), old["name"]))
    return name


def _write(fields: dict[str, Any]) -> None:
    """كتابةٌ ذرّيّة بنفس أسلوب `key_file.save_entries`.

    ملفٌّ مؤقّتٌ في نفس المجلّد ثمّ `os.replace`: القارئُ إمّا يرى الملفَّ القديم
    كاملاً أو الجديدَ كاملاً، ولا يرى نصفاً. والمسجّلُ يقرأ هذا الملفَّ كلَّ
    دورة، فنصفُ ملفٍّ كان سيوقفه.
    """
    path = _path()
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".privy-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(fields, fh, indent=2)
        with contextlib.suppress(OSError):   # لا معنى لها على ويندوز
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise AccountError(f"تعذّرت الكتابة: {type(exc).__name__}", 500) from exc


def _from_local_storage(dump: dict[str, Any]) -> dict[str, Any]:
    """يستخرج الحقولَ الستّة من مخزن المتصفّح.

    يقبل شكلين: المخزنَ الخامَ `{"privy:token": …}` كما ينسخه المستخدم من
    الطرفيّة، والغلافَ `{"_full_localStorage": {…}}` وهو الشكلُ الذي يفهمه
    `credential_store.from_local_storage_dump` أصلاً — قُبِل الشكلان لأنّ
    المستخدم لا يعرف أيَّهما بيده.
    """
    inner = dump.get("_full_localStorage")
    ls = inner if isinstance(inner, dict) else dump
    app_id = client_id = None
    for key, val in ls.items():
        match = _APP_ID_RE.match(str(key))
        if match:
            app_id = match.group(1)
        if client_id is None and isinstance(val, str):
            found = _CLIENT_ID_RE.search(val)
            if found:
                client_id = found.group(0)
    return {
        "access_token": _clean(ls.get(_LS_TOKEN)),
        "refresh_token": _clean(ls.get(_LS_REFRESH)),
        "pat": _clean(ls.get(_LS_PAT)),
        "app_id": app_id,
        "client_id": client_id,
        "ca_id": next((c for c in (_clean(ls.get(k)) for k in _LS_CAID) if c), None),
    }


def _clean(value: Any) -> str | None:
    """يقشّر علاماتَ التنصيص التي يضعها Privy حول قيم localStorage."""
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return text or None


def status() -> dict[str, Any]:
    """حالةُ الحساب الحاليّ — بلا أيّ قيمةٍ سرّيّة."""
    raw = _read()
    access = raw.get("access_token")
    present = {name: bool(raw.get(name)) for name in _FIELDS}
    return {
        "exists": bool(raw),
        "path": _path(),
        "did": _did(access),
        "token_tail": _tail(access),
        "refreshable": all(bool(raw.get(k)) for k in ("refresh_token", "app_id", "pat")),
        "present": present,
        "missing": [name for name in _FIELDS if not raw.get(name)],
        "missing_required": [name for name in _REQUIRED_FIELDS if not raw.get(name)],
        **_expiry(access),
        "backups": _backups(),
        "last_probe": dict(_LAST_PROBE) or None,
        "anon_did": _ANON_DID,
        "written_seconds_ago": _written_seconds_ago() if raw else None,
    }


def _validate(fields: dict[str, Any], current_access: str | None) -> tuple[str, bool]:
    """يرفض قبل الكتابة كلَّ ما يُنتج ملفّاً لا يعمل، ويعيد (البصمة، أهو تجديد).

    و«تجديد» = نفسُ الهويّة بتوكنٍ **أحدث**. كان هذا يُرفض 409 «لا شيء
    ليُبدَّل»، وهو خطأ: لمّا تعطّل تجديدُ Privy في 2026-08-20 (ردّ 200 و
    `session_update_action=ignore` بلا توكن، والقديمُ منتهٍ) كان الطريقُ
    الوحيدُ للنجاة أن يسجّل المستخدمُ دخولاً جديداً **بنفس الحساب** ويلصق
    مخزنَه — واللوحةُ ترفض. فمن كان محقّاً وجد باباً مغلقاً.

    والمقارنةُ بـ`iat` ثمّ `exp` لا بنصّ التوكن: توكنان مختلفان نصّاً قد
    يكونان لنفس اللحظة، والأقدمُ لا يجوز أن يطمس الأحدث (لصقةٌ من نافذةٍ
    قديمة نُسيت مفتوحة).
    """
    missing = [name for name in ("access_token", "refresh_token", "pat") if not fields.get(name)]
    if missing:
        raise AccountError(
            "المخزن ناقص: لم أجد " + "، ".join(missing)
            + ". تأكّد أنّك نسخته من fomo.family بعد تسجيل الدخول."
        )
    if not fields.get("app_id"):
        raise AccountError(
            "لم أجد `privy:<app_id>:…` في المخزن — انسخ المخزنَ كلَّه لا سطراً منه."
        )
    did = _did(fields["access_token"])
    if not did:
        raise AccountError("التوكن غيرُ مقروء — ليس JWT صالحاً.")
    if did == _ANON_DID:
        raise AccountError(
            "هذه جلسةٌ مجهولة لا حساب: Privy يكتبها قبل الدخول. "
            "سجّل دخولك في الصفحة أوّلاً ثمّ انسخ المخزن."
        )
    if did != _did(current_access):
        return did, False
    if not _is_newer(fields["access_token"], current_access):
        raise AccountError(
            "هذه هويّةُ الحساب الحاليّ نفسها وتوكنُها ليس أحدث — لا شيء ليُبدَّل. "
            "إن أردت تجديد جلسةٍ متعطّلة فسجّل دخولاً جديداً ثمّ انسخ المخزن.",
            409,
        )
    return did, True


def _is_newer(candidate: str | None, current: str | None) -> bool:
    """أهو توكنٌ أحدثُ لنفس الحساب؟ بـ`iat` وإلّا بـ`exp`، ولا شيءَ غيرهما."""
    if not current:
        return True
    new_claims, old_claims = _claims(candidate), _claims(current)
    for claim in ("iat", "exp"):
        fresh, stale = new_claims.get(claim), old_claims.get(claim)
        if (
            isinstance(fresh, (int, float))
            and isinstance(stale, (int, float))
            and fresh != stale
        ):
            return fresh > stale
    return False


def switch(dump: dict[str, Any]) -> dict[str, Any]:
    """يبدّل الحساب من مخزن متصفّحٍ ملصوق، بعد نسخةٍ احتياطيّة.

    الدمجُ مقصود: الحقولُ الغائبةُ من اللصقة تبقى من الملفّ القديم — `client_id`
    مثلاً قد لا يظهر في كلّ مخزن. لكنّ الأسرارَ الثلاثة تُفرض حاضرةً في
    `_validate` قبل ذلك، فلا يخرج ملفٌّ يخلط توكنَ حسابٍ بتحديثِ آخر.
    """
    if not isinstance(dump, dict) or not dump:
        raise AccountError("لم أستلم مخزناً — الصق محتوى localStorage كاملاً.")
    fields = _from_local_storage(dump)
    with _LOCK:
        current = _read()
        did, is_refresh = _validate(fields, current.get("access_token"))
        backup = _backup_now("refresh" if is_refresh else "switch")
        merged = {name: current.get(name) for name in _FIELDS}
        merged.update({k: v for k, v in fields.items() if v})
        _write(merged)
    _LAST_PROBE.clear()          # نتيجةُ الحساب السابق لا تصف الجديد
    return {
        "ok": True,
        "did": did,
        "token_tail": _tail(fields["access_token"]),
        "backup": backup,
        "refreshed": is_refresh,
        "message": (
            (
                "جُدّدت جلسةُ الحساب نفسِه بتوكنٍ أحدث. "
                if is_refresh
                else "تمّ التبديل. "
            )
            + "المسجّل يقرأ الملفَّ كلَّ دورة فيلتقطه خلال دقيقة بلا إعادة "
            "تشغيل، وخادمُ الـapi يتبنّى الهويّةَ عند تجديده."
        ),
    }


_PROBE_PATHS: tuple[tuple[str, str, dict | None], ...] = (
    ("GET", "/v2/leaderboard?limit=3", None),
    ("GET", "/proxy/verifiedTokens", None),
    ("POST", "/proxy/trendingTokens", {}),
)


def _verdict(codes: list[int | None]) -> tuple[str, str]:
    """يفصل «الهويّةُ محجوبة» عن «المصدرُ متعثّر» — والخلطُ بينهما مكلف.

    قِيس 2026-08-19: حجبُ الهويّة يردّ 403 على كلّ مسار، والمسجّل كتبه
    «unreachable» فطُوردت الشبكةُ ساعةً وهي سليمة. فالرمزُ هو الحكم:

    - 403 على الكلّ ⇒ الهويّةُ موقوفة، والعلاجُ حسابٌ آخر لا صبر.
    - 401 ⇒ التوكن باطلٌ أو منتهٍ، والعلاجُ تجديدٌ لا تبديل.
    - 5xx أو لا جواب ⇒ المصدرُ نفسه، ولا يُلمس الحساب.
    """
    live = [c for c in codes if c is not None]
    if not live:
        return "warn", "لا جواب من المصدر — ليس الحساب"
    if all(c == 200 for c in live):
        return "good", f"الحساب يعمل — {len(live)}/{len(live)} ردّت 200"
    if all(c == 403 for c in live):
        return "bad", f"محجوب — 403 على {len(live)} مسارات؛ الهويّة موقوفة"
    if any(c == 401 for c in live):
        return "bad", "التوكن باطل أو منتهٍ — HTTP 401 (تجديد لا تبديل)"
    if any(c == 403 for c in live):
        forbidden = sum(1 for c in live if c == 403)
        return "bad", f"حجبٌ جزئيّ — 403 على {forbidden} من {len(live)}"
    if all(c >= 500 for c in live):
        return "warn", "المصدر متعثّر — 5xx وليس الحساب"
    counts = ", ".join(f"{c}×{live.count(c)}" for c in sorted(set(live)))
    return "warn", f"مختلط — {counts}"


def probe() -> dict[str, Any]:
    """يضرب ثلاثةَ مساراتٍ بالتوكن الحاليّ ويعيد الرموزَ الخام.

    بـ`curl_cffi` لا `httpx` — بصمةُ Chrome شرطُ العبور من Cloudflare، ونداءٌ
    عاديّ كان سيردّ صفحةَ تحدٍّ فيُقرأ «محجوب» على حسابٍ سليم.

    و`/feed` مستثنًى من المِجَسّ: يشترط `feedTypes`، وبدونها يردّ 400 فيُقرأ
    فشلاً وهو أدبُ تطبيقٍ لا منع. ثلاثةُ مساراتٍ تكفي للحكم.
    """
    raw = _read()
    token = raw.get("access_token")
    if not token:
        raise AccountError("لا توكن في الملفّ — لا شيء لأفحصه.")

    try:
        from curl_cffi.requests import Session
    except ImportError as exc:  # pragma: no cover - المكتبةُ مثبّتةٌ مع الـapi
        raise AccountError("curl_cffi غيرُ مثبّتة — تعذّر الفحص.", 500) from exc

    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": config.FOMO_APP_ORIGIN,
        "referer": config.FOMO_APP_ORIGIN + "/",
    }
    rows: list[dict[str, Any]] = []
    codes: list[int | None] = []
    with Session(
        impersonate="chrome124", headers=headers, timeout=config.ACCOUNT_PROBE_TIMEOUT
    ) as session:
        for method, path, body in _PROBE_PATHS:
            url = config.FOMO_UPSTREAM_BASE + path
            try:
                response = (
                    session.get(url) if method == "GET" else session.post(url, json=body)
                )
                code: int | None = response.status_code
                note = ""
            except Exception as exc:  # noqa: BLE001 — فشلُ النقل جوابٌ أيضاً
                code, note = None, type(exc).__name__
            codes.append(code)
            rows.append({"method": method, "path": path, "status": code, "note": note})

    level, detail = _verdict(codes)
    result = {
        "level": level,
        "detail": detail,
        "rows": rows,
        "did": _did(token),
        "at": datetime.now(UTC).isoformat(),
    }
    _LAST_PROBE.clear()
    _LAST_PROBE.update(result)
    return result


def restore(name: str) -> dict[str, Any]:
    """يرجع إلى نسخةٍ محفوظة — لأنّ لصقةً خاطئة تُسكِت الجمعَ كلَّه."""
    safe = os.path.basename(str(name or ""))
    if not safe.startswith(_BAK_PREFIX):
        raise AccountError("اسمُ نسخةٍ غيرُ معروف.", 404)
    source = os.path.join(os.path.dirname(_path()), safe)
    if not os.path.exists(source):
        raise AccountError("النسخةُ لم تُعد موجودة.", 404)
    with _LOCK:
        try:
            with open(source, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            raise AccountError(f"النسخةُ غيرُ مقروءة: {type(exc).__name__}", 500) from exc
        if not isinstance(raw, dict) or not raw.get("access_token"):
            raise AccountError("النسخةُ لا تحمل توكناً — لا تصلح للاستعادة.")
        _backup_now("restore")
        _write({name: raw.get(name) for name in _FIELDS})
    _LAST_PROBE.clear()
    return {
        "ok": True,
        "did": _did(raw.get("access_token")),
        "token_tail": _tail(raw.get("access_token")),
        "message": f"استُعيدت النسخة {safe}.",
    }
