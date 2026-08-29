"""Tests for read-only/dry-run derived-data repair helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import repair_derived_data
from db import RecorderDB

SCHEMA = str(Path(__file__).resolve().parents[1] / "schema.sql")
NOW = "2026-08-23T19:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    database = RecorderDB(str(tmp_path / "repair.db"), SCHEMA)
    yield database
    database.close()


def _static_row(token="tok", protocol=None):
    return {
        "token_address": token,
        "network_id": "56",
        "recorded_at": NOW,
        "name": "Token",
        "symbol": "TOK",
        "decimals": 9,
        "dex_protocol": protocol,
        "raw_json": json.dumps({"pair": {"protocol": "PumpAmm"}}),
    }


def _social_row(token="tok", protocol="PumpAmm"):
    raw = {
        "responseObject": {
            "items": [
                {
                    "id": "th-1",
                    "createdAt": "2026-08-23T18:00:00Z",
                    "userHandle": "writer",
                    "userId": "u-1",
                    "numReplies": 0,
                    "comment": {"comment": "hello", "numLikes": 2},
                }
            ],
            "count": 1,
            "hasNextPage": False,
        }
    }
    return {
        "token_address": token,
        "network_id": "56",
        "recorded_at": NOW,
        "thesis_total": 1,
        "thesis_sampled": 1,
        "has_next_page": 0,
        "thesis_count": 1,
        "thesis_likes": 2,
        "thesis_replies": 0,
        "thesis_authors": 1,
        "holder_authors": 0,
        "newest_thesis_at": "2026-08-23T18:00:00Z",
        "raw_json": json.dumps(raw),
    }


def test_static_protocol_repair_is_dry_run_and_idempotent(db):
    db.upsert_static(_static_row())

    dry = repair_derived_data.repair_static_protocols(db, apply=False)
    assert dry == {"candidates": 1, "protocols_found": 1, "updated": 0, "decode_errors": 0}
    assert db._conn.execute("SELECT dex_protocol FROM token_static").fetchone()[0] is None

    applied = repair_derived_data.repair_static_protocols(db, apply=True)
    assert applied["updated"] == 1
    assert db._conn.execute("SELECT dex_protocol FROM token_static").fetchone()[0] == "PumpAmm"

    again = repair_derived_data.repair_static_protocols(db, apply=True)
    assert again["candidates"] == 0
    assert again["updated"] == 0


def test_static_protocol_repair_does_not_overwrite_existing_value(db):
    db.upsert_static(_static_row(protocol="UniswapV2"))

    result = repair_derived_data.repair_static_protocols(db, apply=True)

    assert result["candidates"] == 0
    assert db._conn.execute("SELECT dex_protocol FROM token_static").fetchone()[0] == "UniswapV2"


def test_static_protocol_repair_uses_archived_market_raw(db):
    row = _static_row()
    row["raw_json"] = json.dumps({"token": {"address": "tok"}})
    db.upsert_static(row)
    db.insert_tick({
        "token_address": "tok",
        "network_id": "56",
        "recorded_at": NOW,
        "source": "filter",
        "raw_json": json.dumps({"pair": {"protocol": "UniswapV4"}}),
    })

    result = repair_derived_data.repair_static_protocols(db, apply=True)

    assert result["protocols_found"] == 1
    assert result["updated"] == 1
    assert db._conn.execute(
        "SELECT dex_protocol FROM token_static"
    ).fetchone()[0] == "UniswapV4"


def test_static_created_at_repair_uses_archived_market_raw(db):
    db.upsert_static(_static_row())
    db.insert_tick({
        "token_address": "tok",
        "network_id": "56",
        "recorded_at": NOW,
        "source": "filter",
        "raw_json": json.dumps({"token": {"createdAt": 1700000000}}),
    })

    result = repair_derived_data.repair_static_created_at(db, apply=True)

    assert result["created_found"] == 1
    assert result["updated"] == 1
    assert db._conn.execute(
        "SELECT token_created_at FROM token_static"
    ).fetchone()[0] == "1700000000"


def test_thesis_repair_rebuilds_rows_from_social_raw_and_is_idempotent(db):
    first = _social_row()
    second = _social_row()
    second["recorded_at"] = "2026-08-23T19:30:00+00:00"
    db.insert_social(first)
    db.insert_social(second)

    dry = repair_derived_data.repair_thesis_from_social(db, apply=False)
    assert dry == {
        "snapshots": 2,
        "items_seen": 2,
        "items_valid": 2,
        "unique_items": 1,
        "already_present": 0,
        "missing_items": 1,
        "inserted": 0,
        "decode_errors": 0,
    }
    assert db._conn.execute("SELECT COUNT(*) FROM token_thesis").fetchone()[0] == 0

    applied = repair_derived_data.repair_thesis_from_social(db, apply=True)
    assert applied["inserted"] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM token_thesis").fetchone()[0] == 1

    again = repair_derived_data.repair_thesis_from_social(db, apply=True)
    assert again["items_valid"] == 2
    assert again["unique_items"] == 1
    assert again["already_present"] == 1
    assert again["missing_items"] == 0
    assert again["inserted"] == 0
