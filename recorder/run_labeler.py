"""Labeler launcher for the scheduled task FomoLabeler (pythonw, stderr hidden).

Same pattern as run_recorder.py: pin the working directory, a boot log, an
infinite loop that swallows single-cycle errors and never dies. A separate
process from the recorder on purpose — see labeler.py.

Usage:
  python run_labeler.py            # infinite loop (the scheduled task)
  python run_labeler.py 1          # single cycle (manual check)
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
    except Exception:  # noqa: BLE001 — boot logging must not kill booting
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
    except Exception:  # noqa: BLE001 — writing the log must not kill what it logs
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
                # Log a line only when something happened — a line every 15
                # minutes forever is noise.
                if stats["signals"] or stats["watches"]:
                    _log(f"labeled: {stats}")
            except Exception:  # noqa: BLE001 — the cycle's shield; the loop must not die
                import traceback

                _log("labeler cycle crashed:\n" + traceback.format_exc())
                # And rescue the connection before the next cycle: a read
                # snapshot left behind in WAL rejects every write from this
                # connection with `database is locked` **no matter the
                # timeout**, so every later cycle crashes until a manual
                # restart (happened in a 382-cycle streak, 2026-08-22/23).
                # Details in `db.recover_connection`.
                try:
                    _log(f"connection recovery: {db.recover_connection()}")
                except Exception as rec_exc:  # noqa: BLE001 — the rescue hand must not kill the loop
                    _log(f"connection recovery failed: {type(rec_exc).__name__}")
            n += 1
            if cycles is not None and n >= cycles:
                break
            time.sleep(config.LABEL_INTERVAL_SECONDS)
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        _log_boot("BOOT FAILURE:\n" + traceback.format_exc())
        raise
