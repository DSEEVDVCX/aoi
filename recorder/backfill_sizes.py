"""Retroactive migration: fill trade-size fields from archived `raw_json`.

Reason for the migration: `extract_signal_event` did not extract
`currentSizeUsd`, `inHumanAmount`, or their siblings, so a $1,000 trade and a
$141,000 trade were completely identical in the database — even though the
median of what fomo calls a "large buy" is only $3,448. The signal's most
valuable discriminating field was being wasted.

No data was lost: the raw event is stored in full in `raw_json`, so we
re-extract from it.

Migration safety properties:
- **Resumable**: selects only rows where `size_usd IS NULL`.
- **No network**: reads the local archive alone.
- **Recorder-safe**: writes derived columns only and never touches
  `raw_json`; even so, it refuses to run while the recorder is writing, to
  avoid a write lock.

Usage:
    Stop-ScheduledTask -TaskName FomoRecorder
    py backfill_sizes.py --dry-run
    py backfill_sizes.py
    Start-ScheduledTask -TaskName FomoRecorder
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import decode_raw  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - depends on the terminal
        pass

_FIELDS = (
    "size_usd", "in_amount", "in_token_address",
    "out_amount", "token_amount", "realized_pnl_usd",
)
_BATCH = 500


def _recorder_is_running() -> bool:
    """True if a recorder process is alive.

    We check the **process**, not the scheduled-task state:
    `Stop-ScheduledTask` flips the state to Ready while the pythonw process
    stays alive for moments (or longer), so the old check passed while the
    recorder was still writing.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -like '*run_recorder.py*' }).ProcessId"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 — check failed — the operator decides
        return False  # cannot check — leave the decision to the operator
    return bool(out.stdout.strip())


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    if not dry_run and _recorder_is_running():
        raise SystemExit(
            "The FomoRecorder task is running right now. Stop it first:\n"
            "  Stop-ScheduledTask -TaskName FomoRecorder"
        )

    # Open through RecorderDB first so column migrations get applied; that
    # keeps the script self-sufficient and does not require running the
    # recorder before it.
    from db import RecorderDB

    RecorderDB(config.DB_PATH, config.SCHEMA_PATH).close()

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(signal_events)")}
    missing = [f for f in _FIELDS if f not in cols]
    if missing:
        raise SystemExit(f"Column migration failed: {', '.join(missing)}")

    total = conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE size_usd IS NULL"
    ).fetchone()[0]
    print(f"Rows without size fields: {total}")
    if not total:
        print("Nothing to migrate.")
        return

    done = filled = 0
    cursor = 0
    while True:
        # Advance by rowid cursor, **not** by the `size_usd IS NULL` condition
        # alone: multi_user_buy events carry no size fields at all, so they
        # stay NULL after processing and the query returns the same rows
        # forever (a genuine infinite loop — it actually happened, on 21 rows).
        rows = conn.execute(
            "SELECT rowid AS rid, id, raw_json FROM signal_events "
            "WHERE rowid > ? AND size_usd IS NULL ORDER BY rowid LIMIT ?",
            (cursor, _BATCH),
        ).fetchall()
        if not rows:
            break
        cursor = rows[-1]["rid"]
        updates = []
        for r in rows:
            try:
                event = decode_raw(r["raw_json"])
            except Exception:  # noqa: BLE001 — a corrupt raw row is skipped, not fatal
                continue  # corrupt raw row — skip it, don't abort the migration
            # Re-extract with the same function the recorder uses: a single
            # source of truth, so migrated rows cannot drift from freshly
            # recorded ones.
            new = extract.extract_signal_event(event, "backfill", None)
            if new is None:
                continue
            vals = [new.get(f) for f in _FIELDS]
            if any(v is not None for v in vals):
                filled += 1
            updates.append((*vals, r["id"]))
        if dry_run:
            done += len(rows)
            print(f"  [preview] {done}/{total} scanned, {filled} have values")
            continue
        conn.executemany(
            f"UPDATE signal_events SET {', '.join(f'{f}=?' for f in _FIELDS)} WHERE id=?",
            updates,
        )
        conn.commit()
        done += len(rows)
        print(f"  {done}/{total} rows ({filled} of them have size values)", flush=True)

    if dry_run:
        print("\nPreview mode — nothing was written.")
    else:
        got = conn.execute(
            "SELECT COUNT(*) FROM signal_events WHERE size_usd IS NOT NULL"
        ).fetchone()[0]
        print(f"\nDone. Rows with size_usd now: {got}")
        row = conn.execute(
            "SELECT MIN(size_usd) lo, MAX(size_usd) hi, COUNT(*) n "
            "FROM signal_events WHERE size_usd IS NOT NULL"
        ).fetchone()
        if row["n"]:
            print(f"Position size range: ${row['lo']:,.0f} .. ${row['hi']:,.0f}")
    conn.close()


if __name__ == "__main__":
    main()
