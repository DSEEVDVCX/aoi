"""تعبئة الأعمدة المستخرَجة حديثاً من الخام المحفوظ (بلا شبكة).

الحقول التالية كانت **موجودة في `raw_json` منذ اليوم الأول** ولم يكن المستخرج
يقرأها، فضاعت من التحليل وحده لا من الأرشيف:

- `signal_events`: likes · views · num_replies · pinned
  (تغطية مقيسة: likes/views في 100% من الأحداث؛ numReplies نادر ⇒ يبقى NULL)
- `token_static`: exchanges_count/json · cmc_id · description(+len) ·
  has_banner · has_image
- `token_social`: holder_authors — **تصحيح** لا إضافة: كان يقرأ `equity` وهو
  صفر في 100% من الأطروحات، والمركز الحقيقي في `authorTrade`

لأن الخام محفوظ ومضغوط (zlib BLOB) نعيد الاشتقاق محلياً: **صفر نداء** على
fomo، ولا خطر حدّ معدّل، والنتيجة مطابقة تماماً لما سيسجّله المسجّل الحيّ لأن
السكربت يستدعي دوالّ `extract` نفسها — مرجع واحد فلا ينجرف تعريفان.

القيم الغائبة من المصدر تبقى NULL ولا تُفبرك صفراً (FR-007)، والخام لا يُلمس.

الاستعمال:
    py backfill_extracted_fields.py --dry-run    # تقرير بلا كتابة
    py backfill_extracted_fields.py              # التعبئة
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import RecorderDB, decode_raw  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

BATCH = 5000

# العمود → مفتاح المخرَج من `extract`. الاسمان متطابقان هنا، لكنّ الصراحة
# تمنع تعبئة عمود بمفتاح مشابه الاسم مختلف المعنى عند أي إعادة تسمية لاحقة.
SIGNAL_FIELDS = ("likes", "views", "num_replies", "pinned", "out_token_address")
STATIC_FIELDS = (
    "exchanges_count", "exchanges_json", "cmc_id",
    "description", "description_len", "has_banner", "has_image",
    # عمودان قديمان لا جديدان: كانا NULL في 100% من الصفوف لأن المحوِّل
    # السابق توقّع قيمة منطقية والمصدر يعطي **عنوان** سلطة. الخام يحمل
    # المفتاح في 40/40 لقطة، فالتصحيح رجعيّ بلا شبكة.
    # ملاحظة تشغيلية: صفوف EVM تبقى NULL بحقّ (غير مقيسة — FR-007)، فتطابق
    # شرط `IS NULL` في كل تشغيل لاحق. التكرار غير ضارّ (نفس الخام ⇒ نفس
    # القيمة) لكنه يعني أن هذا السكربت لا يصل أبداً إلى "صفر صفّاً ناقصاً".
    "mintable", "freezable",
)
# ليس حقلاً «جديداً» بل **مصحَّحاً**: كان مشتقّاً من `equity` وهو صفر في
# 28,186/28,186 أطروحة مقيسة، فصار العمود ثابتاً على 0 في 46,040 صفّاً. المصدر
# الصحيح `authorTrade.humanTokenAmount`، والخام يحمله في كل لقطة ⇒ تصحيح رجعيّ
# بلا شبكة. لا نلمس بقيّة أعمدة اللقطة (المجاميع الأخرى صحيحة أصلاً).
SOCIAL_FIELDS = ("holder_authors",)


def _same(a, b) -> bool:
    """هل القيمة المشتقّة تطابق المخزَّنة؟ (يمنع كتابة لا تغيّر شيئاً)."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= 1e-9
        except (TypeError, ValueError):
            return False
    return a == b


def _backfill(
    db: RecorderDB,
    table: str,
    key_cols: tuple[str, ...],
    fields: tuple[str, ...],
    extract_row,
    dry: bool,
    where_sql: str | None = None,
) -> tuple[int, int, int]:
    """يعيد (مفحوص، محدَّث، متعذّر). يمرّ على الصفوف المرشَّحة فقط.

    الشرط الافتراضي `أيّ عمود جديد IS NULL` يجعل السكربت قابلاً لإعادة التشغيل:
    الصفوف المعبّأة تُتخطّى، والمقاطَعة في المنتصف لا تفسد شيئاً. و`where_sql`
    يتجاوزه حين لا يكون الفراغ NULL — عمود ثابت على **صفر** خاطئ يبدو معبّأً
    لكنّه ليس كذلك، فلا سبيل لالتقاطه بـ`IS NULL`.

    بعد الاشتقاق نقارن بالمخزَّن ونتخطّى المتطابق: الصفوف التي لا يملك المصدر
    عنها شيئاً (EVM في mintable مثلاً) تطابق الشرط في كل تشغيل، فبلا المقارنة
    يعيد السكربت كتابة عشرات الآلاف من الصفوف بنفس قيمها إلى الأبد.
    """
    missing = where_sql or " OR ".join(f"{f} IS NULL" for f in fields)
    keys = ", ".join(key_cols)
    cols = ", ".join(fields)
    total = db._conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {missing}"
    ).fetchone()[0]
    print(f"{table}: {total} صفّاً مرشَّحاً")

    # القراءة تُستنفَد **قبل** أي كتابة: المسجّل الحيّ يكتب كل دقيقة، وترك
    # مؤشّر قراءة مفتوحاً أثناء الكتابة يطيل نافذة القفل بلا داعٍ.
    scanned = failed = 0
    pending: list[tuple] = []
    cur = db._conn.execute(
        f"SELECT {keys}, {cols}, raw_json FROM {table} WHERE {missing}"
    )
    while True:
        chunk = cur.fetchmany(BATCH)
        if not chunk:
            break
        for r in chunk:
            scanned += 1
            try:
                row = extract_row(decode_raw(r["raw_json"]))
                if row is None:
                    failed += 1
                    continue
            except Exception as exc:  # noqa: BLE001 — صفّ تالف لا يوقف الباقي
                failed += 1
                if failed <= 3:
                    print(f"  تعذّر: {type(exc).__name__}: {exc}")
                continue
            vals = tuple(row.get(f) for f in fields)
            if all(v is None for v in vals):
                continue  # المصدر صامت فعلاً — لا كتابة ولا فبركة
            if all(_same(v, r[f]) for v, f in zip(vals, fields, strict=True)):
                continue  # مطابق للمخزَّن — كتابة بلا أثر
            pending.append(vals + tuple(r[c] for c in key_cols))

    if dry or not pending:
        return scanned, len(pending), failed

    sets = ", ".join(f"{f}=?" for f in fields)
    where = " AND ".join(f"{c}=?" for c in key_cols)
    sql = f"UPDATE {table} SET {sets} WHERE {where}"
    for start in range(0, len(pending), BATCH):
        _write_with_retry(db, sql, pending[start:start + BATCH])
    return scanned, len(pending), failed


def _write_with_retry(db: RecorderDB, sql: str, rows: list[tuple], tries: int = 6) -> None:
    """كتابة دفعة مع انتظار متزايد عند القفل.

    المسجّل والموسِّم يكتبان الآن؛ WAL يسلسل الكتابات لكنّ دفعة كبيرة قد تتجاوز
    مهلة SQLite. الفشل هنا يعني ضياع الدفعة، والسكربت قابل لإعادة التشغيل، لكنّ
    الانتظار أرخص من إعادة اشتقاق كل شيء.
    """
    for attempt in range(tries):
        try:
            with db.batch():
                db._conn.executemany(sql, rows)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == tries - 1:
                raise
            wait = 2 ** attempt
            print(f"  القاعدة مقفلة — إعادة المحاولة بعد {wait}ث")
            time.sleep(wait)


def main() -> None:
    dry = "--dry-run" in sys.argv
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        s1 = _backfill(
            db, "signal_events", ("id",), SIGNAL_FIELDS,
            lambda raw: extract.extract_signal_event(raw, "1970-01-01T00:00:00+00:00"),
            dry,
        )
        print(f"  مفحوص {s1[0]} · محدَّث {s1[1]} · متعذّر {s1[2]}")

        s2 = _backfill(
            db, "token_static", ("token_address", "network_id"), STATIC_FIELDS,
            lambda raw: extract.extract_token_static(raw, "1970-01-01T00:00:00+00:00"),
            dry,
        )
        print(f"  مفحوص {s2[0]} · محدَّث {s2[1]} · متعذّر {s2[2]}")

        # `holder_authors` كان يقرأ `equity` وهو صفر في 100% من الأطروحات، فبقي
        # العمود ثابتاً على 0 — فراغ **لا يظهر** لـ`IS NULL`، فنمرّ على الكلّ
        # ونتّكل على مقارنة القيمة لتخطّي ما لا يتغيّر.
        s3 = _backfill(
            db, "token_social", ("token_address", "network_id", "recorded_at"),
            SOCIAL_FIELDS,
            lambda raw: extract.extract_social(raw, "", "", ""),
            dry, where_sql="1",
        )
        print(f"  مفحوص {s3[0]} · محدَّث {s3[1]} · متعذّر {s3[2]}")

        if dry:
            print("(dry-run — بلا كتابة)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
