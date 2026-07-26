"""نقطة إطلاق الموسِّم للمهمّة المجدولة FomoLabeler (pythonw، stderr مخفيّ).

نفس نمط run_recorder.py: تثبيت مجلّد العمل، سجلّ إقلاع، حلقة لا نهائية تبتلع
أخطاء الدورة الواحدة ولا تموت. عملية منفصلة عن المسجّل عمداً — انظر labeler.py.

الاستخدام:
  python run_labeler.py            # حلقة لا نهائية (المهمّة المجدولة)
  python run_labeler.py 1          # دورة واحدة (تحقّق يدويّ)
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_BOOT_LOG = os.path.join(HERE, "labeler_boot.log")


def _log_boot(msg: str) -> None:
    try:
        with open(_BOOT_LOG, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


def _log(msg: str) -> None:
    import config
    from db import utcnow_iso

    line = f"{utcnow_iso()} {msg}\n"
    try:
        if (
            config.LOG_MAX_BYTES > 0
            and os.path.exists(config.LABEL_LOG_PATH)
            and os.path.getsize(config.LABEL_LOG_PATH) > config.LOG_MAX_BYTES
        ):
            os.replace(config.LABEL_LOG_PATH, config.LABEL_LOG_PATH + ".1")
    except OSError:
        pass
    try:
        with open(config.LABEL_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def main() -> None:
    import config
    from db import RecorderDB, utcnow_iso
    from labeler import label_pending

    cycles = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        cycles = int(sys.argv[1])

    _log_boot(f"boot ok, db={config.DB_PATH}, cycles={cycles}")
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    n = 0
    try:
        while cycles is None or n < cycles:
            try:
                stats = label_pending(db, now_epoch=int(time.time()))
                db.set_meta("labeler_last_run_at", utcnow_iso())
                db.set_meta("labeler_last_stats", str(stats))
                # نسجّل السطر فقط حين يحدث شيء — سطر كل 15 دقيقة إلى الأبد ضجيج.
                if stats["signals"] or stats["watches"]:
                    _log(f"labeled: {stats}")
            except Exception:  # noqa: BLE001 — درع الدورة؛ الحلقة لا تموت
                import traceback

                _log("labeler cycle crashed:\n" + traceback.format_exc())
            n += 1
            if cycles is not None and n >= cycles:
                break
            time.sleep(config.LABEL_INTERVAL_SECONDS)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — أخطاء الإقلاع قبل بدء الحلقة
        import traceback

        _log_boot("BOOT FAILURE:\n" + traceback.format_exc())
        raise
