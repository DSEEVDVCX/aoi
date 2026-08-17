"""اختبارات قياس BSC من NodeReal وإدراجه في جدول التركّز."""
from __future__ import annotations

import os

import pytest

import bsc_layer
from db import RecorderDB, decode_raw

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-08-14T22:00:00+00:00"
NET = "56"
TOK = "0xaaaa000000000000000000000000000000000001"
ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()


def _watch(db):
    db.upsert_watch(TOK, NET, "large_buy", "sig-1", 48, NOW)


def test_build_row_computes_all_tiers_from_exact_uints():
    row = bsc_layer.build_bsc_concentration_row(
        token=TOK,
        recorded_at=NOW,
        watch_first_seen_at=NOW,
        entry_signal_id="sig-1",
        is_control=0,
        holder_count=40,
        total_supply=2_000,
        top=[("0xa", 700), ("0xb", 500), ("0xc", 200), ("0xd", 100), ("0xe", 100)],
    )

    assert row["holder_count"] == 40
    assert row["top1_pct"] == pytest.approx(35.0)
    assert row["top5_pct"] == pytest.approx(80.0)
    assert row["top10_pct"] == pytest.approx(80.0)
    assert row["top20_pct"] == pytest.approx(80.0)
    assert row["is_replay"] == 0


def test_build_row_excludes_burn_addresses_from_supply_and_top():
    row = bsc_layer.build_bsc_concentration_row(
        token=TOK,
        recorded_at=NOW,
        watch_first_seen_at=NOW,
        entry_signal_id=None,
        is_control=0,
        holder_count=2,
        total_supply=1_000,
        top=[(ZERO, 500), (DEAD, 100), ("0xa", 300), ("0xb", 100)],
        burn_balances=[(ZERO, 500), (DEAD, 100)],
    )

    assert row["holder_count"] == 0
    assert row["top1_pct"] == pytest.approx(75.0)
    assert row["top5_pct"] == pytest.approx(100.0)
    assert row["raw_json"]["supply_base"] == "400"


def test_build_row_keeps_tiers_when_provider_lacks_holder_count():
    row = bsc_layer.build_bsc_concentration_row(
        token=TOK,
        recorded_at=NOW,
        watch_first_seen_at=NOW,
        entry_signal_id=None,
        is_control=0,
        holder_count=None,
        total_supply=1_000,
        top=[("0xa", 500), ("0xb", 250)],
    )

    assert row["holder_count"] is None
    assert row["top1_pct"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_cycle_writes_bsc_snapshot_from_nodereal(db):
    _watch(db)

    class RPC:
        async def holder_count(self, token):
            assert token == TOK
            return 40

        async def total_supply(self, token):
            return 2_000

        async def top_holders(self, token, top_n=20):
            assert top_n == 22
            return [("0xa", 700), ("0xb", 500), ("0xc", 200)]

        async def balance_of(self, token, holder):
            return 0

    async def noop(_seconds):
        pass

    stats = await bsc_layer.run_bsc_cycle(RPC(), db, NOW, sleep=noop)

    assert stats == {"bsc_due": 1, "bsc_rows": 1, "bsc_errors": 0, "bsc_rate_limits": 0}
    row = db._conn.execute(
        "SELECT holder_count, top1_pct, top5_pct, top10_pct, top20_pct, raw_json "
        "FROM chain_concentration"
    ).fetchone()
    assert row["holder_count"] == 40
    assert row["top1_pct"] == pytest.approx(35.0)
    assert decode_raw(row["raw_json"])["source"] == "nodereal_bsc"
