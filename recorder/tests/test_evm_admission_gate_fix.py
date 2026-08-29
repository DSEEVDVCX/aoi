"""اختبارات إصلاح بوابة إدخال EVM (2026-08-29).

أربعة أعطاب قُيست على القاعدة الحيّة يوم 2026-08-29، وهذه الاختبارات تُثبت
إصلاحها واحدًا واحدًا:

1. وحدات العمل في `evm_admission_policy` كانت تُقدَّر بـ10,000 كتلة/نداء
   (سقف المدى) بينما القياس الفعلي للتعبئة على روبن‑هود والرأس هو مئات
   آلاف الكتل بالنداء الواحد (79,894 تحويلًا في 33 نداءً؛ 1,714 في نداء
   واحد). التقدير الخاطئ ضخم work_units ×25 فأبقى البوابة موقوفة أبدًا.
2. لا سقف عمر أعلى: عملة عمرها سنتان تجتاز بوابة العمر الدنيا ويومان
   فتدخل طابور تعبئة بملايين الكتل.
3. فحص السياسة `policy.allows` كان يُطبَّق على العملات الجديدة فقط، فإعادة
   تنشيط مراقبة منتهية تتجاوز الإيقاف كليًا.
4. `upsert_watch` كان يحذف تقدّم التعبئة (balances/backfill/replay) كله
   عند إعادة التنشيط، فتعود العملة إلى الصفر وتُبنى الطوابير بلا نهاية.
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

# الثوابت الزمنية نصوص ISO؛ هاتان الدالتان تحوّلانها إلى epoch للبذر.
from datetime import datetime  # noqa: E402

_NOW_TS = int(datetime.fromisoformat(NOW).timestamp())


def _created_days_before(ts: int, days: float) -> str:
    """timestamp لعملة سُكّت قبل `days` يومًا من اللحظة المعطاة."""
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
# 1) وحدات العمل: التقدير يجب أن يتبع المدى الفعلي المقطوع لا سقف المدى
# ---------------------------------------------------------------------------

def test_work_units_follow_measured_range_per_call(db, monkeypatch):
    """عملة بمدى متبقٍّ 3M كتلة = وحدات حسب ما يقطعه النداء فعلاً.

    القياس الحي (سجل evm.log): التعبئة تقطع مئات آلاف الكتل في النداء،
    وتقدير «سقف المدى = وحدة واحدة» يضخّم العمل ويغلق البوابة أبدًا.
    الوحدة الآن = مدى مقيس لكل نداء (`EVM_BACKFILL_BLOCKS_PER_CALL`).
    على روبن‑هود (500K/نداء): 3M كتلة = 6 وحدات فقط (كانت 300 بالسقف
    القديم 10K)؛ وعلى Base (سقفها الحقيقي 10K/نداء) تبقى 300 — الفرق
    أنّ 4663 بلا سقف مدى أصلًا فتقديرها القديم كان خاطئًا بالكامل.
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
    # 3M كتلة بـ500K/نداء = 6 وحدات فقط، لا 300 كما كان التقدير القديم
    assert state.work_units == 6
    assert state.paused is False


def test_work_units_huge_range_still_pauses(db, monkeypatch):
    """مدى بحجم سلسلة كاملة (49M) يبقى مقيَّدًا — البوابة لا تنفتح عمياء."""
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
    assert state.paused is True  # استغلال ≥4 ⇒ إيقاف


# ---------------------------------------------------------------------------
# 2) سقف العمر الأعلى لإدخال EVM
# ---------------------------------------------------------------------------

def test_evm_max_age_rejects_old_tokens(db, monkeypatch):
    """عملة EVM أقدم من السقف لا تفتح مراقبة، والإشارة تبقى محفوظة."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    monkeypatch.setattr(config, "EVM_MAX_TOKEN_AGE_DAYS", 60)
    old_created = _created_days_before(_NOW_TS, 400)  # 400 يوم
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
    assert row is None  # لم تُفتح مراقبة
    assert stats["evm_max_age_rejected"] == 1


def test_evm_max_age_allows_fresh_tokens(db, monkeypatch):
    """عملة ضمن السقف تدخل طبيعيًا — البوابة لا تُغلق الشبكة كلها."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    monkeypatch.setattr(config, "EVM_MAX_TOKEN_AGE_DAYS", 60)
    fresh_created = _created_days_before(_NOW_TS, 10)  # 10 أيام
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
    """صفر يعطّل السقف — عملة عمرها سنة تدخل كما كانت."""
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
    """مجهول العمر لا يُرفض بالسقف — بوابة العمر الدنيا تتكفل به (AGE_UNKNOWN)."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    monkeypatch.setattr(config, "EVM_MAX_TOKEN_AGE_DAYS", 60)
    token = "0x" + "5" * 40
    db.upsert_static({
        "token_address": token, "network_id": "8453", "recorded_at": NOW,
        "raw_json": "{}",
    })  # بلا token_created_at
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

    # مجهول العمر يُرفض ببوابة العمر الدنيا (age_unknown) لا بالسقف الأعلى
    assert stats["evm_max_age_rejected"] == 0
    assert stats["age_unknown"] == 1


# ---------------------------------------------------------------------------
# 3) السياسة تُطبَّق على إعادة التنشيط لا الجديد فقط
# ---------------------------------------------------------------------------

def test_reactivation_respects_paused_policy(db, monkeypatch):
    """إشارة على عملة مراقَبة منتهية لا تعيد تنشيطها والبوابة موقوفة."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    token = "0x" + "6" * 40
    # عمر 30 يومًا: ضمن سقف العمر الأعلى فيُقاس خنقُ السياسة وحده
    _seed_watch(db, token, created=_created_days_before(_NOW_TS, 30))
    # نافذة أولى انتهت
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

    # الإشارة محفوظة، والمراقبة لم تُعَد تنشيطها
    assert db._conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT active FROM watchlist WHERE token_address=?", (token,)
    ).fetchone()[0] == 0
    assert stats["evm_admission_deferred"] == 1


def test_reactivation_allowed_when_policy_open(db, monkeypatch):
    """البوابة المفتوحة تسمح بإعادة التنشيط كالمعتاد."""
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
# 4) إعادة التنشيط لا تحذف تقدّم التعبئة كله
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
    """فجوة قصيرة (≤ السقف) تحافظ على الدفتر والنقطة وتوسّع to_block فقط."""
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

    # إعادة تنشيط بعد ساعتين من انتهاء النافذة
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
    assert row[1] == 500_000          # نقطة الاستئناف لم تُصفَّر
    assert row[2] == 123              # عدّاد التحويلات محفوظ
    balances = db._conn.execute(
        "SELECT COUNT(*) FROM evm_balances WHERE token_address=?",
        (token.lower(),),
    ).fetchone()[0]
    assert balances == 1              # الدفتر لم يُمحَ


def test_reactivation_resets_ledger_long_gap(db, monkeypatch):
    """فجوة طويلة (> السقف) تعيد البناء من الصفر — سلوك الأمان القديم."""
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    token = "0x" + "a" * 40
    _seed_watch(db, token, created=_created_days_before(_NOW_TS, 30))
    db.upsert_watch(token, "8453", "large_buy", "s-old", 48, NOW)
    # النافذة الأولى انتهت 2026-08-31T12:00 (NOW+48س). إعادة تنشيط بعد
    # 5 أيام من انتهائها — أبعد من سقف الحفظ 48س فتُعاد البناء من الصفر.
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
    assert row == 0  # حُذفت كليًا — إعادة بناء من genesis


# ---------------------------------------------------------------------------
# تكامل: البوابة تُفتح فعلًا ببيانات القاعدة الحيّة (قيم 2026-08-29)
# ---------------------------------------------------------------------------

def test_live_backlog_shape_now_admits(db, monkeypatch):
    """شكل الطابور الحي (55 عملة، ~132k وحدة قديمة) يصير قابلًا للقبول.

    بالتقدير المصحَّح: 55 عملة × ~24M كتلة متبقية وسطيًا = ~4,400 وحدة
    (بـ500K/نداء على 4663) مقابل capacity 72 ⇒ لا يزال مقيدًا لكنه في
    نطاق يستنزفه النظام في أيام لا قرون — وهذا يُقاس على أيام التشغيل.
    هنا نثبت فقط أن القياس نفسه دقيق: وحدة = مدى/نداء لا مدى/سقف.
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
