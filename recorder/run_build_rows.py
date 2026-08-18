"""نقطة إطلاق بناء صفوف التدريب للمهمّة المجدولة FomoBuildRows (pythonw).

المرحلة الثالثة من الأنبوب، وكانت الوحيدة اليدويّة: المسجّل يجمع كل دقيقة،
والموسِّم يعنون كل 15 دقيقة، ثم كان `build_training_rows.py` ينتظر يداً بشريّة.
النتيجة أنّ النتائج الموسومة تتراكم بلا صفوف تدريب مقابلة، فتتجمّد الميزات عند
`FEATURE_VERSION` قديمة بلا أن يُنبّه أحد.

الجدولة آمنة لثلاثة أسباب مقيسة لا مفترضة:
  1. تزايديّة — `pending_outcomes` يختار ما ينقصه `FEATURE_VERSION` الحالي فقط.
  2. عديمة الأثر عند التكرار — `INSERT OR REPLACE` على `(kind, key)`.
  3. كاتب ثالث آمن — WAL نشط و`RecorderDB` يضبط `timeout=30` (db.py) تحسّباً
     لازدحام المسجّل والموسِّم.

**بلا `--rebuild` إطلاقاً**: تلك الراية تحذف `training_rows` كاملاً، وحذفٌ مجدول
بلا يد بشرية خطر لا يُحتمل. رفع `FEATURE_VERSION` وحده يكفي لإعادة البناء —
الصفوف تُكتب فوقها في مكانها.

الاستخدام:
  python run_build_rows.py            # حلقة لا نهائية (المهمّة المجدولة)
  python run_build_rows.py 1          # دورة واحدة (تحقّق يدويّ)
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_BOOT_LOG = os.path.join(HERE, "build_rows_boot.log")


def _log_boot(msg: str) -> None:
    try:
        with open(_BOOT_LOG, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:  # noqa: BLE001 — سجلّ الإقلاع لا يُسقط الإقلاع
        pass


def _log(msg: str) -> None:
    import config
    from db import utcnow_iso

    line = f"{utcnow_iso()} {msg}\n"
    try:
        if (
            config.LOG_MAX_BYTES > 0
            and os.path.exists(config.BUILD_ROWS_LOG_PATH)
            and os.path.getsize(config.BUILD_ROWS_LOG_PATH) > config.LOG_MAX_BYTES
        ):
            os.replace(config.BUILD_ROWS_LOG_PATH, config.BUILD_ROWS_LOG_PATH + ".1")
    except OSError:
        pass
    try:
        with open(config.BUILD_ROWS_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001 — الكتابةُ في السجلّ لا تُسقط ما تُسجّله
        pass


def run_cycle(db) -> dict:
    """دورة واحدة: قضم الصفوف المعلّقة دفعةً دفعةً حتى السقف أو النفاد.

    الصفوف المبنيّة تُرمى بعد كل دفعة (`pop("rows")`): العملية تعيش أياماً،
    وتكديس عشرات الآلاف من القواميس في الذاكرة تسريب بطيء بلا فائدة — الكتابة
    تمّت في القاعدة داخل `build`.
    """
    import config
    from build_training_rows import build

    stats = {"built": 0, "skipped_no_event": 0, "batches": 0}
    remaining = config.BUILD_ROWS_MAX_PER_CYCLE
    while remaining > 0:
        take = min(config.BUILD_ROWS_BATCH, remaining)
        part = build(db, False, take, False, False)
        part.pop("rows", None)
        processed = part["built"] + part["skipped_no_event"]
        stats["built"] += part["built"]
        stats["skipped_no_event"] += part["skipped_no_event"]
        stats["batches"] += 1
        if processed < take or processed == 0:
            break  # نفدت الصفوف المعلّقة
        remaining -= processed
    return stats


def _check_config() -> int:
    """تحقّق قبل التسجيل في جدولة المهامّ — يطابق `backup_db.py --check-config`.

    يفتح القاعدة فعلاً ويعدّ المعلّق: خطأ إعداد يُكتشف الآن أفضل من مهمّة مسجّلة
    تفشل صامتةً كل ساعة تحت pythonw بلا نافذة ولا مخرَج.
    """
    import config
    import features
    from build_training_rows import pending_outcomes
    from db import RecorderDB

    for name in (
        "BUILD_ROWS_INTERVAL_SECONDS",
        "BUILD_ROWS_BATCH",
        "BUILD_ROWS_MAX_PER_CYCLE",
        "BUILD_ROWS_LOG_PATH",
    ):
        if not hasattr(config, name):
            print(f"config.{name} مفقود", file=sys.stderr)
            return 1
    if not os.path.exists(config.DB_PATH):
        print(f"قاعدة البيانات غير موجودة: {config.DB_PATH}", file=sys.stderr)
        return 1
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        pend = len(pending_outcomes(db, False, 1, False))
    finally:
        db.close()
    print(
        f"ok · feature_version={features.FEATURE_VERSION} "
        f"· كل {config.BUILD_ROWS_INTERVAL_SECONDS}ث "
        f"· سقف {config.BUILD_ROWS_MAX_PER_CYCLE}/دورة "
        f"· معلّق الآن: {'نعم' if pend else 'لا'}"
    )
    return 0


def main() -> None:
    import config
    from db import RecorderDB, utcnow_iso

    cycles = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        cycles = int(sys.argv[1])

    _log_boot(f"boot ok, db={config.DB_PATH}, cycles={cycles}")
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    n = 0
    try:
        while cycles is None or n < cycles:
            try:
                started = time.time()
                stats = run_cycle(db)
                stats["seconds"] = round(time.time() - started, 1)
                db.set_meta("build_rows_last_run_at", utcnow_iso())
                db.set_meta("build_rows_last_stats", str(stats))
                # نسجّل فقط حين يُبنى شيء — سطر كل ساعة إلى الأبد ضجيج.
                if stats["built"] or stats["skipped_no_event"]:
                    _log(f"built: {stats}")
            except Exception:  # noqa: BLE001 — درع الدورة؛ الحلقة لا تموت
                import traceback

                _log("build_rows cycle crashed:\n" + traceback.format_exc())
            n += 1
            if cycles is not None and n >= cycles:
                break
            time.sleep(config.BUILD_ROWS_INTERVAL_SECONDS)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        if "--check-config" in sys.argv:
            raise SystemExit(_check_config())
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback

        _log_boot("BOOT FAILURE:\n" + traceback.format_exc())
        raise
