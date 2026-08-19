"""إدارةُ مفاتيح المزوّدين من اللوحة: قراءةٌ، إضافةٌ، إيقافٌ، حذفٌ، واختبارٌ حيّ.

هذه أوّلُ كتابةٍ تملكها اللوحة بعد أن صارت قراءةً محضة، فحدودُها مرسومةٌ بدقّة:

- الهدفُ ملفُّ `recorder/chain_keys.json` وحدَه. قاعدةُ البيانات تبقى `mode=ro`
  كما هي — لا سطرَ كتابةٍ واحد إليها، فلا مزاحمةَ على قفل الكاتب.
- صيغةُ الملفّ ليست معرّفةً هنا بل في `recorder/key_file.py`، ويُحمَّل بمسارٍ
  صريح لا عبر `sys.path`: للمشروعين `config.py` بنفس الاسم، فإضافةُ مجلّد
  المسجّل إلى المسار كانت ستجعل استيرادَ `config` رهناً بالترتيب.
- القيمةُ لا تُعاد أبداً. ما يخرج من هنا: اسمُ الحساب، وآخرُ أربعة أحرف
  (`key_file.tail` — خفضٌ مقصودٌ لِـ FR-013 طلبَه المستخدم للتمييز)، ومؤشّرات.
- والقيمةُ لا تُسجَّل أبداً: رسائلُ الاختبار تُشطب منها قيمةُ المفتاح قبل
  عرضها، لأنّ خطأ المزوّد كثيراً ما يردّ الرابطَ كاملاً وفيه المفتاح.
"""
from __future__ import annotations

import hmac
import importlib.util
import os
import re
import secrets
import threading
from datetime import UTC, datetime
from typing import Any

import config
import httpx


def _load_key_file():
    """يحمّل `recorder/key_file.py` بمسارٍ صريح باسمٍ فريد.

    باسمٍ فريد (`aoi_key_file`) لا `key_file`: لو استوردت اللوحةُ يوماً وحدةً
    بنفس الاسم لم يتنازعا في `sys.modules`.
    """
    path = os.path.join(config.RECORDER_DIR, "key_file.py")
    spec = importlib.util.spec_from_file_location("aoi_key_file", path)
    if spec is None or spec.loader is None:  # pragma: no cover - مسارٌ مفقود
        raise RuntimeError(f"تعذّر تحميل صيغة ملفّ المفاتيح: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


key_file = _load_key_file()

# قفلٌ لكلّ تعديل: التعديلُ قراءةٌ ثمّ كتابة، وطلبان متزامنان من نفس اللوحة
# كانا سيقرآن نفس الحالة فيضيع أحدُ التعديلين بلا أثر.
_LOCK = threading.Lock()

# نتائجُ الاختبار الحيّ — في الذاكرة لا في القاعدة ولا في الملفّ. سبب ذلك أنّ
# نتيجةَ فحصٍ لحظيّة، وكتابتُها في القاعدة كانت ستُدخل الذيلَ في ملفٍّ يُنسخ
# احتياطيّاً. تُفقد بإعادة تشغيل اللوحة، وهذا مقبول: يُعاد الفحص بضغطة.
_PROBES: dict[tuple[str, str], dict[str, Any]] = {}

# اسمُ الحساب: نصٌّ بشريّ قصير. نمنع محارف التحكّم وأقواسَ الوسوم كي لا يتحوّل
# إلى وسمٍ في الصفحة، ونقصّه فلا يزحم السطر.
_LABEL_MAX = 60
_BAD_LABEL = re.compile(r"[\x00-\x1f<>]")

# المفتاح: قيمةٌ واحدة بلا فراغات. الحدّان يمنعان اللصقَ الخاطئ (سطرٌ كامل من
# ملفّ .env مثلاً) قبل أن يصير مفتاحاً ميّتاً في الحوض يُبرَّد كلَّ دورة.
_KEY_MIN, _KEY_MAX = 8, 200


class KeyStoreError(RuntimeError):
    """طلبٌ مرفوض بسببٍ يُعرض للمستخدم كما هو."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _meta(provider: str) -> dict[str, Any]:
    try:
        return key_file.PROVIDERS[provider]
    except KeyError:
        raise KeyStoreError(f"مزوّد غير معروف: {provider}", 404) from None


def _path() -> str:
    return config.CHAIN_KEYS_PATH


def _env_override(provider: str) -> bool:
    """هل يتقدّم متغيّرُ البيئة على الملفّ لهذا المزوّد؟

    إن كان مضبوطاً فتحريرُ الملفّ لا يفعل شيئاً: `provider_keys.read_keys`
    يرجع من البيئة قبل أن يفتح الملفّ. فالصمتُ هنا كان سيعني «أضفتُ مفتاحاً
    ولا يعمل» بلا سبب ظاهر.
    """
    return bool(os.environ.get(_meta(provider)["env"], "").strip())


def _entries(provider: str) -> list[dict[str, Any]]:
    meta = _meta(provider)
    return key_file.load_entries(_path(), meta["plural"], meta["singular"])


def _write(provider: str, rows: list[dict[str, Any]]) -> None:
    meta = _meta(provider)
    try:
        key_file.save_entries(_path(), meta["plural"], meta["singular"], rows)
    except (OSError, ValueError) as exc:
        raise KeyStoreError(f"تعذّرت الكتابة: {type(exc).__name__}", 500) from exc


def _clean_label(label: str) -> str:
    text = _BAD_LABEL.sub(" ", str(label or "")).strip()
    return text[:_LABEL_MAX]


def _clean_key(value: str) -> str:
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        raise KeyStoreError("المفتاح فارغ أو يحتوي فراغات — الصقه وحدَه")
    if not (_KEY_MIN <= len(text) <= _KEY_MAX):
        raise KeyStoreError(f"طول المفتاح غير معقول ({len(text)} حرفاً)")
    return text


def _locate(
    provider: str, slot: int, expect_tail: str, expect_id: str = "",
) -> tuple[list[dict], int]:
    """يجد سطراً بالموضع، ويتحقّق من الذيل قبل تعديله.

    الموضعُ وحده لا يكفي: بين رسمِ الصفحة وضغطِ الزرّ قد يكون الملفُّ تغيّر
    (تحريرٌ يدويّ، أو لسانُ متصفّحٍ آخر)، فيصير الموضعُ 1 مفتاحاً آخر ويُحذف
    السليمُ بدل المقصود. فنطابق الذيلَ الذي رُسم به السطر — وهو مقارنةٌ لا
    إظهار: المستخدم يملكه في صفحته أصلاً.
    """
    rows = _entries(provider)
    if not 0 <= slot < len(rows):
        raise KeyStoreError("تغيّر الملفّ — أعد تحميل الصفحة", 409)
    if key_file.tail(rows[slot]["key"]) != str(expect_tail or ""):
        raise KeyStoreError("تغيّر الملفّ — أعد تحميل الصفحة", 409)
    if expect_id:
        if _key_id(rows[slot]["key"]) != expect_id:
            raise KeyStoreError("تغيّر الملفّ — أعد تحميل الصفحة", 409)
    elif sum(key_file.tail(row["key"]) == str(expect_tail or "") for row in rows) > 1:
        raise KeyStoreError("آخر أحرف المفتاح غير فريدة — أعد تحميل الصفحة", 409)
    return rows, slot


def _redact(text: str, key: str) -> str:
    """يشطب المفتاح من نصٍّ قبل عرضه. نفس درس `solana_rpc._redact`."""
    out = str(text)
    if key:
        out = out.replace(key, "<محجوب>")
    return re.sub(r"(api-key=|Bearer\s+)[^\s\"'&)>]+", r"\1<محجوب>", out)


# ملحٌ عشوائيّ يُولَد مرّةً لكلّ عمليّة ولا يخرج من الذاكرة.
_ID_SALT = secrets.token_bytes(32)


def _key_id(key: str) -> str:
    """هويّةٌ تميّز مفتاحاً عن آخر: ثابتةٌ داخل العمليّة، بلا معنى خارجها.

    الذيلُ الرباعيّ لا يكفي هويّةً — مفتاحان ينتهيان بـ`wxyz` يجعلان `_locate`
    يطابق الخطأ فيُحذف السليم، ونتيجةَ فحصٍ لأحدهما تُلوّن الآخر. فاحتجنا معرّفاً
    فريداً يُرسَل مع الزرّ.

    وهو **ليس** بصمةَ المفتاح: `sha256(key)` كانت ستؤدّي الوظيفةَ نفسها وتخالف
    القاعدةَ («لا قيمة، ولا شَظيّة، ولا بصمة»)، لأنّها تصلح مِحكّاً — من يملك
    قائمةَ مفاتيحٍ مرشَّحة يؤكّد بها أيَّها المستعمل هنا — وتصلح رابطاً يُطابق
    نفسَ المفتاح بين نظامين. أمّا HMAC بملحٍ عشوائيٍّ فناتجُه لافتةٌ عشوائيّة:
    لا تُشتقّ منها قيمة ولا تُطابق من خارج هذه العمليّة.

    والملحُ يموت بموتِ العمليّة، فالهويّاتُ تبطل عند الإقلاع. وهذا هو الصواب لا
    عيبٌ فيه: صفحةٌ مفتوحةٌ من قبل الإقلاع تُعاد تحميلاً بدل أن يُصدَّق زرُّها،
    ولوحُ المفاتيح يُرسَم كلَّ دورةِ تحديثٍ فالنافذةُ ثوانٍ.
    """
    return hmac.new(_ID_SALT, key.strip().encode("utf-8"), "sha256").hexdigest()[:32]


# --- العرض ---
def _pool_view(pool_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """يلمُّ حالةَ كلّ مزوّدٍ من أحواض كلّ العمليّات المالكة.

    المزوّدُ قد يوجد في عمليّتين بحالتين مستقلّتين (كان GoldRush في `FomoChain`
    و`FomoEVMReplay` كذلك)، فنجمع لا نُرجّح: موضعٌ مبرَّدٌ في إحداهما مبرَّدٌ فعلاً،
    وذكرُ المالكِ يجعل السببَ مفهوماً بدل «مبرَّد» مجهولةِ المصدر.
    """
    view: dict[str, dict[str, Any]] = {}
    for row in pool_rows:
        state = view.setdefault(
            row["provider"], {"cooled": {}, "in_use": set(), "disabled_by": [], "owners": []},
        )
        state["owners"].append(row["owner"])
        if row.get("disabled"):
            state["disabled_by"].append(row["owner"])
        # تقريرٌ متقادمٌ وصفٌ للماضي: التبريدُ والاستعمالُ حالتان لحظيّتان تنتهيان
        # مع الدورة، فتلوينُ مفتاحٍ بعينه بهما من تقريرٍ بائتٍ كذبٌ صريح. أمّا
        # المالكُ والتعطيلُ فوصفُ إعدادٍ لا لحظة، وكتمانُهما يقول «سليم» عن
        # مزوّدٍ مُطفأ — وسطرُ الحوض يحمل علامةَ stale فالقارئ يرى المصدر.
        if row.get("stale"):
            continue
        for index in row.get("blocked_index") or []:
            state["cooled"].setdefault(int(index), []).append(row["owner"])
        if row.get("keys"):
            state["in_use"].add(int(row.get("index") or 0))
    return view


def rows(pool_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """أسطرُ المفاتيح للعرض: اسمُ الحساب، الذيل، والحالة. **لا قيمة**."""
    pools = _pool_view(pool_rows)
    out: list[dict[str, Any]] = []
    providers: list[dict[str, Any]] = []
    for provider, meta in key_file.PROVIDERS.items():
        env = _env_override(provider)
        entries = _entries(provider)
        state = pools.get(provider, {})
        disabled_by = state.get("disabled_by") or []
        providers.append({
            "provider": provider,
            "title": meta["title"],
            "env": env,
            "env_name": meta["env"],
            "keys": len(entries),
            "enabled": sum(1 for row in entries if row["enabled"]),
            "owners": sorted(set(state.get("owners") or [])),
            "disabled_by": sorted(disabled_by),
        })
        # موضعُ الحوض يُحسب على المفعّلة فقط وبنفس الترتيب: الحوضُ يُبنى من
        # `read_keys` التي تُصفّي المعطّلة، فترقيمُ الملفّ كان سيُزيح الألوان.
        pool_slot = 0
        for slot, row in enumerate(entries):
            enabled = row["enabled"]
            cooled_by = sorted(state.get("cooled", {}).get(pool_slot, [])) if enabled else []
            in_use = enabled and pool_slot in (state.get("in_use") or set())
            tail = key_file.tail(row["key"])
            probe = _PROBES.get((provider, _key_id(row["key"])))
            out.append({
                "provider": provider,
                "title": meta["title"],
                "slot": slot,
                "pool_slot": pool_slot if enabled else None,
                "label": row["label"],
                "tail": tail,
                "key_id": _key_id(row["key"]),
                "enabled": enabled,
                "env": env,
                "cooled_by": cooled_by,
                "in_use": in_use,
                "provider_disabled": bool(disabled_by) and enabled,
                "probe": probe,
                "level": (
                    "off" if not enabled
                    else "bad" if (probe and probe["level"] == "bad") or disabled_by
                    else "warn" if cooled_by or (probe and probe["level"] == "warn")
                    else "good"
                ),
            })
            if enabled:
                pool_slot += 1
    return {"keys": out, "providers": providers}


# --- التعديل ---
def add(provider: str, key: str, label: str) -> dict[str, Any]:
    value = _clean_key(key)
    name = _clean_label(label)
    with _LOCK:
        if _env_override(provider):
            raise KeyStoreError(
                f"مضبوطٌ من البيئة ({_meta(provider)['env']}) — الملفّ مُهمَل هناك", 409,
            )
        rows_now = _entries(provider)
        if any(row["key"] == value for row in rows_now):
            raise KeyStoreError("هذا المفتاح موجود بالفعل", 409)
        rows_now.append({"key": value, "label": name, "enabled": True})
        _write(provider, rows_now)
    return {"ok": True, "tail": key_file.tail(value), "keys": len(rows_now)}


def set_enabled(
    provider: str, slot: int, expect_tail: str, enabled: bool, expect_id: str = "",
) -> dict[str, Any]:
    with _LOCK:
        if _env_override(provider):
            raise KeyStoreError(
                f"مضبوطٌ من البيئة ({_meta(provider)['env']}) — الملفّ مُهمَل هناك", 409,
            )
        rows_now, index = _locate(provider, slot, expect_tail, expect_id)
        rows_now[index]["enabled"] = bool(enabled)
        _write(provider, rows_now)
        left = sum(1 for row in rows_now if row["enabled"])
    return {"ok": True, "enabled": bool(enabled), "warning": _shortfall(provider, left)}


def remove(provider: str, slot: int, expect_tail: str, expect_id: str = "") -> dict[str, Any]:
    with _LOCK:
        if _env_override(provider):
            raise KeyStoreError(
                f"مضبوطٌ من البيئة ({_meta(provider)['env']}) — الملفّ مُهمَل هناك", 409,
            )
        rows_now, index = _locate(provider, slot, expect_tail, expect_id)
        rows_now.pop(index)
        _write(provider, rows_now)
        left = sum(1 for row in rows_now if row["enabled"])
    return {"ok": True, "keys": len(rows_now), "warning": _shortfall(provider, left)}


def _shortfall(provider: str, enabled_left: int) -> str | None:
    """تحذيرٌ بعد الفعل لا منعٌ قبله: قد يكون الإفراغ مقصوداً.

    لا نمنع حذفَ آخرِ مفتاح — قد يكون المزوّد قد أُلغي حسابُه فعلاً — لكنّ
    الأثرَ يُقال بصراحة: الطبقةُ ستتوقّف بخطأ «لا مفاتيح» كلَّ دورة.
    """
    if enabled_left:
        return None
    return f"لم يبقَ مفتاحٌ مفعّل لِـ {_meta(provider)['title']} — الطبقة ستتوقّف"


# --- الاختبار الحيّ ---
def _classify(status: int) -> tuple[str, str]:
    """يفصل «المفتاح مرفوض» عن «الخدمة متعطّلة» — نفس تقسيم `_step_aside`.

    الخلطُ بينهما هو الخطأ المكلف: عرضُ نقطةٍ حمراء على مفتاحٍ سليمٍ لأنّ
    المزوّد كان يتعثّر لحظةَ الفحص يدفع المستخدم إلى حذفِ مفتاحٍ صالح.
    """
    if status == 200:
        return "good", "سليم"
    if status in (401, 403):
        return "bad", f"مرفوض — HTTP {status}"
    if status == 402:
        return "bad", "نفد الرصيد — HTTP 402"
    if status == 429:
        return "warn", "محدود مؤقّتاً — HTTP 429 (المفتاح صالح)"
    if status >= 500:
        return "warn", f"الخدمة متعطّلة — HTTP {status} (ليس المفتاح)"
    return "warn", f"HTTP {status}"


def probe(provider: str, slot: int, expect_tail: str, expect_id: str = "") -> dict[str, Any]:
    """نداءٌ حقيقيّ واحد بهذا المفتاح بعينه، إلى العنوان الذي يستعمله العميل."""
    meta = _meta(provider)
    rows_now, index = _locate(provider, slot, expect_tail, expect_id)
    value = rows_now[index]["key"]
    spec = meta["probe"]
    url = spec["url"].format(key=value)
    headers = {name: text.format(key=value) for name, text in (spec.get("headers") or {}).items()}
    try:
        with httpx.Client(timeout=config.KEY_PROBE_TIMEOUT) as client:
            response = client.request(
                spec["method"], url, headers=headers or None,
                params=spec.get("params"), json=spec.get("json"),
            )
        level, detail = _classify(response.status_code)
        if level == "good":
            # 200 لا يكفي دائماً: JSON-RPC يردّ 200 وفيه `error`.
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("error") not in (None, False):
                level, detail = "bad", _redact(str(body["error"])[:120], value)
        status: int | None = response.status_code
    except httpx.TimeoutException:
        level, detail, status = "warn", f"مهلة {config.KEY_PROBE_TIMEOUT:g}ث — لا استجابة", None
    except httpx.HTTPError as exc:
        level, detail, status = "warn", _redact(f"{type(exc).__name__}", value), None

    result = {
        "level": level,
        "detail": detail,
        "status": status,
        "at": datetime.now(UTC).isoformat(),
    }
    _PROBES[(provider, _key_id(value))] = result
    return result
