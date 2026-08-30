"""حراس نظافة مستودع API: المجسّات التاريخية لا تعود إلى جذر الحزمة."""

from pathlib import Path

API_DIR = Path(__file__).resolve().parents[2]


def test_api_probe_scripts_live_only_in_the_archive():
    """`_probe_*.py` تقرأ الاعتماد الحقيقي وقد تندّه upstream؛ تُعزل في probes/."""
    assert list(API_DIR.glob("_probe_*.py")) == []
    archived = sorted((API_DIR / "probes").glob("_probe_*.py"))
    assert len(archived) == 8


def test_archived_database_probes_open_recorder_read_only():
    """حتى الأرشيف لا يفتح قاعدة التشغيل بصلاحية كتابة افتراضية."""
    for name in ("_probe_history.py", "_probe_history2.py", "_probe_overlap.py"):
        text = (API_DIR / "probes" / name).read_text(encoding="utf-8")
        assert "mode=ro" in text, name
        assert "uri=True" in text, name
