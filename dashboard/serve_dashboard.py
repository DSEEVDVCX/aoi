"""Dashboard launcher (run via pythonw as a scheduled task).

Following api/serve.py's pattern: pin the working directory and import path,
write a boot log (pythonw hides stderr), then run uvicorn on 127.0.0.1:8090.
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
    except Exception:  # noqa: BLE001 — the boot log must not crash the boot
        pass


def main() -> None:
    _boot_log(f"[boot] starting dashboard on {config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    _boot_log(f"[boot] db={config.DB_PATH} exists={os.path.exists(config.DB_PATH)}")
    try:
        import uvicorn

        # log_config=None: under pythonw sys.stdout is None, and uvicorn's
        # default formatter calls sys.stdout.isatty() and breaks. Disabling it
        # avoids that (same as api/serve.py does).
        server_config = uvicorn.Config(
            "app:app",
            host=config.DASHBOARD_HOST,
            port=config.DASHBOARD_PORT,
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(server_config)

        # Warm the cache before listening: the first visitor doesn't pay the
        # cold-compute cost (3.4s for networks, measured). The thread is
        # daemonic and its failure never crashes the boot — requests that beat
        # it just get the cold computation as before.
        import warmup

        warmup.start_warmup_thread()
        _boot_log("[boot] warmup thread started")

        server.run()
    except Exception:
        _boot_log("[boot] FATAL:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
