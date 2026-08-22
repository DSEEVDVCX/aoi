import os

import pytest
import repair_incomplete_outcomes
from db import RecorderDB

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()


def test_repair_is_dry_run_by_default(db):
    db._conn.execute(
        """INSERT INTO outcomes(
               kind,key,token_address,network_id,entry_ts,status,labeled_at,
               final_return_48h,max_gain_24h,is_rug,design_version,analysis_eligible)
           VALUES('watch','bad','tok','56',1,'ok','now',0.1,NULL,0,3,1)"""
    )
    db._conn.commit()

    assert repair_incomplete_outcomes.repair(db, apply=False) == 1
    row = db._conn.execute("SELECT status FROM outcomes").fetchone()
    assert row["status"] == "ok"


def test_repair_is_idempotent(db):
    db._conn.execute(
        """INSERT INTO outcomes(
               kind,key,token_address,network_id,entry_ts,status,labeled_at,
               final_return_48h,max_gain_24h,is_rug,design_version,analysis_eligible)
           VALUES('watch','bad','tok','56',1,'ok','now',0.1,NULL,0,3,1)"""
    )
    db._conn.commit()
    db._conn.execute(
        "INSERT INTO training_rows(kind,key,token_address,entry_ts,built_at) "
        "VALUES('watch','bad','tok',1,'now')"
    )
    db._conn.commit()

    assert repair_incomplete_outcomes.repair(db, apply=True) == 1
    assert repair_incomplete_outcomes.repair(db, apply=True) == 0
    row = db._conn.execute(
        "SELECT status, analysis_eligible, exclusion_reason FROM outcomes"
    ).fetchone()
    assert dict(row) == {
        "status": "incomplete", "analysis_eligible": 0,
        "exclusion_reason": "incomplete_metrics",
    }
    assert db._conn.execute("SELECT COUNT(*) FROM training_rows").fetchone()[0] == 0


def test_repair_removes_training_rows_for_already_incomplete_outcomes(db):
    db._conn.execute(
        """INSERT INTO outcomes(
               kind,key,token_address,network_id,entry_ts,status,labeled_at,
               design_version,analysis_eligible)
           VALUES('watch','already','tok','56',1,'incomplete','now',3,0)"""
    )
    db._conn.execute(
        "INSERT INTO training_rows(kind,key,token_address,entry_ts,built_at) "
        "VALUES('watch','already','tok',1,'now')"
    )
    db._conn.commit()

    assert repair_incomplete_outcomes.repair(db, apply=True) == 0
    assert db._conn.execute("SELECT COUNT(*) FROM training_rows").fetchone()[0] == 0
