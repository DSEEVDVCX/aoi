import os

import config
import pytest
from db import RecorderDB

import recorder

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-20T12:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    recorder._evm_admission_paused_runtime = False
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()
    recorder._evm_admission_paused_runtime = False


def _backlog(db, count):
    for index in range(count):
        token = f"0x{index:040x}"
        db.upsert_watch(token, "8453", "large_buy", f"s-{index}", 48, NOW)
        db.set_evm_backfill_state(
            "8453", token, "partial", NOW, from_block=1, to_block=2,
        )


@pytest.mark.parametrize(
    ("backlog", "percent"),
    [(0, 100), (14, 100), (15, 75), (29, 75), (30, 50), (44, 50), (45, 0)],
)
def test_policy_levels_follow_backlog(db, monkeypatch, backlog, percent):
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    _backlog(db, backlog)

    policy = recorder.evm_admission_policy(db)

    assert policy.backlog == backlog
    assert policy.percent == percent


def test_paused_policy_resumes_only_below_low_watermark(db, monkeypatch):
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    db.set_meta("evm_admission_paused", "1")
    _backlog(db, 20)
    assert recorder.evm_admission_policy(db).percent == 0

    db._conn.execute(
        "UPDATE evm_backfill_state SET status='done' WHERE token_address=?",
        ("0x0000000000000000000000000000000000000000",),
    )
    db._conn.commit()
    assert recorder.evm_admission_policy(db).percent == 75


def test_non_evm_network_is_never_throttled():
    policy = recorder.EVMAdmissionPolicy(
        networks=frozenset({"8453"}), backlog=50, numerator=0, denominator=1,
        paused=True,
    )
    assert policy.allows("signal", "sol-token", "1399811149", "event")


def test_same_event_has_a_stable_decision():
    policy = recorder.EVMAdmissionPolicy(
        networks=frozenset({"8453"}), backlog=30, numerator=1, denominator=2,
        paused=False,
    )
    decisions = {
        policy.allows("signal", "0xtoken", "8453", "event-1")
        for _ in range(20)
    }
    assert len(decisions) == 1
    assert policy.allows("signal", "0xtoken", "8453", "event-1") == policy.allows(
        "signal", "0xtoken", "8453", "event-2"
    )


def test_same_token_has_one_decision_across_all_admission_paths():
    policy = recorder.EVMAdmissionPolicy(
        networks=frozenset({"8453"}), backlog=30, numerator=1, denominator=2,
        paused=False,
    )

    decisions = {
        policy.allows(kind, token, "8453", key)
        for kind, token, key in (
            ("signal", "0xAbCd", "event-1"),
            ("comparison", "0xabcd", "event-2"),
            ("control", "0xABCD", NOW),
        )
    }

    assert len(decisions) == 1
    assert policy.score("signal", "0xAbCd", "8453") == policy.score(
        "control", "0xabcd", "8453"
    )


def test_mixed_case_completed_address_is_not_counted_pending(db, monkeypatch):
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    token = "0xAbCd000000000000000000000000000000000001"
    db.upsert_watch(token, "8453", "large_buy", "s", 48, NOW)
    db.set_evm_backfill_state("8453", token, "done", NOW)
    assert recorder.evm_admission_policy(db).backlog == 0


def test_comparison_windows_throttle_only_evm(db):
    for event_id, token, network in (
        ("evm", "0xabc", "8453"),
        ("sol", "sol-token", "1399811149"),
    ):
        db.insert_signal({
            "id": event_id, "token_address": token, "network_id": network,
            "ts": NOW, "recorded_at": NOW, "signal_type": "large_buy",
            "raw_json": "{}",
        })
    policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 45, 0, 1, True,
    )

    added = recorder.admit_signal_comparison_windows(
        db,
        [("0xabc", "8453", 1.0, "trending"),
         ("sol-token", "1399811149", 1.0, "trending")],
        NOW,
        policy=policy,
    )

    assert added == 1
    assert db._conn.execute(
        "SELECT token_address FROM watch_windows"
    ).fetchone()[0] == "sol-token"


def test_existing_evm_watch_bypasses_pause_for_comparison(db):
    db.upsert_watch("0xabc", "8453", "large_buy", "old", 48, NOW)
    db.insert_signal({
        "id": "new", "token_address": "0xabc", "network_id": "8453",
        "ts": NOW, "recorded_at": NOW, "signal_type": "large_buy",
        "raw_json": "{}",
    })
    policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 45, 0, 1, True,
    )
    assert recorder.admit_signal_comparison_windows(
        db, [("0xabc", "8453", 1.0, "trending")], NOW, policy=policy,
    ) == 1


def test_cycle_comparison_requires_operational_admission(db):
    db.insert_signal({
        "id": "new", "token_address": "0xabc", "network_id": "8453",
        "ts": NOW, "recorded_at": NOW, "signal_type": "large_buy",
        "raw_json": "{}",
    })
    policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 0, 1, 1, False,
    )
    assert recorder.admit_signal_comparison_windows(
        db, [("0xabc", "8453", 1.0, "trending")], NOW,
        policy=policy, admitted_signals=set(),
    ) == 0


async def test_feed_signal_survives_watch_admission_failure(db, monkeypatch):
    raw = {"responseObject": {"data": [{
        "id": "event", "tokenAddress": "0xabc", "networkId": 8453,
        "type": "large_buy", "createdAt": NOW,
        "body": {"ticker": "ABC", "price": 1.0},
    }]}}
    policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 0, 1, 1, False,
    )
    # عمرٌ معروفٌ وقديم: البوّابة ليست موضوع هذا الاختبار، فلا تحجب المسار عنه.
    db.upsert_static({
        "token_address": "0xabc", "network_id": "8453", "recorded_at": NOW,
        "token_created_at": "1600000000", "raw_json": "{}",
    })
    monkeypatch.setattr(db, "upsert_watch", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("watch failed")
    ))
    stats = {"signals": 0, "watch_added": 0, "evm_admission_deferred": 0,
             "errors": 0, "age_rejected": 0, "age_unknown": 0}

    await recorder.record_feed(
        db, raw, NOW, lambda _id: None, {}, policy, stats,
    )

    assert db._conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0] == 1
    assert stats["errors"] == 1


def test_control_sample_throttles_only_evm(db):
    db.upsert_watch("signal-sol", "1399811149", "large_buy", "s1", 48, NOW)
    db.upsert_watch("signal-evm", "8453", "large_buy", "s2", 48, NOW)
    policy = recorder.EVMAdmissionPolicy(
        frozenset({"8453"}), 45, 0, 1, True,
    )

    added = recorder.admit_control_sample(
        db,
        [("control-sol", "1399811149"), ("control-evm", "8453")],
        NOW,
        policy=policy,
    )

    assert added == 1
    assert db._conn.execute(
        "SELECT network_id FROM watchlist WHERE is_control=1"
    ).fetchone()[0] == "1399811149"
