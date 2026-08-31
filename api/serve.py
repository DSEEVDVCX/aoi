"""The server's launch point, with any boot error logged to a file (pythonw shows no stderr).
It's called from the scheduled task via pythonw.exe with a hidden window.

Important: `log_config=None` is required under pythonw (uvicorn's default
formatter calls `isatty()` on the nonexistent stdout and breaks) — but it
also means **no logging handler at all**, so every application message (token
refresh failures, alert-poll errors, boot warnings) was lost without a
trace. So we install our own rotating file handler here.
"""
import logging
import logging.handlers
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)                       # so .privy_state.json is read by its relative path
sys.path.insert(0, os.path.join(HERE, "src"))

log = open(  # noqa: SIM115 — stays open for the process's whole lifetime (pythonw has no stdout)
    os.path.join(HERE, "server_boot.log"), "a", encoding="utf-8", buffering=1,
)


def _setup_logging() -> None:
    """A rotating file log (5MB × 3) — the only source of visibility under pythonw."""
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
    # uvicorn adds no handlers of its own with log_config=None, so these
    # inherit from the root.
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
