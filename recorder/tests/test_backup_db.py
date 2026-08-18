import sqlite3
from pathlib import Path

import pytest
from backup_db import backup_database, validate_destination


def _database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.execute("INSERT INTO sample VALUES (?)", (value,))


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
