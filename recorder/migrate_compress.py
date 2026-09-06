"""One-time migration: compress existing raw_json columns (text → zlib BLOB).

Reason for the migration: the recorder used to write raw data as text, and
`recorder.db` reached ~720 MB within 20 hours (the `snapshots` table alone
577 MB), growing at ~1.3 GB per day. Lossless compression (~4.7x ratio)
brings that down to ~280 MB per day without losing a single byte of the
archive.

Migration safety properties:
- **Lossless and verified**: every row is decompressed and compared with the
  original before writing; any mismatch aborts the whole migration.
- **Resumable (idempotent)**: selects only rows with `typeof(raw_json)='text'`,
  so a restart after an interruption picks up where it stopped and never
  touches already-compressed rows.
- **Recorder-safe**: refuses to run while the `FomoRecorder` scheduled task
  is running — the writer must be stopped.

Usage:
    Stop-ScheduledTask -TaskName FomoRecorder
    py migrate_compress.py            # migrate + VACUUM
    py migrate_compress.py --dry-run  # estimate the gain only, no writes
    Start-ScheduledTask -TaskName FomoRecorder
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from db import decode_raw, encode_raw  # noqa: E402

# Tables that carry raw_json, and each one's key for precise updates.
_TARGETS = (
    ("snapshots", "id"),
    ("market_ticks", "rowid"),
    ("signal_events", "rowid"),
    ("token_static", "rowid"),
)
_BATCH = 200   # rows per transaction — bounds process memory on large snapshots
_SAMPLE = 50   # sample rows in preview mode

# A Windows console may be cp1256, unable to render Arabic or arrows — force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - depends on the terminal
        pass


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


def _pending_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE typeof(raw_json)='text'"
    ).fetchone()[0]


def estimate_table(conn: sqlite3.Connection, table: str, pending: int) -> tuple[int, int]:
    """Preview: measures a sample and extrapolates to the whole table. Returns (bytes before, bytes after)."""
    sample = conn.execute(
        f"SELECT raw_json FROM {table} WHERE typeof(raw_json)='text' "
        f"LIMIT {_SAMPLE}"
    ).fetchall()
    if not sample:
        return 0, 0
    before = sum(len(t.encode("utf-8")) for (t,) in sample)
    after = sum(len(encode_raw(t)) for (t,) in sample)
    scale = pending / len(sample)
    return int(before * scale), int(after * scale)


def migrate_table(conn: sqlite3.Connection, table: str, key: str) -> tuple[int, int, int]:
    """Compress the rows of one table. Returns (row count, bytes before, bytes after)."""
    rows_done = before = after = 0
    while True:
        rows = conn.execute(
            f"SELECT {key} AS k, raw_json FROM {table} "
            f"WHERE typeof(raw_json)='text' LIMIT {_BATCH}"
        ).fetchall()
        if not rows:
            break
        updates = []
        for k, text in rows:
            blob = encode_raw(text)
            # Lossless verification: write only if the original comes back verbatim.
            if decode_raw(blob) != decode_raw(text):
                raise SystemExit(
                    f"Migration aborted: mismatch after compression in {table} {key}={k}. "
                    "This batch was not written."
                )
            before += len(text.encode("utf-8"))
            after += len(blob)
            updates.append((blob, k))
        conn.executemany(f"UPDATE {table} SET raw_json=? WHERE {key}=?", updates)
        conn.commit()
        rows_done += len(updates)
        print(f"  {table}: {rows_done} rows ({before/1e6:.0f} -> {after/1e6:.0f} MB)", flush=True)
    return rows_done, before, after


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    db_path = config.DB_PATH
    if not os.path.isfile(db_path):
        raise SystemExit(f"No database at {db_path}")

    if not dry_run and _recorder_is_running():
        raise SystemExit(
            "The FomoRecorder task is running right now. Stop it first:\n"
            "  Stop-ScheduledTask -TaskName FomoRecorder"
        )

    size_before = os.path.getsize(db_path)
    print(f"Database: {db_path} ({size_before/1e6:.0f} MB)")
    print("Preview mode (no writes)\n" if dry_run else "")

    conn = sqlite3.connect(db_path)
    t0 = time.perf_counter()
    total_rows = total_before = total_after = 0
    try:
        for table, key in _TARGETS:
            pending = _pending_count(conn, table)
            if pending == 0:
                print(f"{table}: already compressed - skipping")
                continue
            print(f"{table}: {pending} uncompressed rows")
            if dry_run:
                before, after = estimate_table(conn, table, pending)
                rows = pending
            else:
                rows, before, after = migrate_table(conn, table, key)
            total_rows += rows
            total_before += before
            total_after += after

        if total_before:
            label = "estimate" if dry_run else "raw"
            print(
                f"\n{label}: {total_before/1e6:.0f} MB -> {total_after/1e6:.0f} MB "
                f"({total_before/max(total_after,1):.1f}x) across {total_rows} rows "
                f"in {time.perf_counter()-t0:.0f}s"
            )
        else:
            print("\nNothing to migrate.")

        if not dry_run and total_rows:
            print("VACUUM to reclaim space (may take minutes)...", flush=True)
            conn.execute("VACUUM")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('raw_encoding','zlib') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            conn.commit()
    finally:
        conn.close()

    if not dry_run:
        size_after = os.path.getsize(db_path)
        print(
            f"File size: {size_before/1e6:.0f} MB -> {size_after/1e6:.0f} MB "
            f"(saved {(size_before-size_after)/1e6:.0f} MB)"
        )


if __name__ == "__main__":
    main()
