"""نقطة إطلاق المسجّل للمهمّة المجدولة (pythonw، stderr مخفيّ).

يعكس نمط serve.py في api/: يثبّت مجلّد العمل، يضيف المسارات، ويسجّل أي خطأ إقلاع
إلى ملف حتى لا يضيع تحت pythonw. ثمّ يشغّل الحلقة اللانهائية.

الاستخدام:
  python run_recorder.py            # حلقة لا نهائية (المهمّة المجدولة)
  python run_recorder.py 1          # دورة واحدة (تحقّق حيّ)
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_BOOT_LOG = os.path.join(HERE, "recorder_boot.log")


def _log_boot(msg: str) -> None:
    try:
        with open(_BOOT_LOG, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:  # noqa: BLE001 — سجلّ الإقلاع لا يُسقط الإقلاع
        pass


def main() -> None:
    import asyncio

    import config  # يضيف api/src إلى sys.path عند الاستيراد
    from keep_awake import keep_awake, release

    from recorder import main_loop

    cycles = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        cycles = int(sys.argv[1])

    # قفل الاستيقاظ للحلقة اللانهائية وحدها: تشغيلٌ بعدد دورات محدّد أداةُ تحقّق
    # يدويّة، وليس من حقّها أن تمنع الجهاز من النوم بعد أن تنتهي.
    awake = keep_awake() if cycles is None else False
    _log_boot(f"boot ok, db={config.DB_PATH}, cycles={cycles}, keep_awake={awake}")
    try:
        asyncio.run(main_loop(cycles=cycles))
    finally:
        # القفل يسقط مع العملية أصلاً، لكنّ الإسقاط الصريح يجعل التوقّف النظيف
        # يعيد سلوك النوم فوراً بلا انتظار موت العملية.
        if awake:
            release()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        _log_boot("BOOT FAILURE:\n" + traceback.format_exc())
        raise
