"""بوّابة العمر على الذراع الضابطة — الذراعان بقاعدةٍ واحدة أو لا مقارنة.

البوّابةُ وُضعت أوّلاً على مسار الإشارة وحده، فانفرق الذراعان في المتغيّر الأقوى
أثراً: 1.5% من نوافذ الإشارة لعملةٍ دون يومين مقابل 50% من نوافذ الضابط. وأثرٌ
يُقاس على ذراعين كهذين أثرُ عمرٍ لا أثرُ إشارة.
"""
import os

import config
import pytest
from db import RecorderDB

import recorder

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-20T12:00:00+00:00"
NOW_TS = 1787227200
DAY = 86400
SOL = "1399811149"


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()


def _age(db, token, *, days_old, net=SOL):
    db.upsert_static({
        "token_address": token, "network_id": net, "recorded_at": NOW,
        "token_created_at": str(int(NOW_TS - days_old * DAY)), "raw_json": "{}",
    })


def _signal_watch(db, token="signal-tok", net=SOL):
    """الضابطة لا تُطلب إلّا وهناك إشارة — والأوزان تُقرأ من شبكات الإشارة."""
    db.upsert_watch(token, net, "large_buy", "s1", 48, NOW)


def _controls(db):
    return [
        r[0] for r in db._conn.execute(
            "SELECT token_address FROM watchlist WHERE is_control=1"
        )
    ]


def test_young_control_candidate_is_rejected(db):
    _signal_watch(db)
    _age(db, "young", days_old=0.5)

    added = recorder.admit_control_sample(db, [("young", SOL, 1.0)], NOW)

    assert added == 0
    assert _controls(db) == []
    assert db.get_meta("control_age_rejected_total") == "1"


def test_old_control_candidate_is_admitted(db):
    _signal_watch(db)
    _age(db, "old", days_old=9.0)

    added = recorder.admit_control_sample(db, [("old", SOL, 1.0)], NOW)

    assert added == 1
    assert _controls(db) == ["old"]


def test_unknown_age_control_candidate_is_rejected(db):
    """نفس القاعدة لا قاعدةٌ ألطف: ثمنُها 12 من 1,212 عملة (0.99%)."""
    _signal_watch(db)

    added = recorder.admit_control_sample(db, [("nostatic", SOL, 1.0)], NOW)

    assert added == 0
    assert _controls(db) == []


def test_both_arms_apply_the_same_rule(db):
    """جوهرُ الإصلاح: عملةٌ واحدة تُرفض في المسارين، لا في واحدٍ منهما."""
    _age(db, "young", days_old=0.5)
    stats = {"signals": 0, "watch_added": 0, "evm_admission_deferred": 0,
             "errors": 0, "age_rejected": 0, "age_unknown": 0,
             "age_resolved": 0, "age_lookup_failed": 0}
    raw = {"responseObject": {"data": [{
        "id": "e1", "tokenAddress": "young", "networkId": int(SOL),
        "type": "large_buy", "createdAt": NOW,
        "body": {"ticker": "ABC", "price": 1.0},
    }]}}
    import asyncio
    asyncio.run(recorder.record_feed(
        db, raw, NOW, lambda _id: None, {},
        recorder.EVMAdmissionPolicy(frozenset(), 0, 1, 1, False), stats,
    ))
    assert stats["age_rejected"] == 1          # مرفوضة كإشارة

    _signal_watch(db, "other")
    assert recorder.admit_control_sample(db, [("young", SOL, 1.0)], NOW) == 0


def test_gate_disabled_by_zero_lets_the_control_arm_through(monkeypatch, db):
    monkeypatch.setattr(config, "MIN_TOKEN_AGE_DAYS", 0)
    _signal_watch(db)
    _age(db, "young", days_old=0.1)

    assert recorder.admit_control_sample(db, [("young", SOL, 1.0)], NOW) == 1


def test_rejection_is_counted_in_cycle_stats(db):
    _signal_watch(db)
    _age(db, "young", days_old=0.5)
    stats = {"control_age_rejected": 0}

    recorder.admit_control_sample(db, [("young", SOL, 1.0)], NOW, stats=stats)

    assert stats["control_age_rejected"] == 1


def test_network_weighting_survives_the_gate(db):
    """البوّابة قبل الترجيح: إسقاطُ الصغيرة لا يُنقص نصيبَ شبكتها بلا إعادة وزن.

    شبكةُ الإشارة سولانا، والمرشّحون: صغيرةٌ على سولانا وقديمةٌ على سولانا.
    الناتج يجب أن يكون القديمةَ — لا صفراً لأنّ الاختيار وقع على الصغيرة.
    """
    _signal_watch(db)
    _age(db, "young", days_old=0.5)
    _age(db, "old", days_old=9.0)

    added = recorder.admit_control_sample(
        db, [("young", SOL, 1.0), ("old", SOL, 1.0)], NOW,
    )

    assert added == 1
    assert _controls(db) == ["old"]


def test_control_gate_uses_creation_age_from_current_candidate(db):
    _signal_watch(db)
    db.add_signal_comparison_window(
        "signal-tok", SOL, "large_buy", "s1", 48, NOW,
        1.0, config.CONTROL_DESIGN_VERSION, "trending",
    )
    added = recorder.admit_control_sample(
        db,
        [("current", SOL, 1.0, "trending", str(NOW_TS - 9 * DAY))],
        NOW,
    )

    assert added == 1
    assert _controls(db) == ["current"]


def test_admitted_control_persists_current_static_item(db):
    _signal_watch(db)
    db.add_signal_comparison_window(
        "signal-tok", SOL, "large_buy", "s1", 48, NOW,
        1.0, config.CONTROL_DESIGN_VERSION, "trending",
    )
    item = {
        "token": {
            "address": "current",
            "networkId": SOL,
            "symbol": "CUR",
            "createdAt": str(NOW_TS - 9 * DAY),
        },
        "priceUSD": 1.0,
    }

    added = recorder.admit_control_sample(
        db,
        [("current", SOL, 1.0, "trending", str(NOW_TS - 9 * DAY))],
        NOW,
        static_items={("current", SOL): item},
    )

    assert added == 1
    row = db._conn.execute(
        "SELECT token_created_at, symbol FROM token_static WHERE token_address='current'"
    ).fetchone()
    assert row["token_created_at"] == str(NOW_TS - 9 * DAY)
    assert row["symbol"] == "CUR"


def test_control_rejects_age_without_observation_timestamp(db, monkeypatch):
    monkeypatch.setattr(config, "AGE_GATE_ENABLED_AT", NOW)
    _signal_watch(db)
    db.add_signal_comparison_window(
        "signal-tok", SOL, "large_buy", "s1", 48, NOW,
        1.0, config.CONTROL_DESIGN_VERSION, "trending",
    )
    db.upsert_static({
        "token_address": "unobserved-old", "network_id": SOL,
        "recorded_at": NOW, "token_created_at": str(NOW_TS - 9 * DAY),
        "token_created_at_observed_at": None, "raw_json": "{}",
    })
    db._conn.execute(
        "UPDATE token_static SET token_created_at_observed_at=NULL "
        "WHERE token_address='unobserved-old'"
    )
    db._conn.commit()

    assert recorder.admit_control_sample(
        db, [("unobserved-old", SOL, 1.0, "trending")], NOW,
    ) == 0


def test_current_candidate_stamps_an_unobserved_stored_age(db):
    _signal_watch(db)
    db.add_signal_comparison_window(
        "signal-tok", SOL, "large_buy", "s1", 48, NOW,
        1.0, config.CONTROL_DESIGN_VERSION, "trending",
    )
    created = str(NOW_TS - 9 * DAY)
    db.upsert_static({
        "token_address": "stamp-me", "network_id": SOL,
        "recorded_at": NOW, "token_created_at": created, "raw_json": "{}",
    })
    db._conn.execute(
        "UPDATE token_static SET token_created_at_observed_at=NULL "
        "WHERE token_address='stamp-me'"
    )
    db._conn.commit()
    item = {
        "token": {
            "address": "stamp-me", "networkId": SOL,
            "createdAt": created, "symbol": "STAMP",
        },
        "priceUSD": 1.0,
    }

    assert recorder.admit_control_sample(
        db, [("stamp-me", SOL, 1.0, "trending", created)], NOW,
        static_items={("stamp-me", SOL): item},
    ) == 1
    row = db._conn.execute(
        "SELECT token_created_at, token_created_at_observed_at "
        "FROM token_static WHERE token_address='stamp-me'"
    ).fetchone()
    assert row["token_created_at"] == created
    assert row["token_created_at_observed_at"] == NOW


def test_changed_candidate_replaces_an_unproven_stored_age(db):
    _signal_watch(db)
    db.add_signal_comparison_window(
        "signal-tok", SOL, "large_buy", "s1", 48, NOW,
        1.0, config.CONTROL_DESIGN_VERSION, "trending",
    )
    stored = str(NOW_TS - 9 * DAY)
    current = str(NOW_TS - 8 * DAY)
    db.upsert_static({
        "token_address": "changed-age", "network_id": SOL,
        "recorded_at": NOW, "token_created_at": stored, "raw_json": "{}",
    })
    db._conn.execute(
        "UPDATE token_static SET token_created_at_observed_at=NULL "
        "WHERE token_address='changed-age'"
    )
    db._conn.commit()
    item = {
        "token": {
            "address": "changed-age", "networkId": SOL,
            "createdAt": current, "symbol": "CHANGED",
        },
        "priceUSD": 1.0,
    }

    assert recorder.admit_control_sample(
        db, [("changed-age", SOL, 1.0, "trending", current)], NOW,
        static_items={("changed-age", SOL): item},
    ) == 1
    row = db._conn.execute(
        "SELECT token_created_at, token_created_at_observed_at "
        "FROM token_static WHERE token_address='changed-age'"
    ).fetchone()
    assert row["token_created_at"] == current
    assert row["token_created_at_observed_at"] == NOW


def test_observation_stamp_is_conditional_on_the_value_read(db):
    db.upsert_static({
        "token_address": "raced-age", "network_id": SOL,
        "recorded_at": NOW, "token_created_at": "1000000000", "raw_json": "{}",
    })
    db._conn.execute(
        "UPDATE token_static SET token_created_at='1100000000', "
        "token_created_at_observed_at=NULL WHERE token_address='raced-age'"
    )
    db._conn.commit()

    assert not db.observe_static_created_at(
        "raced-age", SOL, "1000000000", NOW,
    )
    row = db._conn.execute(
        "SELECT token_created_at, token_created_at_observed_at "
        "FROM token_static WHERE token_address='raced-age'"
    ).fetchone()
    assert row["token_created_at"] == "1100000000"
    assert row["token_created_at_observed_at"] is None
