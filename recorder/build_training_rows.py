"""بناء جدول التدريب من النتائج الموسومة (المرحلة 2) — بلا شبكة.

لكل صفّ في `outcomes` يبني `features.build_training_row` صفّاً كامل الميزات
مقيَّداً بـ t=0، ويكتبه في `training_rows`. تزايديّ: الصفوف المبنية تُتخطّى إلّا
مع `--rebuild`.

الفلترة تتبع PLAN §2.1: `asset_class='meme'` إلزاميّ للتدريب (الأرشيف يحوي BTC
وأسهماً مرمّزة)، لكنّ السكربت يبني **كل** الأصناف ويكتب `asset_class` في الصفّ —
الفصل عند الاستعلام لا عند الجمع، فلا نخسر إمكانية تحليل الأصناف الأخرى.

الاستعمال:
    py build_training_rows.py --dry-run          # تقرير: كم صفّاً وأيّ ميزات فارغة
    py build_training_rows.py                    # بناء تزايديّ
    py build_training_rows.py --model-candidates-only  # الإشارات الحيّة المستقلّة فقط
    py build_training_rows.py --rebuild          # إعادة بناء الكل
    py build_training_rows.py --limit 200        # دفعة محدودة
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import features  # noqa: E402
from db import RecorderDB  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass


def _arg_int(name: str, default: int) -> int:
    if name in sys.argv:
        try:
            return int(sys.argv[sys.argv.index(name) + 1])
        except (IndexError, ValueError):
            pass
    return default


def pending_outcomes(
    db: RecorderDB,
    rebuild: bool,
    limit: int,
    model_candidates_only: bool = False,
) -> list[dict]:
    candidate_filter = ""
    if model_candidates_only:
        candidate_filter = """
        AND o.kind = 'signal'
        AND o.is_independent = 1
        AND o.entry_ts >= ?"""
    where = """
        AND NOT EXISTS (SELECT 1 FROM training_rows r
                         WHERE r.kind = o.kind AND r.key = o.key
                           AND r.feature_version = ?)"""
    params: tuple[int, ...]
    if model_candidates_only:
        params = (config.LIVE_START_TS, features.FEATURE_VERSION, limit)
    else:
        params = (features.FEATURE_VERSION, limit)
    rows = db._conn.execute(
        f"""SELECT o.* FROM outcomes o
             WHERE o.status IN ('ok', 'no_bars')
             {candidate_filter} {where}
             ORDER BY o.entry_ts LIMIT ?""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def build(
    db: RecorderDB,
    rebuild: bool,
    limit: int,
    dry: bool,
    model_candidates_only: bool = False,
) -> dict:
    if rebuild and model_candidates_only:
        raise ValueError("--rebuild and --model-candidates-only cannot be combined")
    if rebuild:
        # Rebuild is a one-time reset. Subsequent invocations can resume by
        # selecting rows missing the current feature version.
        db._conn.execute("DELETE FROM training_rows")
        db._conn.commit()
    stats = {"built": 0, "skipped_no_event": 0}
    rows: list[dict] = []
    for out in pending_outcomes(db, rebuild, limit, model_candidates_only):
        row = features.build_training_row(db, out)
        if row is None:
            stats["skipped_no_event"] += 1
            continue
        rows.append(row)
        stats["built"] += 1
    if rows and not dry:
        cols = features.ROW_COLUMNS
        with db.batch():
            db._conn.executemany(
                f"INSERT OR REPLACE INTO training_rows({', '.join(cols)}) "
                f"VALUES({', '.join(f':{c}' for c in cols)})",
                [{c: r.get(c) for c in cols} for r in rows],
            )
    stats["rows"] = rows
    return stats


def coverage_report(rows: list[dict]) -> None:
    """نسبة الحضور لكل ميزة — الفراغ الكامل يكشف عائلة معطوبة أو غير مغطّاة."""
    if not rows:
        return
    n = len(rows)
    filled = {
        c: sum(1 for r in rows if r.get(c) is not None) for c in features.FEATURE_COLUMNS
    }
    print(f"\nتغطية الميزات على {n} صفّاً (المرتّبة تصاعدياً):")
    for c, k in sorted(filled.items(), key=lambda kv: kv[1]):
        pct = 100 * k / n
        mark = "  ← فارغة تماماً" if k == 0 else ("  ← تغطية ضعيفة" if pct < 25 else "")
        print(f"  {c:<28} {pct:5.1f}%{mark}")


def main() -> None:
    dry = "--dry-run" in sys.argv
    rebuild = "--rebuild" in sys.argv
    model_candidates_only = "--model-candidates-only" in sys.argv
    limit = _arg_int("--limit", 10**9)
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        stats = build(db, rebuild, limit, dry, model_candidates_only)
        rows = stats.pop("rows")
        print(f"صفوف مبنيّة: {stats['built']} · بلا حدث مصدر: {stats['skipped_no_event']}")
        if rows:
            classes: dict[str, int] = {}
            for r in rows:
                cls = r.get("asset_class") or "unknown"
                classes[cls] = classes.get(cls, 0) + 1
            print("التوزيع بالصنف:", dict(sorted(classes.items(), key=lambda kv: -kv[1])))
            coverage_report(rows)
        if dry:
            print("\n(dry-run — بلا كتابة)")
        else:
            total = db._conn.execute("SELECT COUNT(*) FROM training_rows").fetchone()[0]
            print(f"\nإجمالي training_rows: {total}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
