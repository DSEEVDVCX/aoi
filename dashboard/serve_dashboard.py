"""نقطة إطلاق اللوحة (تُشغَّل عبر pythonw كمهمّة مجدولة).

نمط api/serve.py: نثبّت مجلّد العمل ومسار الاستيراد، نكتب سجلّ إقلاع (pythonw
يُخفي stderr)، ثم نُشغّل uvicorn على 127.0.0.1:8090.
"""
from __future__ import annotations

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import config  # noqa: E402


def _boot_log(msg: str) -> None:
    try:
        with open(config.BOOT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:  # noqa: BLE001 — سجلّ الإقلاع لا يُسقط الإقلاع
        pass


def main() -> None:
    _boot_log(f"[boot] starting dashboard on {config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    _boot_log(f"[boot] db={config.DB_PATH} exists={os.path.exists(config.DB_PATH)}")
    try:
        import uvicorn

        # log_config=None: تحت pythonw يكون sys.stdout = None، ومنسّق uvicorn
        # الافتراضي يستدعي sys.stdout.isatty() فيتعطّل. تعطيله يتجنّب ذلك
        # (نفس ما يفعله api/serve.py).
        uvicorn.run(
            "app:app",
            host=config.DASHBOARD_HOST,
            port=config.DASHBOARD_PORT,
            log_config=None,
            access_log=False,
        )
    except Exception:
        _boot_log("[boot] FATAL:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
