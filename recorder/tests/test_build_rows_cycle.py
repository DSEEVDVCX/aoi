"""اختبارات دورة بناء صفوف التدريب المجدولة (بلا شبكة، بلا قاعدة حقيقيّة).

المُختبَر هو `run_cycle` لا `build`: منطق الدفعات والسقف هو ما أضافه المُشغّل،
وهو ما قد ينحرف صامتاً. `build` نفسه مغطّى في test_features / test_flow_and_coverage.
"""
import config
import pytest
import run_build_rows


class _FakeBuild:
    """بديل `build` يُرجع أعداداً مُبرمَجة ويسجّل حدّ كل نداء."""

    def __init__(self, per_call):
        self.per_call = list(per_call)
        self.takes = []

    def __call__(self, _db, rebuild, limit, dry, candidates_only):
        assert rebuild is False, "المُشغّل يجب ألّا يمرّر rebuild أبداً"
        assert dry is False
        assert candidates_only is False
        self.takes.append(limit)
        built = self.per_call.pop(0) if self.per_call else 0
        return {"built": built, "skipped_no_event": 0, "rows": [{}] * built}


@pytest.fixture()
def patched(monkeypatch):
    def _apply(per_call, batch=500, cap=4000):
        fake = _FakeBuild(per_call)
        monkeypatch.setattr("build_training_rows.build", fake)
        monkeypatch.setattr(config, "BUILD_ROWS_BATCH", batch)
        monkeypatch.setattr(config, "BUILD_ROWS_MAX_PER_CYCLE", cap)
        return fake

    return _apply


def test_stops_when_pending_exhausted(patched):
    """دفعة ناقصة تعني نفاد المعلّق — نتوقّف بلا نداء زائد."""
    fake = patched([500, 120], batch=500, cap=4000)
    stats = run_build_rows.run_cycle(None)
    assert stats == {"built": 620, "skipped_no_event": 0, "batches": 2}
    assert fake.takes == [500, 500]


def test_respects_per_cycle_cap(patched):
    """السقف يقضم الرفع الكبير على دورات بدل قفل القاعدة دفعةً واحدة."""
    fake = patched([500] * 10, batch=500, cap=1500)
    stats = run_build_rows.run_cycle(None)
    assert stats["built"] == 1500
    assert stats["batches"] == 3
    assert fake.takes == [500, 500, 500]


def test_last_batch_is_clipped_to_cap(patched):
    """الدفعة الأخيرة لا تتجاوز ما تبقّى من السقف."""
    fake = patched([500, 500], batch=500, cap=700)
    run_build_rows.run_cycle(None)
    assert fake.takes == [500, 200]


def test_no_pending_is_a_single_cheap_call(patched):
    """لا شيء معلّق ⇒ نداء واحد ثم خروج (الحالة الطبيعيّة كل ساعة)."""
    fake = patched([0], batch=500, cap=4000)
    stats = run_build_rows.run_cycle(None)
    assert stats == {"built": 0, "skipped_no_event": 0, "batches": 1}
    assert fake.takes == [500]


def test_rows_are_not_accumulated(patched):
    """الصفوف تُرمى بعد كل دفعة — العملية تعيش أياماً، والتكديس تسريب."""
    patched([500, 10], batch=500, cap=4000)
    stats = run_build_rows.run_cycle(None)
    assert "rows" not in stats
