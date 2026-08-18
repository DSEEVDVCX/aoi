"""اختبارات قفل الاستيقاظ — منطق القرار لا نداء ويندوز نفسه."""
import ctypes
import sys

import keep_awake
import pytest


def _stub(monkeypatch, ret=1, exc=None):
    """يستبدل kernel32 ويعيد قائمة الرايات المرصودة.

    `keep_awake` يستورد ctypes **داخل** الدالّة، فالتصحيح على وحدة ctypes نفسها
    هو ما يُرى — لا على وحدة keep_awake.
    """
    seen: list[int] = []

    class _K32:
        @staticmethod
        def SetThreadExecutionState(flags):
            seen.append(flags)
            if exc is not None:
                raise exc
            return ret

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"kernel32": _K32})(),
                        raising=False)
    return seen


def test_flags_match_winbase():
    """الرايات مثبَّتة بقيمها من winbase.h — خطأ فيها يعني قفلاً صامتاً."""
    assert keep_awake._ES_CONTINUOUS == 0x80000000
    assert keep_awake._ES_SYSTEM_REQUIRED == 0x00000001


def test_noop_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert keep_awake.keep_awake() is False
    assert keep_awake.release() is False


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="ويندوز فقط")
def test_requests_continuous_system_required(monkeypatch):
    """يطلب الحالتين معاً: بلا CONTINUOUS تصير نبضة تنتهي فوراً."""
    seen = _stub(monkeypatch)
    assert keep_awake.keep_awake() is True
    assert seen == [0x80000000 | 0x00000001]


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="ويندوز فقط")
def test_release_clears_only_continuous(monkeypatch):
    seen = _stub(monkeypatch)
    assert keep_awake.release() is True
    assert seen == [0x80000000]   # بلا SYSTEM_REQUIRED ⇒ إسقاط الطلب


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="ويندوز فقط")
def test_failure_is_not_fatal(monkeypatch):
    """تعذّر القفل لا يُسقط الجمع — تحسين لا شرط تشغيل."""
    _stub(monkeypatch, exc=OSError("denied"))
    assert keep_awake.keep_awake() is False
    assert keep_awake.release() is False


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="ويندوز فقط")
def test_zero_return_reported_as_failure(monkeypatch):
    """صفر من الـAPI يعني رفضاً، لا نبلّغ نجاحاً كاذباً."""
    _stub(monkeypatch, ret=0)
    assert keep_awake.keep_awake() is False
