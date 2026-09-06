import os

import repair_recoverable_outcomes
from db import RecorderDB

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)


def _outcome(db, status="no_bars"):
    db._conn.execute(
        """INSERT INTO outcomes(
               kind,key,token_address,network_id,entry_ts,status,labeled_at,
               design_version,analysis_eligible)
           VALUES('watch','w','tok','56',1000,?, 'now',3,1)""",
        (status,),
    )
    db._conn.commit()


def test_dry_run_does_not_change_outcome(tmp_path):
    db = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    try:
        _outcome(db)
        assert repair_recoverable_outcomes.repair(db, apply=False) == []
        assert db._conn.execute("SELECT status FROM outcomes").fetchone()[0] == "no_bars"
    finally:
        db.close()


def test_recomputes_no_bars_when_local_bars_exist(tmp_path):
    db = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    try:
        _outcome(db)
        db.insert_bars([
            {"token_address": "tok", "network_id": "56", "resolution": "5",
             "ts": 1000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "fetched_at": "now"},
            {"token_address": "tok", "network_id": "56", "resolution": "5",
             "ts": 1300, "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 1, "fetched_at": "now"},
        ])
        db._conn.execute(
            "INSERT INTO training_rows(kind,key,token_address,entry_ts,built_at) "
            "VALUES('watch','w','tok',1000,'now')"
        )
        db._conn.commit()
        changes = repair_recoverable_outcomes.repair(db, apply=True)
        assert len(changes) == 1
        row = db._conn.execute(
            "SELECT status, analysis_eligible, max_gain_24h FROM outcomes"
        ).fetchone()
        assert row["status"] == "ok"
        assert row["analysis_eligible"] == 1
        assert row["max_gain_24h"] == 1.0
        assert db._conn.execute("SELECT COUNT(*) FROM training_rows").fetchone()[0] == 0
    finally:
        db.close()


def test_keeps_no_bars_when_no_later_bars_exist(tmp_path):
    db = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    try:
        _outcome(db)
        db.insert_bars([
            {"token_address": "tok", "network_id": "56", "resolution": "5",
             "ts": 1000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "fetched_at": "now"},
        ])
        assert repair_recoverable_outcomes.repair(db, apply=True) == []
        assert db._conn.execute("SELECT status FROM outcomes").fetchone()[0] == "no_bars"
    finally:
        db.close()
