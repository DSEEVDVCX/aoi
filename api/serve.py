"""نقطة تشغيل الخادم مع تسجيل أي خطأ إقلاع إلى ملف (pythonw لا يُظهر stderr).
تُستدعى من المهمّة المجدولة عبر pythonw.exe بنافذة مخفية.

مهمّ: `log_config=None` ضروريّ تحت pythonw (منسّق uvicorn الافتراضي يستدعي
`isatty()` على stdout المعدوم فيتعطّل) — لكنّه يعني أيضاً **ألّا مُعالج تسجيل
إطلاقاً**، فكانت كل رسائل التطبيق (فشل تجديد التوكن، أخطاء استطلاع التنبيهات،
تحذيرات الإقلاع) تضيع بلا أثر. لذا نركّب هنا مُعالج ملفّ مُدوَّر بأنفسنا.
"""
import logging
import logging.handlers
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)                       # لضمان قراءة .privy_state.json بمسار نسبي
sys.path.insert(0, os.path.join(HERE, "src"))

log = open(  # noqa: SIM115 — يبقى مفتوحاً عمر العملية كلها (pythonw بلا stdout)
    os.path.join(HERE, "server_boot.log"), "a", encoding="utf-8", buffering=1,
)


def _setup_logging() -> None:
    """سجلّ ملفّ مُدوَّر (5MB × 3) — المصدر الوحيد للرؤية تحت pythonw."""
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(HERE, "server.log"), maxBytes=5 * 1024 * 1024,
        backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # uvicorn لا يضيف مُعالجاته مع log_config=None، فتَرِث هذه من الجذر.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).propagate = True


try:
    import uvicorn
    _setup_logging()
    log.write("boot: starting uvicorn\n")
    logging.getLogger("fomo_api.serve").info("server starting on 127.0.0.1:8080")
    uvicorn.run("fomo_api.main:app", host="127.0.0.1", port=8080, log_config=None)
except Exception:
    log.write("BOOT ERROR:\n" + traceback.format_exc() + "\n")
    logging.getLogger("fomo_api.serve").exception("boot failed")
    raise
