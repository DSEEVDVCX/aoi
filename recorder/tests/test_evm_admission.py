import json
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


def _backlog(db, count, *, span=2):
    db.set_evm_cursor("8453", 100, NOW, "ok")
    for index in range(count):
        token = f"0x{index:040x}"
        db.upsert_watch(token, "8453", "large_buy", f"s-{index}", 48, NOW)
        db.set_evm_backfill_state(
            "8453", token, "partial", NOW, from_block=1, to_block=span,
        )


@pytest.mark.parametrize(
    ("backlog", "percent"),
    [(0, 100), (14, 100), (480, 75), (960, 50), (1920, 0)],
)
def test_policy_levels_follow_network_rpc_capacity(db, monkeypatch, backlog, percent):
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    _backlog(db, backlog)

    policy = recorder.evm_admission_policy(db)

    assert policy.backlog == backlog
    assert policy.network_percent("8453") == percent


def test_policy_isolated_per_network_and_records_rpc_budget(db, monkeypatch):
    """شبكة متوقفة لا توقف شبكة أخرى ذات RPC سليم."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453", "143"))
    for index in range(60):
        token = f"0x{index + 100:040x}"
        db.upsert_watch(token, "8453", "large_buy", f"b-{index}", 48, NOW)
        db.set_evm_backfill_state(
            "8453", token, "partial", NOW, from_block=1, to_block=2,
        )
    db.upsert_watch("0x" + "f" * 40, "143", "large_buy", "m-1", 48, NOW)
    db.set_evm_backfill_state(
        "143", "0x" + "f" * 40, "retry", NOW, last_error="429",
    )
    db.set_evm_cursor("8453", 100, NOW, "ok")

    policy = recorder.evm_admission_policy(db)

    assert policy.network("143").paused is True
    assert policy.network("143").reason == "active_retry"
    assert policy.network("8453").paused is False
    assert policy.network_percent("8453") == 100
    state = json.loads(db.get_meta("evm_admission_network_state"))
    assert set(state) == {"143", "8453"}


def test_healthy_network_can_admit_when_other_network_is_paused(db, monkeypatch):
    """الحالة السليمة تبقى مستقلة حتى لو توقفت Monad."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453", "143"))
    db.upsert_watch("0x" + "1" * 40, "143", "large_buy", "m-1", 48, NOW)
    db.set_evm_backfill_state(
        "143", "0x" + "1" * 40, "retry", NOW, last_error="429",
    )
    db.set_evm_cursor("8453", 100, NOW, "ok")

    policy = recorder.evm_admission_policy(db)

    assert policy.network("143").paused is True
    assert policy.network("8453").paused is False
    assert policy.network_percent("8453") == 100
    assert policy.allows("signal", "0x" + "2" * 40, "8453", "event") is True
    assert policy.allows("signal", "0x" + "3" * 40, "143", "event") is False


def test_paused_policy_resumes_only_below_low_watermark(db, monkeypatch):
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    db.set_meta("evm_admission_network_state", json.dumps({"8453": {"paused": True}}))
    _backlog(db, 20)
    assert recorder.evm_admission_policy(db).network_percent("8453") == 100

    db._conn.execute(
        "UPDATE evm_backfill_state SET status='done' WHERE token_address=?",
        ("0x0000000000000000000000000000000000000000",),
    )
    db._conn.commit()
    assert recorder.evm_admission_policy(db).network_percent("8453") == 100


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
        db.upsert_static({
            "token_address": token, "network_id": network, "recorded_at": NOW,
            "token_created_at": "1600000000", "raw_json": "{}",
        })
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
    db.upsert_static({
        "token_address": "0xabc", "network_id": "8453", "recorded_at": NOW,
        "token_created_at": "1600000000", "raw_json": "{}",
    })
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
    # عمرٌ معروفٌ وحديث (2026-08-29): بوابة العمر وسقف EVM الأعلى ليسا
    # موضوع هذا الاختبار، فلا يحجبان المسار عن الإشارة. (عمر ~10 أيام.)
    db.upsert_static({
        "token_address": "0xabc", "network_id": "8453", "recorded_at": NOW,
        "token_created_at": "1786368000", "raw_json": "{}",
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
    # عمرٌ معروفٌ وقديم للمرشّحين: موضوعُ الاختبار خنقُ EVM لا بوّابةُ العمر،
    # وبلا تاريخٍ يرفضهما الضابطُ كمجهولَي العمر فيُقاس الخنق على مجموعةٍ خالية.
    for addr, net in (("control-sol", "1399811149"), ("control-evm", "8453")):
        db.upsert_static({
            "token_address": addr, "network_id": net, "recorded_at": NOW,
            "token_created_at": "1600000000", "raw_json": "{}",
        })
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
