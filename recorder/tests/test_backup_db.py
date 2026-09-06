import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest
from backup_db import backup_database, validate_destination


def _database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample VALUES (?)", (value,))


def _stage_in(tmp_path, monkeypatch):
    """Point every staging path (purge and creation alike) at the test's own
    directory, so no test ever sweeps or writes the machine's real Temp."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


def test_backup_is_consistent_and_readable(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "external"
    _database(source, "kept")

    backup, removed = backup_database(source, destination, keep=3)

    assert removed == []
    assert backup.parent == destination
    assert not list(destination.glob("*.tmp"))
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "kept"


def test_backup_retention_only_prunes_managed_snapshots(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "external"
    _database(source, "kept")
    unrelated = destination / "keep-me.txt"
    destination.mkdir()
    unrelated.write_text("safe", encoding="utf-8")

    backups = [backup_database(source, destination, keep=2)[0] for _ in range(3)]

    assert len(list(destination.glob("recorder-*.db"))) == 2
    assert not backups[0].exists()
    assert unrelated.read_text(encoding="utf-8") == "safe"


def test_backup_destination_cannot_be_inside_workspace():
    with pytest.raises(ValueError, match="outside"):
        validate_destination(Path(__file__).parents[2] / "backups")


def test_interrupted_staging_is_purged_before_the_next_backup(tmp_path, monkeypatch):
    """A killed backup's staging directory is the 35 GB of silent disk rot
    found 2026-09-06; the next run sweeps it *before* its own free-space
    check — the leftover was on the same drive and could fail that check."""
    _stage_in(tmp_path, monkeypatch)
    source = tmp_path / "source.db"
    destination = tmp_path / "external"
    _database(source, "kept")
    stale = tmp_path / "aoi-backup-deadbeef"
    stale.mkdir()
    (stale / "recorder.db").write_bytes(b"\0" * 1024)
    week_ago = time.time() - 7 * 24 * 3600
    os.utime(stale, (week_ago, week_ago))

    backup, _removed = backup_database(source, destination)

    assert not stale.exists()
    assert backup.exists()


def test_a_staging_directory_still_in_flight_is_left_alone(tmp_path, monkeypatch):
    """Only age marks a directory abandoned: a backup running in parallel
    keeps rewriting its staging, and the sweep must not take it."""
    _stage_in(tmp_path, monkeypatch)
    source = tmp_path / "source.db"
    destination = tmp_path / "external"
    _database(source, "kept")
    fresh = tmp_path / "aoi-backup-live"
    fresh.mkdir()
    (fresh / "recorder.db").write_bytes(b"\0" * 1024)

    backup_database(source, destination)

    assert fresh.exists()


def test_an_unrelated_temp_directory_is_never_touched(tmp_path, monkeypatch):
    """The sweep owns one prefix, not the whole Temp directory."""
    _stage_in(tmp_path, monkeypatch)
    source = tmp_path / "source.db"
    destination = tmp_path / "external"
    _database(source, "kept")
    stranger = tmp_path / "someone-elses-work"
    stranger.mkdir()
    week_ago = time.time() - 7 * 24 * 3600
    os.utime(stranger, (week_ago, week_ago))

    backup_database(source, destination)

    assert stranger.exists()
