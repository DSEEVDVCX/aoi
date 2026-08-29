"""اختبارات عدّاد بوابة الصعود + backoff إعادة التقييم."""
import pytest
from db import RecorderDB

import recorder


@pytest.fixture()
def db(tmp_path):
    recorder._RUNUP_STATE = {}          # تصفير الذاكرة بين الاختبارات
    value = RecorderDB(str(tmp_path / "t.db"), "schema.sql")
    yield value
    value.close()
    recorder._RUNUP_STATE = {}


def _bars(db: RecorderDB, token: str, closes: list[float], *,
          end_ts: int = 1_787_600_000) -> None:
    n = len(closes)
    rows = []
    for i, px in enumerate(closes):
        rows.append({
            "token_address": token, "network_id": "1399811149",
            "resolution": "5", "ts": end_ts - (n - i) * 300,
            "o": px, "h": px, "l": px, "c": px, "v": 1.0,
            "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
            "fetched_at": "2026-08-27T00:00:00+00:00",
        })
    db.insert_bars(rows)


T0 = 1_787_600_000


def test_rejected_runup_is_counted_total(db, monkeypatch):
    """الرفض يثبَّت تراكميًا في meta — البوابة الصامتة عمياء (اكتشاف 2026-08-28)."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    _bars(db, "LATE1x", [1.0 + i * 0.3 for i in range(24)])   # log(8)=2.08
    stats = {"runup_rejected": 3}
    recorder._persist_runup_rejection(db, stats)
    assert db.get_meta("runup_rejected_total") == "3"
    stats2 = {"runup_rejected": 2}
    recorder._persist_runup_rejection(db, stats2)
    assert db.get_meta("runup_rejected_total") == "5"          # تراكمي لا استبدال


def test_zero_rejection_writes_nothing(db):
    """دورة بلا رفض لا تلمس meta — لا ضجيج كتابة."""
    recorder._persist_runup_rejection(db, {"runup_rejected": 0})
    assert db.get_meta("runup_rejected_total") is None


def test_runup_backoff_caches_late_verdict(db, monkeypatch):
    """حكم «متأخر» يُخزَّن ولا يُعاد حسابه كل إشارة — backoff مثل بوابة العمر.

    العملة الصاعدة تتلقى 5-10 إشارات يوميًا وكل واحدة كانت تعيد استعلام
    الشموع. الآن: الحكم يُخزَّن `RUNUP_RETRY_SECONDS` ويعاد تقييمه بعدها
    فقط (قد يهدأ الصعود فتصبح الإشارة اللاحقة مبكرة بحق).
    """
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    monkeypatch.setattr(recorder.config, "RUNUP_RETRY_SECONDS", 3600)
    _bars(db, "HOT1x", [1.0 + i * 0.3 for i in range(24)])
    # أول حكم: يحسب ويخزن
    v1 = recorder.runup_verdict(db, "HOT1x", "1399811149", T0)
    assert v1 == recorder.RUNUP_LATE
    assert ("HOT1x", "1399811149") in recorder._RUNUP_STATE
    # إشارة بعد دقيقة: من الذاكرة، بلا إعادة حساب (t0 نفسه ⇒ نفس النتيجة
    # لكن الاستعلام لم يُنفَّذ — نتحقق بتغيير الشموع تحت الأقدام)
    db._conn.execute(
        "DELETE FROM token_bars WHERE token_address='HOT1x'")
    db._commit()
    v2 = recorder.runup_verdict(db, "HOT1x", "1399811149", T0 + 60)
    assert v2 == recorder.RUNUP_LATE            # من الكاش رغم حذف الشموع


def test_runup_backoff_expires(db, monkeypatch):
    """بعد RUNUP_RETRY_SECONDS يُعاد التقييم فعليًا (الصعود قد يهدأ)."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    monkeypatch.setattr(recorder.config, "RUNUP_RETRY_SECONDS", 3600)
    _bars(db, "COOLx", [1.0 + i * 0.3 for i in range(24)])
    assert recorder.runup_verdict(db, "COOLx", "1399811149", T0) == recorder.RUNUP_LATE
    # بعد ساعة+: الشموع حُذفت (لا تاريخ) ⇒ مبكرة بالتعريف = إعادة تقييم حدثت
    db._conn.execute("DELETE FROM token_bars WHERE token_address='COOLx'")
    db._commit()
    v = recorder.runup_verdict(db, "COOLx", "1399811149", T0 + 3700)
    assert v == recorder.RUNUP_OK


def test_ok_verdict_not_cached(db, monkeypatch):
    """حكم «مقبول» لا يُخزَّن: الصعود يتغير سريعًا والصف التالي يستحق قياسًا طازجًا."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    monkeypatch.setattr(recorder.config, "RUNUP_RETRY_SECONDS", 3600)
    _bars(db, "OKAYx", [1.0] * 24)
    assert recorder.runup_verdict(db, "OKAYx", "1399811149", T0) == recorder.RUNUP_OK
    assert ("OKAYx", "1399811149") not in recorder._RUNUP_STATE
