"""Training-rows build launcher for the scheduled task FomoBuildRows (pythonw).

The third stage of the pipeline, and the only one that was manual: the
recorder collects every minute, the labeler labels every 15 minutes, and then
`build_training_rows.py` waited for a human hand. The result: labeled outcomes
pile up with no matching training rows, and features freeze at an old
`FEATURE_VERSION` with nobody alerted.

Scheduling is safe for three measured, not assumed, reasons:
  1. Incremental — `pending_outcomes` selects only what lacks the current
     `FEATURE_VERSION`.
  2. Idempotent — `INSERT OR REPLACE` on `(kind, key)`.
  3. A third safe writer — WAL is on and `RecorderDB` sets `timeout=30`
     (db.py) against contention with the recorder and the labeler.

**Never `--rebuild`**: that flag deletes `training_rows` entirely, and a
scheduled delete with no human hand is an unbearable risk. Bumping
`FEATURE_VERSION` alone is enough to rebuild — rows are overwritten in place.

Usage:
  python run_build_rows.py            # infinite loop (the scheduled task)
  python run_build_rows.py 1          # single cycle (manual check)
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
    except Exception:  # noqa: BLE001 — boot logging must not kill booting
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
    except Exception:  # noqa: BLE001 — writing the log must not kill what it logs
        pass


def run_cycle(db) -> dict:
    """One cycle: chew through pending rows batch by batch, up to the cap or until dry.

    Built rows are discarded after each batch (`pop("rows")`): the process
    lives for days, and stacking tens of thousands of dicts in memory is a
    slow leak for no benefit — the write already happened in the database
    inside `build`.
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
            break  # pending rows exhausted
        remaining -= processed
    return stats


def _check_config() -> int:
    """Pre-scheduling check — mirrors `backup_db.py --check-config`.

    Actually opens the database and counts the pending: a configuration error
    is better caught now than by a registered task failing silently every hour
    under pythonw with no window and no output.
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
            print(f"config.{name} is missing", file=sys.stderr)
            return 1
    if not os.path.exists(config.DB_PATH):
        print(f"database not found: {config.DB_PATH}", file=sys.stderr)
        return 1
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        pend = len(pending_outcomes(db, False, 1, False))
    finally:
        db.close()
    print(
        f"ok · feature_version={features.FEATURE_VERSION} "
        f"· every {config.BUILD_ROWS_INTERVAL_SECONDS}s "
        f"· cap {config.BUILD_ROWS_MAX_PER_CYCLE}/cycle "
        f"· pending now: {'yes' if pend else 'no'}"
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
                # Log only when something was built — a line every hour
                # forever is noise.
                if stats["built"] or stats["skipped_no_event"]:
                    _log(f"built: {stats}")
            except Exception:  # noqa: BLE001 — the cycle's shield; the loop must not die
                import traceback

                _log("build_rows cycle crashed:\n" + traceback.format_exc())
                # And rescue the connection before the next cycle: a read
                # snapshot left behind in WAL rejects every write from this
                # connection with `database is locked` **no matter the
                # timeout**, and this loop's period is an hour — a permanent
                # fault here costs an hour per dropped cycle. Details in
                # `db.recover_connection`.
                try:
                    _log(f"connection recovery: {db.recover_connection()}")
                except Exception as rec_exc:  # noqa: BLE001 — the rescue hand must not kill the loop
                    _log(f"connection recovery failed: {type(rec_exc).__name__}")
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
