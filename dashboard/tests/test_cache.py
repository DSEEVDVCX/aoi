"""اختبارات الذاكرة المؤقّتة: بلا انتظارٍ حقيقيّ وبلا اعتمادٍ على جدولة الخيوط.

الساعةُ والمُشعِلُ محقونان، فالبياتةُ تُصنع بتقديم عقربٍ لا بـ`sleep`، والتجديدُ
الخلفيّ يُنفَّذ حين نقرّر نحن. اختبارٌ ينتظر ثانيتين هشّ، وآخرُ يعتمد على من يسبق
من في الجدولة يفشل مرّةً من عشر — وكلاهما يجعل الفشلَ الحقيقيّ غيرَ مقروء.
"""
import threading

import cache
import pytest


class Clock:
    """عقربٌ يدويّ. `advance` هو الطريقةُ الوحيدة لمرور الزمن في هذه الاختبارات."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Spawner:
    """يحتفظ بالتجديدات المؤجّلة ولا ينفّذها إلّا عند `run_all`."""

    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, run) -> None:
        self.pending.append(run)

    def run_all(self) -> int:
        queued, self.pending = self.pending, []
        for run in queued:
            run()
        return len(queued)


@pytest.fixture()
def memo():
    clock, spawn = Clock(), Spawner()
    memo = cache.TTLMemo(clock=clock, wall_clock=clock, spawn=spawn, error_backoff=15.0)
    memo.clock, memo.spawn = clock, spawn      # للوصول من الاختبار
    return memo


def _counter():
    """دالةُ حسابٍ تعدّ نداءاتها — بها نعرف «حُسب مرّةً» من «حُسب ثلاثةَ عشر»."""
    calls = []

    def compute():
        calls.append(len(calls) + 1)
        return f"v{len(calls)}"

    return compute, calls


# --- الحساب البارد ---
def test_cold_call_computes_and_returns_fresh(memo):
    compute, calls = _counter()
    value, meta = memo.get("k", 60.0, compute)
    assert (value, len(calls)) == ("v1", 1)
    assert meta["stale"] is False
    assert meta["age_seconds"] == 0.0
    assert meta["error"] is None
    assert meta["ttl_seconds"] == 60.0
    assert meta["computed_at"].endswith("+00:00")


def test_within_ttl_does_not_recompute(memo):
    compute, calls = _counter()
    memo.get("k", 60.0, compute)
    memo.clock.advance(59.0)
    value, meta = memo.get("k", 60.0, compute)
    assert (value, len(calls)) == ("v1", 1)
    assert meta["age_seconds"] == 59.0
    assert meta["stale"] is False
    assert memo.spawn.pending == []


# --- البائتةُ تُقدَّم فوراً والتجديدُ في الخلف ---
def test_stale_returns_old_value_without_waiting(memo):
    """هذا هو جوهرُ التصميم: بعُمرٍ ساذجٍ كان الطلبُ الثاني عشر يدفع الثانيتين."""
    compute, calls = _counter()
    memo.get("k", 60.0, compute)
    memo.clock.advance(61.0)

    value, meta = memo.get("k", 60.0, compute)
    assert value == "v1"                     # القديمةُ رجعت فوراً
    assert len(calls) == 1                   # ولم يُحسب شيءٌ في مسار الطلب
    assert meta["stale"] is True
    assert meta["refreshing"] is True

    assert memo.spawn.run_all() == 1          # التجديدُ كان مؤجّلاً فعلاً
    assert len(calls) == 2
    value, meta = memo.get("k", 60.0, compute)
    assert (value, meta["stale"], meta["refreshing"]) == ("v2", False, False)


def test_only_one_refresh_in_flight(memo):
    """طلباتُ الصفحة متوازية: بلا هذا الحرس تُشعل ثلاثةَ عشرَ مسحاً في آنٍ واحد."""
    compute, calls = _counter()
    memo.get("k", 60.0, compute)
    memo.clock.advance(61.0)

    for _ in range(13):
        assert memo.get("k", 60.0, compute)[0] == "v1"
    assert len(memo.spawn.pending) == 1

    memo.spawn.run_all()
    assert len(calls) == 2


def test_concurrent_cold_callers_compute_once():
    """حسابٌ واحد للبارد أيضاً — والخيوطُ هنا حقيقيّة لأنّ المُختبَر هو القفل."""
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def compute():
        calls.append(1)
        started.set()
        release.wait(5)
        return "value"

    memo = cache.TTLMemo(spawn=lambda run: None)
    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(memo.get("k", 60.0, compute)[0]))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    assert started.wait(5)
    release.set()
    for t in threads:
        t.join(5)

    assert results == ["value"] * 6
    assert len(calls) == 1


# --- الفشل ---
def test_failed_refresh_keeps_stale_value(memo):
    """قاعدةٌ مشغولة تُسقط تجديداً؛ القيمةُ القديمة أفضلُ من لا شيء."""
    calls: list[int] = []

    def compute():
        calls.append(1)
        if len(calls) == 1:
            return "good"
        raise sqlite_busy()

    memo.get("k", 60.0, compute)
    memo.clock.advance(61.0)
    memo.get("k", 60.0, compute)
    memo.spawn.run_all()

    value, meta = memo.get("k", 60.0, compute)
    assert value == "good"
    assert meta["error"].startswith("OperationalError: database is locked")
    assert meta["stale"] is True
    assert meta["refreshing"] is False


def test_failure_respects_backoff_then_retries(memo):
    """بلا تمهّلٍ يصير كلُّ طلبٍ محاولةً فاشلة جديدة — عشرُ محاولاتٍ في الدقيقة."""
    calls: list[int] = []

    def compute():
        calls.append(1)
        if len(calls) == 1:
            return "good"
        if len(calls) == 2:
            raise sqlite_busy()
        return "recovered"

    memo.get("k", 60.0, compute)
    memo.clock.advance(61.0)
    memo.get("k", 60.0, compute)
    memo.spawn.run_all()
    assert len(calls) == 2

    memo.clock.advance(5.0)                   # داخل التمهّل ⇒ لا محاولة
    memo.get("k", 60.0, compute)
    assert memo.spawn.pending == []

    memo.clock.advance(11.0)                  # انتهى التمهّل ⇒ محاولةٌ واحدة
    memo.get("k", 60.0, compute)
    assert memo.spawn.run_all() == 1
    assert memo.get("k", 60.0, compute)[0] == "recovered"
    assert memo.get("k", 60.0, compute)[1]["error"] is None


def test_error_backoff_argument_overrides_default(memo):
    calls: list[int] = []

    def compute():
        calls.append(1)
        if len(calls) == 1:
            return "good"
        raise sqlite_busy()

    memo.get("k", 60.0, compute, error_backoff=1.0)
    memo.clock.advance(61.0)
    memo.get("k", 60.0, compute, error_backoff=1.0)
    memo.spawn.run_all()

    memo.clock.advance(2.0)                   # تمهّلٌ أقصر من الافتراضيّ (15ث)
    memo.get("k", 60.0, compute, error_backoff=1.0)
    assert len(memo.spawn.pending) == 1


def test_cold_failure_propagates(memo):
    """لا قيمةَ تُقدَّم ⇒ الخطأ يخرج للمنادي: مسارُ FastAPI يعرضه، لا يكتمه."""
    def compute():
        raise sqlite_busy()

    with pytest.raises(Exception, match="database is locked"):
        memo.get("k", 60.0, compute)


def test_warm_swallows_failure(memo):
    def bad():
        raise sqlite_busy()

    assert memo.warm("k", 60.0, bad) is False
    assert memo.warm("j", 60.0, lambda: "ok") is True
    assert memo.get("j", 60.0, lambda: "other")[0] == "ok"


# --- إدارة المفاتيح ---
def test_keys_are_independent(memo):
    memo.get("a", 60.0, lambda: "A")
    memo.get("b", 60.0, lambda: "B")
    assert memo.get("a", 60.0, lambda: "x")[0] == "A"
    assert memo.get("b", 60.0, lambda: "x")[0] == "B"


def test_invalidate_forces_recompute(memo):
    compute, calls = _counter()
    memo.get("k", 60.0, compute)
    memo.invalidate("k")
    assert memo.get("k", 60.0, compute)[0] == "v2"
    assert len(calls) == 2


def test_clear_drops_every_key(memo):
    memo.get("a", 60.0, lambda: "A")
    memo.get("b", 60.0, lambda: "B")
    memo.clear()
    assert memo.get("a", 60.0, lambda: "fresh")[0] == "fresh"
    assert memo.get("b", 60.0, lambda: "fresh")[0] == "fresh"


def test_zero_ttl_refreshes_every_call_but_never_blocks(memo):
    compute, calls = _counter()
    memo.get("k", 0.0, compute)
    value, meta = memo.get("k", 0.0, compute)
    assert value == "v1"                      # ما زال بلا انتظار
    assert meta["stale"] is True
    memo.spawn.run_all()
    assert len(calls) == 2


def sqlite_busy():
    import sqlite3

    return sqlite3.OperationalError("database is locked")
