"""Create a consistent, verified SQLite backup while the recorder stays live.

SQLite ``VACUUM INTO`` reads a fixed snapshot even while WAL writes continue.
Backups go outside the repository (AOI_BACKUP_DIR, or OneDrive/aoi-backups), are
published atomically after verification, and old snapshots are pruned only from
that dedicated directory.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import config

BACKUP_PREFIX = "recorder-"
BACKUP_SUFFIX = ".db"
DEFAULT_KEEP = 3
MIN_FREE_MULTIPLIER = 1.10


def default_backup_dir() -> Path:
    configured = os.environ.get("AOI_BACKUP_DIR")
    if configured:
        return Path(configured).expanduser()
    onedrive = os.environ.get("OneDrive")  # noqa: SIM112 — this is its exact casing on Windows
    if onedrive:
        return Path(onedrive) / "aoi-backups"
    raise RuntimeError(
        "No external backup directory configured. Set AOI_BACKUP_DIR to an "
        "external or synchronized directory."
    )


def validate_destination(destination: Path) -> Path:
    resolved = destination.expanduser().resolve()
    workspace = Path(config.ROOT).resolve()
    if resolved == workspace or workspace in resolved.parents:
        raise ValueError("backup destination must be outside the project workspace")
    return resolved


def _backup_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S-%f")
    return f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"


def _prune(destination: Path, keep: int, current: Path) -> list[Path]:
    candidates = sorted(
        (
            path
            for path in destination.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}")
            if path.is_file() and path != current
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    # `keep` includes the backup just created.
    for path in candidates[max(keep - 1, 0):]:
        path.unlink()
        removed.append(path)
    return removed


def _clean_partials(destination: Path) -> None:
    """Remove only temporary files created by this backup tool."""
    for pattern in (f".{BACKUP_PREFIX}*{BACKUP_SUFFIX}.tmp", f".{BACKUP_PREFIX}*.tmp-journal"):
        for path in destination.glob(pattern):
            if path.is_file():
                path.unlink(missing_ok=True)


def backup_database(
    db_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    keep: int = DEFAULT_KEEP,
    verify: bool = True,
    log: Callable[[str], None] | None = None,
) -> tuple[Path, list[Path]]:
    source_path = Path(db_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if keep < 1:
        raise ValueError("keep must be at least 1")

    destination_path = validate_destination(Path(destination))
    destination_path.mkdir(parents=True, exist_ok=True)
    source_bytes = source_path.stat().st_size
    staging_root = Path(tempfile.gettempdir()).resolve()
    same_drive = os.path.splitdrive(staging_root)[0].lower() == os.path.splitdrive(
        destination_path
    )[0].lower()
    destination_required = int(
        source_bytes * MIN_FREE_MULTIPLIER * (2 if same_drive else 1)
    )
    destination_free = shutil.disk_usage(destination_path).free
    staging_free = shutil.disk_usage(staging_root).free
    if destination_free < destination_required:
        raise OSError(
            "insufficient backup space: "
            f"need about {destination_required} bytes, have {destination_free}"
        )
    if not same_drive and staging_free < int(source_bytes * MIN_FREE_MULTIPLIER):
        raise OSError("insufficient free space in the local staging directory")

    final_path = destination_path / _backup_name()
    temp_path = destination_path / f".{final_path.name}.tmp"
    source_uri = source_path.as_uri().replace("file:///", "file:/") + "?mode=ro"

    try:
        # OneDrive/network destinations can be much slower than the live WAL
        # update cadence. Snapshot to a local unmanaged temp directory first so
        # source changes cannot repeatedly restart a multi-gigabyte cloud write.
        with tempfile.TemporaryDirectory(prefix="aoi-backup-") as staging_dir:
            staged_path = Path(staging_dir) / "recorder.db"
            emit = log or (lambda _message: None)
            emit("creating consistent local snapshot with VACUUM INTO")
            with closing(sqlite3.connect(source_uri, uri=True, timeout=60)) as source:
                source.execute("PRAGMA busy_timeout=60000")
                source.execute("VACUUM INTO ?", (str(staged_path),))
            if verify:
                emit("snapshot copied locally; running quick_check")
                with closing(sqlite3.connect(staged_path, timeout=60)) as check:
                    result = check.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise RuntimeError(f"backup integrity check failed: {result!r}")
            _clean_partials(destination_path)
            emit(f"snapshot verified; copying to {destination_path}")
            shutil.copy2(staged_path, temp_path)
            os.replace(temp_path, final_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    removed = _prune(destination_path, keep, final_path)
    return final_path, removed


def _log(message: str) -> None:
    line = f"{datetime.now(UTC).isoformat()} {message}\n"
    try:
        with open(Path(config.HERE) / "backup.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--destination")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate and print the destination without copying the database",
    )
    args = parser.parse_args()
    try:
        destination = validate_destination(
            Path(args.destination) if args.destination else default_backup_dir()
        )
        if args.check_config:
            print(destination)
            return 0
        from db import RecorderDB, utcnow_iso

        def stamp(key: str, value: str) -> None:
            try:
                health_db = RecorderDB(args.db, config.SCHEMA_PATH)
                try:
                    health_db.note_error(key, value)
                finally:
                    health_db.close()
            except Exception:  # noqa: BLE001 — health bookkeeping must not hide backup results
                _log(f"health stamp failed key={key}")

        stamp("backup_last_run_at", utcnow_iso())
        backup, removed = backup_database(
            args.db,
            destination,
            keep=args.keep,
            verify=not args.skip_verify,
            log=_log,
        )
        stamp("backup_last_ok_at", utcnow_iso())
        _log(f"backup_last_run_at={datetime.now(UTC).isoformat()}")
        _log(f"backup_last_ok_at={datetime.now(UTC).isoformat()}")
        _log(f"backup ok path={backup} bytes={backup.stat().st_size} pruned={len(removed)}")
        print(backup)
        return 0
    except Exception as exc:  # noqa: BLE001 — scheduled pythonw process needs a durable failure record
        try:
            from db import RecorderDB, utcnow_iso

            health_db = RecorderDB(args.db, config.SCHEMA_PATH)
            try:
                health_db.note_error(
                    "last_error_backup",
                    f"{utcnow_iso()}: {type(exc).__name__}: {exc}"[:400],
                )
            finally:
                health_db.close()
        except Exception:  # noqa: BLE001 — failure bookkeeping must not mask the original error
            pass
        _log(f"backup failed: {type(exc).__name__}: {exc}")
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
