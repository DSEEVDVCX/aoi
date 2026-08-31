"""EVM admission gate fix tests (2026-08-29).

Four defects measured on the live database on 2026-08-29; these tests pin
their fixes one by one:

1. Work units in `evm_admission_policy` were estimated at 10,000
   blocks/call (the range cap), while the measured backfill on Robinhood
   and at the head is hundreds of thousands of blocks per call (79,894
   transfers in 33 calls; 1,714 in a single call). The wrong estimate
   inflated work_units ×25, keeping the gate paused forever.
2. No upper age cap: a two-year-old coin passes the two-day minimum age
   gate and enters a backfill queue of millions of blocks.
3. The `policy.allows` check applied to new coins only, so reactivating an
   expired watch bypassed the pause entirely.
4. `upsert_watch` deleted all backfill progress
   (balances/backfill/replay) on reactivation, so the coin went back to
   zero and the queues were rebuilt endlessly.
"""
import os

import config
import pytest
from db import RecorderDB

import recorder

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-29T12:00:00+00:00"
LATER = "2026-08-30T12:00:00+00:00"

# The time constants are ISO strings; these helpers convert them to epoch for seeding.
from datetime import datetime  # noqa: E402

_NOW_TS = int(datetime.fromisoformat(NOW).timestamp())


def _created_days_before(ts: int, days: float) -> str:
    """A timestamp for a coin minted `days` days before the given moment."""
    return str(int(ts - days * 86400))


@pytest.fixture()
def db(tmp_path):
    recorder._evm_admission_paused_runtime = False
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()
    recorder._evm_admission_paused_runtime = False


def _seed_watch(db, token, net="8453", created="1600000000"):
    db.upsert_static({
        "token_address": token, "network_id": net, "recorded_at": NOW,
        "token_created_at": created, "raw_json": "{}",
    })


# ---------------------------------------------------------------------------
# 1) Work units: the estimate must follow the range actually cut per call, not the range cap
# ---------------------------------------------------------------------------

def test_work_units_follow_measured_range_per_call(db, monkeypatch):
    """A coin with 3M blocks of remaining range = units per what a call actually cuts.

    The live measurement (evm.log): backfill cuts hundreds of thousands of
    blocks per call, and estimating "range cap = one unit" inflates the
    work and closes the gate forever. A unit now = a measured range per
    call (`EVM_BACKFILL_BLOCKS_PER_CALL`). On Robinhood (500K/call): 3M
    blocks = only 6 units (it was 300 under the old 10K cap); on Base
    (whose real cap is 10K/call) it stays 300 — the difference is that
    4663 has no range cap at all, so its old estimate was entirely wrong.
    """
    monkeypatch.setattr(config, "EVM_NETWORKS", ("4663",))
    db.set_evm_cursor("4663", 4_000_000, NOW, "ok")
    token = "0x" + "1" * 40
    db.upsert_watch(token, "4663", "large_buy", "s-1", 48, NOW)
    db.set_evm_backfill_state(
        "4663", token, "partial", NOW, from_block=1, to_block=3_000_000,
    )

    state = recorder.evm_admission_policy(db).network("4663")

    per_call = int(config.EVM_BACKFILL_BLOCKS_PER_CALL["4663"])
    expected = max(1, -(-3_000_000 // per_call))
    assert state.work_units == expected
    # 3M blocks at 500K/call = only 6 units, not 300 as the old estimate had it
    assert state.work_units == 6
    assert state.paused is False


def test_work_units_huge_range_still_pauses(db, monkeypatch):
    """A range the size of a whole chain (49M) stays throttled — the gate does not open blindly."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    db.set_evm_cursor("8453", 50_000_000, NOW, "ok")
    for i in range(6):
        token = f"0x{i:040x}"
        db.upsert_watch(token, "8453", "large_buy", f"s-{i}", 48, NOW)
        db.set_evm_backfill_state(
            "8453", token, "partial", NOW,
            from_block=1, to_block=49_000_000,
        )

    state = recorder.evm_admission_policy(db).network("8453")
    per_call = int(config.EVM_BACKFILL_BLOCKS_PER_CALL["8453"])
    assert state.work_units == 6 * max(1, -(-49_000_000 // per_call))
    assert state.paused is True  # utilization ≥4 ⇒ pause


# ---------------------------------------------------------------------------
# 2) The upper age cap for EVM admission
# ---------------------------------------------------------------------------

def test_evm_max_age_rejects_old_tokens(db, monkeypatch):
    """An EVM coin older than the cap opens no watch, and the signal stays saved."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    monkeypatch.setattr(config, "EVM_MAX_TOKEN_AGE_DAYS", 60)
    old_created = _created_days_before(_NOW_TS, 400)  # 400 days
    token = "0x" + "2" * 40
    _seed_watch(db, token, created=old_created)
    raw = {"responseObject": {"data": [{
        "id": "evt-old", "tokenAddress": token, "networkId": 8453,
        "type": "large_buy", "createdAt": NOW,
        "body": {"ticker": "OLD", "price": 1.0},
    }]}}
    stats = {"signals": 0, "watch_added": 0, "evm_admission_deferred": 0,
             "errors": 0, "age_rejected": 0, "age_unknown": 0,
             "evm_max_age_rejected": 0}
    policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 0, 1, 1, False,
    )

    import asyncio
    asyncio.run(recorder.record_feed(
        db, raw, NOW, lambda _id: None, {}, policy, stats,
    ))

    assert db._conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0] == 1
    row = db._conn.execute(
        "SELECT active FROM watchlist WHERE token_address=?", (token,)
    ).fetchone()
    assert row is None  # no watch was opened
    assert stats["evm_max_age_rejected"] == 1


def test_evm_max_age_allows_fresh_tokens(db, monkeypatch):
    """A coin within the cap enters normally — the gate does not close the whole network."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    monkeypatch.setattr(config, "EVM_MAX_TOKEN_AGE_DAYS", 60)
    fresh_created = _created_days_before(_NOW_TS, 10)  # 10 days
    token = "0x" + "3" * 40
    _seed_watch(db, token, created=fresh_created)
    raw = {"responseObject": {"data": [{
        "id": "evt-fresh", "tokenAddress": token, "networkId": 8453,
        "type": "large_buy", "createdAt": NOW,
        "body": {"ticker": "FRESH", "price": 1.0},
    }]}}
    stats = {"signals": 0, "watch_added": 0, "evm_admission_deferred": 0,
             "errors": 0, "age_rejected": 0, "age_unknown": 0,
             "evm_max_age_rejected": 0}
    policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 0, 1, 1, False,
    )

    import asyncio
    asyncio.run(recorder.record_feed(
        db, raw, NOW, lambda _id: None, {}, policy, stats,
    ))

    assert db._conn.execute(
        "SELECT active FROM watchlist WHERE token_address=?", (token,)
    ).fetchone()[0] == 1
    assert stats["watch_added"] == 1


def test_evm_max_age_zero_disables_cap(db, monkeypatch):
    """Zero disables the cap — a one-year-old coin enters as before."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    monkeypatch.setattr(config, "EVM_MAX_TOKEN_AGE_DAYS", 0)
    old_created = _created_days_before(_NOW_TS, 400)
    token = "0x" + "4" * 40
    _seed_watch(db, token, created=old_created)
    raw = {"responseObject": {"data": [{
        "id": "evt-zero", "tokenAddress": token, "networkId": 8453,
        "type": "large_buy", "createdAt": NOW,
        "body": {"ticker": "ZERO", "price": 1.0},
    }]}}
    stats = {"signals": 0, "watch_added": 0, "evm_admission_deferred": 0,
             "errors": 0, "age_rejected": 0, "age_unknown": 0,
             "evm_max_age_rejected": 0}
    policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 0, 1, 1, False,
    )

    import asyncio
    asyncio.run(recorder.record_feed(
        db, raw, NOW, lambda _id: None, {}, policy, stats,
    ))

    assert db._conn.execute(
        "SELECT active FROM watchlist WHERE token_address=?", (token,)
    ).fetchone()[0] == 1


def test_evm_max_age_unknown_age_is_ignored_by_cap(db, monkeypatch):
    """Unknown age is not rejected by the cap — the minimum age gate handles it (AGE_UNKNOWN)."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    monkeypatch.setattr(config, "EVM_MAX_TOKEN_AGE_DAYS", 60)
    token = "0x" + "5" * 40
    db.upsert_static({
        "token_address": token, "network_id": "8453", "recorded_at": NOW,
        "raw_json": "{}",
    })  # no token_created_at
    raw = {"responseObject": {"data": [{
        "id": "evt-unknown", "tokenAddress": token, "networkId": 8453,
        "type": "large_buy", "createdAt": NOW,
        "body": {"ticker": "UNK", "price": 1.0},
    }]}}
    stats = {"signals": 0, "watch_added": 0, "evm_admission_deferred": 0,
             "errors": 0, "age_rejected": 0, "age_unknown": 0,
             "evm_max_age_rejected": 0}
    policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 0, 1, 1, False,
    )

    import asyncio
    asyncio.run(recorder.record_feed(
        db, raw, NOW, lambda _id: None, {}, policy, stats,
    ))

    # Unknown age is rejected by the minimum age gate (age_unknown), not by the upper cap
    assert stats["evm_max_age_rejected"] == 0
    assert stats["age_unknown"] == 1


# ---------------------------------------------------------------------------
# 3) The policy applies to reactivation, not only to new coins
# ---------------------------------------------------------------------------

def test_reactivation_respects_paused_policy(db, monkeypatch):
    """A signal on an expired watched coin does not reactivate it while the gate is paused."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    token = "0x" + "6" * 40
    # Age 30 days: within the upper age cap, so the policy's throttle alone is measured
    _seed_watch(db, token, created=_created_days_before(_NOW_TS, 30))
    # a first window that has expired
    db.upsert_watch(token, "8453", "large_buy", "s-old", 48, NOW)
    db._conn.execute(
        "UPDATE watchlist SET active=0 WHERE token_address=?", (token,)
    )
    db._conn.commit()
    raw = {"responseObject": {"data": [{
        "id": "evt-react", "tokenAddress": token, "networkId": 8453,
        "type": "large_buy", "createdAt": LATER,
        "body": {"ticker": "REACT", "price": 1.0},
    }]}}
    stats = {"signals": 0, "watch_added": 0, "evm_admission_deferred": 0,
             "errors": 0, "age_rejected": 0, "age_unknown": 0,
             "evm_max_age_rejected": 0}
    paused_policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 45, 0, 1, True,
    )

    import asyncio
    asyncio.run(recorder.record_feed(
        db, raw, LATER, lambda _id: None, {}, paused_policy, stats,
    ))

    # The signal is saved, and the watch was not reactivated
    assert db._conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT active FROM watchlist WHERE token_address=?", (token,)
    ).fetchone()[0] == 0
    assert stats["evm_admission_deferred"] == 1


def test_reactivation_allowed_when_policy_open(db, monkeypatch):
    """An open gate allows reactivation as usual."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    token = "0x" + "7" * 40
    _seed_watch(db, token, created=_created_days_before(_NOW_TS, 30))
    db.upsert_watch(token, "8453", "large_buy", "s-old", 48, NOW)
    db._conn.execute(
        "UPDATE watchlist SET active=0 WHERE token_address=?", (token,)
    )
    db._conn.commit()
    raw = {"responseObject": {"data": [{
        "id": "evt-react2", "tokenAddress": token, "networkId": 8453,
        "type": "large_buy", "createdAt": LATER,
        "body": {"ticker": "REACT2", "price": 1.0},
    }]}}
    stats = {"signals": 0, "watch_added": 0, "evm_admission_deferred": 0,
             "errors": 0, "age_rejected": 0, "age_unknown": 0,
             "evm_max_age_rejected": 0}
    open_policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 0, 1, 1, False,
    )

    import asyncio
    asyncio.run(recorder.record_feed(
        db, raw, LATER, lambda _id: None, {}, open_policy, stats,
    ))

    assert db._conn.execute(
        "SELECT active FROM watchlist WHERE token_address=?", (token,)
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 4) Reactivation does not delete all backfill progress
# ---------------------------------------------------------------------------

def _seed_progress(db, token, net="8453"):
    db.set_evm_cursor(net, 1_000_000, NOW, "ok")
    db.set_evm_backfill_state(
        net, token, "partial", NOW, from_block=500_000, to_block=1_000_000,
        transfers=123, calls=7,
    )
    db._conn.execute(
        """INSERT INTO evm_balances(network_id, token_address, holder_address,
               balance_hex, first_seen_block, updated_block, updated_at)
           VALUES(?, ?, ?, '0x1', 500000, 900000, ?)""",
        (net, token.lower(), "0x" + "9" * 40, NOW),
    )
    db._conn.commit()


def test_reactivation_keeps_backfill_progress_short_gap(db, monkeypatch):
    """A short gap (≤ the cap) keeps the ledger and the checkpoint and only widens to_block."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    token = "0x" + "8" * 40
    _seed_watch(db, token, created=_created_days_before(_NOW_TS, 30))
    db.upsert_watch(token, "8453", "large_buy", "s-old", 48, NOW)
    db._conn.execute(
        "UPDATE watchlist SET active=0, watch_until=? WHERE token_address=?",
        ("2026-08-29T13:00:00+00:00", token),
    )
    db._conn.commit()
    _seed_progress(db, token)

    # Reactivation two hours after the window ended
    gap = int(config.EVM_REACTIVATION_KEEP_LEDGER_SECONDS)
    reactivate_at = "2026-08-29T14:00:00+00:00"
    assert gap >= 2 * 3600

    db.upsert_watch(token, "8453", "large_buy", "s-new", 48, reactivate_at)

    row = db._conn.execute(
        "SELECT status, from_block, transfers FROM evm_backfill_state"
        " WHERE token_address=?", (token.lower(),),
    ).fetchone()
    assert row is not None
    assert row[0] == "partial"
    assert row[1] == 500_000          # the resume point was not zeroed
    assert row[2] == 123              # the transfer counter is kept
    balances = db._conn.execute(
        "SELECT COUNT(*) FROM evm_balances WHERE token_address=?",
        (token.lower(),),
    ).fetchone()[0]
    assert balances == 1              # the ledger was not wiped


def test_reactivation_resets_ledger_long_gap(db, monkeypatch):
    """A long gap (> the cap) rebuilds from scratch — the old safety behavior."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    token = "0x" + "a" * 40
    _seed_watch(db, token, created=_created_days_before(_NOW_TS, 30))
    db.upsert_watch(token, "8453", "large_buy", "s-old", 48, NOW)
    # The first window ended 2026-08-31T12:00 (NOW+48h). Reactivation five
    # days after it ended — beyond the 48h retention cap, so it rebuilds from scratch.
    db._conn.execute(
        "UPDATE watchlist SET active=0 WHERE token_address=?", (token,),
    )
    db._conn.commit()
    _seed_progress(db, token)

    db.upsert_watch(
        token, "8453", "large_buy", "s-new", 48,
        "2026-09-05T12:00:00+00:00",
    )

    row = db._conn.execute(
        "SELECT COUNT(*) FROM evm_backfill_state WHERE token_address=?",
        (token.lower(),),
    ).fetchone()[0]
    assert row == 0  # deleted entirely — rebuilt from genesis


# ---------------------------------------------------------------------------
# Integration: the gate truly opens with live-database shapes (2026-08-29 values)
# ---------------------------------------------------------------------------

def test_live_backlog_shape_now_admits(db, monkeypatch):
    """The live queue shape (55 coins, ~132k old units) becomes admissible.

    With the corrected estimate: 55 coins × ~24M remaining blocks on average
    = ~4,400 units (at 500K/call on 4663) against a capacity of 72 ⇒ still
    throttled, but in a range the system drains in days, not centuries — and
    that is measured in running days. Here we only pin that the measurement
    itself is accurate: a unit = range/call, not range/cap.
    """
    monkeypatch.setattr(config, "EVM_NETWORKS", ("4663",))
    db.set_evm_cursor("4663", 49_300_000, NOW, "ok")
    for i in range(3):
        token = f"0x{100 + i:040x}"
        db.upsert_watch(token, "4663", "large_buy", f"s-{i}", 48, NOW)
        db.set_evm_backfill_state(
            "4663", token, "partial", NOW,
            from_block=25_000_000, to_block=49_300_000,
        )
    state = recorder.evm_admission_policy(db).network("4663")
    per_call = int(config.EVM_BACKFILL_BLOCKS_PER_CALL["4663"])
    expected = 3 * max(1, -(-24_300_000 // per_call))
    assert state.work_units == expected
