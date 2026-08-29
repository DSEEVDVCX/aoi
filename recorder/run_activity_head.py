"""Scheduled head backfill for global trading activity.

The upstream activity endpoint exposes a current head plus ``lastId`` paging.
This worker periodically walks from the head until it overlaps local history,
without touching the older historical cursor used by ``backfill_activity.py``.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import backfill_activity as activity  # noqa: E402
import config  # noqa: E402
from db import RecorderDB, utcnow_iso  # noqa: E402


def _log(message: str) -> None:
    """Write a bounded diagnostic line without exposing credentials."""
    try:
        with open(config.LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{utcnow_iso()} activity-head: {message}\n")
    except OSError:
        pass


async def main_loop(cycles: int | None = None) -> None:
    client = activity._load_client()
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        count = 0
        while cycles is None or count < cycles:
            started = time.monotonic()
            try:
                stats = await activity.walk_head(
                    client,
                    db,
                    max_pages=getattr(config, "ACTIVITY_HEAD_MAX_PAGES", 3),
                )
                db.note_error("activity_head_last_run_at", utcnow_iso())
                db.note_error("activity_head_last_stats", str(stats))
                if stats["added"]:
                    _log(str(stats))
            except Exception as exc:  # noqa: BLE001 - next cycle retries
                db.note_error(
                    "last_error_activity_head",
                    f"{utcnow_iso()}: {type(exc).__name__}: {exc}"[:400],
                )
            count += 1
            if cycles is not None and count >= cycles:
                break
            remaining = getattr(config, "ACTIVITY_HEAD_INTERVAL_SECONDS", 300) - (
                time.monotonic() - started
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
    finally:
        await client.aclose()
        db.close()


def main() -> None:
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    asyncio.run(main_loop(cycles))


if __name__ == "__main__":
    main()
