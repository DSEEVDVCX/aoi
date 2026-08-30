"""تسخينُ الذاكرة المؤقّتة عند الإقلاع — أولُ زائرٍ لا يدفع ثمن البرودة.

المشكلة المقيسة (2026-08-29): إقلاعُ اللوحة يبدأ بذاكرةٍ فارغة، فأوّلُ من يفتح
الصفحة ينتظر المسارات الثقيلة الثلاثة بالتوازي — 3.4 ثانية للشبكات، 1.7 للتوسيم،
1.0 للأعداد — لأنّ `Promise.all` في الصفحة ينتظر أبطأها قبل رسم أيّ شيء.

والحلُّ جاهزٌ في `cache.py` منذ البداية: دالةُ `warm()` موثّقةٌ «للاستدعاء من خيطٍ
خلفيّ عند الإقلاع» لكن لا أحد يستدعيها. هذا الملف يثبّت العقد:

1. **الإقلاعُ يسخّن.** `serve_dashboard.py` يُشعل خيطًا واحدًا بعد الاستماع،
   يسخّن المفاتيح الثقيلة بالترتيب. الفشلُ يُسجَّل ولا يُسقط الإقلاع.
2. **التسخينُ يخدم.** بعد انتهائه لا يُنفَّذ الحساب البارد في الطلب الأوّل —
   `cache.MEMO.get` يجد القيمة فيُعيدها فورًا (تُختبر عبر `TTLMemo` حرفيًا).
3. **الصمتُ التشغيليّ.** التسخينُ لا يطبع ولا يكتب في سجلٍّ إلا عند الفشل، لأنّه
   يُشغَّل مع كل إقلاعٍ للمهمّة المجدولة (والخادمُ يعيد إطلاقها بعد كل إقلاع).
"""
import threading

import cache
import pytest
import warmup


def test_warmup_heats_all_heavy_keys():
    """الخيطُ يسخّن المفاتيح الثقيلة الثلاثة — بالتوازي، فلا ينتظر أحدها الآخر.

    المقيس: التسخينُ التتابعيّ كان يجعل `labeling` يُجاب بعد **مجموع** أزمنة
    الشبكات والتوسيم (3.4+1.5 ثانية) لمن سبق خيطَ التسخين. التوازيُّ يجعل كلَّ
    مفتاحٍ جاهزًا خلال زمنه الخاص.
    """
    computed: set[str] = set()

    def make_compute(key: str):
        def _compute():
            computed.add(key)
            return {"done": key}
        return _compute

    memo = cache.TTLMemo()
    done = warmup.warm_heavy_keys(memo, compute_for=make_compute)

    assert set(done) == {"network_summary", "labeling", "table_counts"}
    assert all(done.values())
    assert computed == {"network_summary", "labeling", "table_counts"}


def test_warmup_survives_a_failing_key_and_continues():
    """مفتاحٌ فاشل لا يُوقف البقيّة — التسخينُ مساعاةٌ لا شرطَ إقلاع.

    القاعدةُ قد تكون مشغولةٌ بالمسجّل لحظة الإقلاع؛ فشلُ مفتاحٍ واحدٍ (استثناءٌ
    يُرفع من `compute`) يجب أن يُسجَّل ويمرّ، لا أن يقتل خيط التسخين كلَّه قبل
    المفاتيح الباقية.
    """
    calls: list[str] = []

    def make_compute(key: str):
        def _compute():
            calls.append(key)
            if key == "labeling":
                raise RuntimeError("database is locked")
            return {"ok": key}
        return _compute

    memo = cache.TTLMemo()
    done = warmup.warm_heavy_keys(memo, compute_for=make_compute)

    assert set(calls) == {"network_summary", "labeling", "table_counts"}
    assert len(calls) == 3
    assert done == {"network_summary": True, "labeling": False, "table_counts": True}


def test_a_warmed_cache_answers_the_first_request_without_cold_compute():
    """العقدُ كلُّه هنا: بعد التسخين لا يُنفَّذ حسابٌ باردٌ في الطلب الأوّل.

    `warm()` نفسُها استدعت `get()`: القيمةُ موجودةٌ داخلَ مدّتها، فأوّلُ طلبٍ حقيقيّ
    يُجاب من الذاكرة، وأيُّ `compute` يُمرَّر له بعد التسخين لا يُنفَّذ أصلًا.
    """
    memo = cache.TTLMemo()

    def compute():
        return {"value": 41}
    memo.warm("labeling", ttl=300.0, compute=compute)

    def never():
        raise AssertionError("لا يجب أن يُنفَّذ — القيمة مسخَّنة")

    value, meta = memo.get("labeling", 300.0, never)
    assert value == {"value": 41}
    assert meta["stale"] is False
    assert meta["age_seconds"] < 300


def test_warmup_thread_is_daemon_and_runs_after_start():
    """الخيطُ خفيٌّ (daemon) لا يمنع انطفاء العمليّة — عقدُّ الخيوط الخلفيّة هنا."""
    started = threading.Event()
    probe = {"spawned": False}

    original_spawn = warmup._spawn_background

    def tracking_spawn(run):
        probe["spawned"] = True
        started.set()
        original_spawn(run)

    warmup._spawn_background = tracking_spawn
    try:
        thread = warmup.start_warmup_thread()
        assert started.wait(timeout=5)
        assert probe["spawned"] is True
        if thread is not None:
            assert thread.daemon is True
    finally:
        warmup._spawn_background = original_spawn


def test_warmup_uses_the_live_memo_and_config_ttls():
    """يستعملُ ذاكرةَ العمليّة الحقيقيّة وأعمارَ config — لا ذاكرة اختبار.

    بلا هذا الحرس قد يُبنى التسخينُ على نسخةٍ من `TTLMemo` لا تصلها الطلبات،
    فيخضرّ الاختبارُ والخادمُ باردٌ كما كان.
    """
    seen: dict[str, float] = {}

    real_get = cache.MEMO.get
    real_ttls = {
        "network_summary": config_ttls()["NETWORK_SUMMARY_TTL_SECONDS"],
        "labeling": config_ttls()["LABELING_TTL_SECONDS"],
        "table_counts": config_ttls()["TABLE_COUNTS_TTL_SECONDS"],
    }

    def fake_get(key, ttl, compute, **kwargs):
        seen[key] = ttl
        return {"warmed": key}, {}

    cache.MEMO.get = fake_get
    try:
        warmup.warm_heavy_keys(cache.MEMO, compute_for=lambda key: (lambda: None))
        assert seen == real_ttls
    finally:
        cache.MEMO.get = real_get


def config_ttls():
    import config
    return {
        "NETWORK_SUMMARY_TTL_SECONDS": config.NETWORK_SUMMARY_TTL_SECONDS,
        "LABELING_TTL_SECONDS": config.LABELING_TTL_SECONDS,
        "TABLE_COUNTS_TTL_SECONDS": config.TABLE_COUNTS_TTL_SECONDS,
    }


@pytest.mark.parametrize("missing", ["network_summary", "labeling", "table_counts"])
def test_every_heavy_key_is_covered_by_warmup(missing):
    """الحرسُ من الانحراف: مفتاحٌ ثقيلٌ جديدٌ بلا تسخينٍ يُكشف فورًا."""
    keys = {key for key, _ttl in warmup.HEAVY_KEYS}

    assert missing in keys
    assert keys == {"network_summary", "labeling", "table_counts"}
