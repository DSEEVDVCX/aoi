"""Recorder launcher for the scheduled task (pythonw, stderr hidden).

Mirrors the serve.py pattern in api/: pins the working directory, sets up
import paths, and logs any boot error to a file so it is not lost under
pythonw. Then runs the infinite loop.

Usage:
  python run_recorder.py            # infinite loop (the scheduled task)
  python run_recorder.py 1          # single cycle (live check)
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
    except Exception:  # noqa: BLE001 — boot logging must not kill booting
        pass


def main() -> None:
    import asyncio

    import config  # adds api/src to sys.path on import
    from keep_awake import keep_awake, release

    from recorder import main_loop

    cycles = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        cycles = int(sys.argv[1])

    # The wake lock is for the infinite loop only: a run with a fixed cycle
    # count is a manual verification tool, and it has no right to keep the
    # machine awake after it finishes.
    awake = keep_awake() if cycles is None else False
    _log_boot(f"boot ok, db={config.DB_PATH}, cycles={cycles}, keep_awake={awake}")
    try:
        asyncio.run(main_loop(cycles=cycles))
    finally:
        # The lock dies with the process anyway, but releasing it explicitly
        # makes a clean stop restore sleep behavior immediately, without
        # waiting for the process to die.
        if awake:
            release()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        _log_boot("BOOT FAILURE:\n" + traceback.format_exc())
        raise
