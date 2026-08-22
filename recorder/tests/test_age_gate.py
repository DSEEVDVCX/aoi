"""بوّابة العمر عند الإشارة — لا نراقب عملةً أصغر من يومين لحظة الإشارة.

الغرضُ صريح: لا يُبنى بوتٌ على عملةٍ عمرُها أقلّ من يومين. ومقيس على الإشارات
المستقلّة أنّ ما دون اليومين ترتفع فيه نسبة الانهيار 29 ضعفاً ووسيطُ المردود
−46.2% مقابل −5.1%، وثمنُ إقصائه 4% من صفوف التدريب.

والرفضُ **ليس حظراً**: العملة المرفوضة لا تدخل القائمة فحسب، فإن جاءتها إشارةٌ
أخرى وهي حينها أكبر من يومين دخلت كأيّ عملة. هذه هي الحالة التي تُختبر أوّلاً.
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
DAY = 86400
NOW_TS = 1787227200          # == NOW بالثواني


@pytest.fixture()
def db(tmp_path):
    recorder._evm_admission_paused_runtime = False
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()
    recorder._evm_admission_paused_runtime = False


@pytest.fixture()
def policy():
    return recorder.EVMAdmissionPolicy(frozenset(), 0, 1, 1, False)


def _stats():
    return {"signals": 0, "watch_added": 0, "evm_admission_deferred": 0,
            "errors": 0, "age_rejected": 0, "age_unknown": 0,
            "age_resolved": 0, "age_lookup_failed": 0}


def _feed(*, token="tok", net=1399811149, event_id="e1"):
    return {"responseObject": {"data": [{
        "id": event_id, "tokenAddress": token, "networkId": net,
        "type": "large_buy", "createdAt": NOW,
        "body": {"ticker": "ABC", "price": 1.0},
    }]}}


def _age(db, token, net, *, days_old, now_ts=NOW_TS):
    db.upsert_static({
        "token_address": token, "network_id": str(net), "recorded_at": NOW,
        "token_created_at": str(int(now_ts - days_old * DAY)), "raw_json": "{}",
    })


async def _feed_cycle(db, policy, stats, raw, client=None, now=NOW):
    return await recorder.record_feed(
        db, raw, now, lambda _id: None, {}, policy, stats, client,
    )


class _AgeClient:
    """يجيب filterTokens بتاريخ إنشاء معطى. يعدّ نداءاته ليُقاس ثمنُ البوّابة."""

    def __init__(self, created=None, *, fail=False):
        self.calls = []
        self._created = created
        self._fail = fail

    async def _post(self, path, body):
        self.calls.append(body)
        if self._fail:
            raise RuntimeError("upstream boom")
        items = []
        for symbol in body:
            addr, _, net = str(symbol).partition(":")
            token = {"address": addr, "networkId": net, "symbol": "AAA"}
            if self._created is not None:
                token["createdAt"] = self._created
            items.append({"token": token, "priceUSD": 1.0})
        return {"responseObject": items}


# ---------------------------------------------------------------------------
# الحكم المجرّد على تاريخ واحد
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("days_old", "expected"),
    [
        (0.0, recorder.AGE_TOO_YOUNG),
        (1.99, recorder.AGE_TOO_YOUNG),
        (2.0, recorder.AGE_OK),          # الحدّ نفسه مقبول
        (30.0, recorder.AGE_OK),
    ],
)
def test_age_verdict_at_the_boundary(days_old, expected):
    created = str(int(NOW_TS - days_old * DAY))
    assert recorder.age_verdict(created, NOW) == expected


@pytest.mark.parametrize("created", [None, "", "abc", "0", "-5"])
def test_unreadable_creation_date_is_unknown_not_old(created):
    """الفراغُ والنصُّ غيرُ الرقميّ لا يصيران عمراً هائلاً بالغلط."""
    assert recorder.age_verdict(created, NOW) == recorder.AGE_UNKNOWN


def test_future_creation_date_is_unknown_not_old():
    """عمرٌ سالب (انحرافُ ساعةٍ أو خطأُ منبع) ليس عمراً — ولا يفتح البوّابة."""
    created = str(NOW_TS + 3 * DAY)
    assert recorder.age_verdict(created, NOW) == recorder.AGE_UNKNOWN


def test_gate_disabled_by_zero(monkeypatch):
    monkeypatch.setattr(config, "MIN_TOKEN_AGE_DAYS", 0)
    assert recorder.age_verdict(str(NOW_TS), NOW) == recorder.AGE_OK
    assert recorder.age_verdict(None, NOW) == recorder.AGE_OK


def test_epoch_seconds_are_read_as_text():
    """`token_created_at` **ثوانٍ مخزَّنةٌ نصّاً**، و`julianday()` عليها يعيد
    NULL بصمت — فالحساب في بايثون لا في SQL."""
    assert recorder.token_age_days("1787054400", NOW) == pytest.approx(2.0)
    assert recorder.token_age_days(1787054400, NOW) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# البوّابة داخل مسار القبول
# ---------------------------------------------------------------------------
async def test_young_coin_is_rejected(db, policy):
    _age(db, "tok", 1399811149, days_old=0.5)
    stats = _stats()

    await _feed_cycle(db, policy, stats, _feed())

    assert stats["signals"] == 1              # الإشارة محفوظة
    assert stats["watch_added"] == 0           # والمراقبة مرفوضة
    assert stats["age_rejected"] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


async def test_old_coin_is_admitted(db, policy):
    _age(db, "tok", 1399811149, days_old=9.0)
    stats = _stats()

    await _feed_cycle(db, policy, stats, _feed())

    assert stats["watch_added"] == 1
    assert stats["age_rejected"] == 0
    assert db._conn.execute(
        "SELECT active FROM watchlist WHERE token_address='tok'"
    ).fetchone()[0] == 1


async def test_rejection_is_not_a_ban_a_later_signal_admits(db, policy):
    """جوهرُ الطلب: المرفوضةُ لا تُحظر. نفس العملة، إشارةٌ بعد ثلاثة أيّام."""
    _age(db, "tok", 1399811149, days_old=0.5)
    first = _stats()
    await _feed_cycle(db, policy, first, _feed(event_id="e1"))
    assert first["age_rejected"] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0

    later = "2026-08-23T12:00:00+00:00"        # +3 أيّام ⇒ صارت 3.5 يوماً
    second = _stats()
    await _feed_cycle(db, policy, second, _feed(event_id="e2"), now=later)

    assert second["age_rejected"] == 0
    assert second["watch_added"] == 1
    assert db._conn.execute(
        "SELECT active FROM watchlist WHERE token_address='tok'"
    ).fetchone()[0] == 1


async def test_unknown_age_is_rejected(db, policy):
    """مجهولُ العمر يُرفض: الجهلُ لا يحمل خبراً (39.8% منها كانت < يومين مقابل
    42.4% لمعروفاتها)، فقبولُه يقبل الصغيرَ بمعدّل السكّان."""
    client = _AgeClient(created=None)          # المنبع يجيب بلا تاريخ
    stats = _stats()

    await _feed_cycle(db, policy, stats, _feed(), client=client)

    assert stats["age_rejected"] == 1
    assert stats["age_unknown"] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


async def test_active_watch_is_not_re_gated(db, policy):
    """نافذةٌ تجري لا تُحكَم ثانيةً — البوّابةُ تحكم فتحَ النوافذ لا استمرارَها.

    عملةٌ دخلت وهي قديمة ثمّ جاءتها إشارةٌ ثانية: لا يجوز أن يُسقطها غيابُ
    تاريخٍ أو تبدُّلُه في المنبع.
    """
    db.upsert_watch("tok", "1399811149", "large_buy", "s0", 48, NOW)
    stats = _stats()

    await _feed_cycle(db, policy, stats, _feed(event_id="e2"))

    assert stats["age_rejected"] == 0
    assert db._conn.execute(
        "SELECT active FROM watchlist WHERE token_address='tok'"
    ).fetchone()[0] == 1


async def test_reactivation_of_an_expired_row_is_gated(db, policy):
    """`upsert_watch` يُحيي صفّاً منتهياً بنافذةٍ جديدة — فلو حُصِرت البوّابة في
    الصفّ الجديد لدخلت الصغيرةُ من باب الإحياء."""
    db.upsert_watch("tok", "1399811149", "large_buy", "s0", 48, NOW)
    db._conn.execute("UPDATE watchlist SET active=0 WHERE token_address='tok'")
    db._conn.commit()
    _age(db, "tok", 1399811149, days_old=0.25)
    stats = _stats()

    await _feed_cycle(db, policy, stats, _feed(event_id="e2"))

    assert stats["age_rejected"] == 1
    assert db._conn.execute(
        "SELECT active FROM watchlist WHERE token_address='tok'"
    ).fetchone()[0] == 0


async def test_non_trigger_signal_is_never_gated(db, policy):
    """البوّابةُ على المُشغّلات وحدها (multi_user_buy / large_buy)."""
    raw = {"responseObject": {"data": [{
        "id": "e1", "tokenAddress": "tok", "networkId": 1399811149,
        "type": "comment", "createdAt": NOW, "body": {"ticker": "ABC"},
    }]}}
    stats = _stats()

    await _feed_cycle(db, policy, stats, raw)

    assert stats["age_rejected"] == 0
    assert stats["watch_added"] == 0


# ---------------------------------------------------------------------------
# مصدر العمر: نداءٌ واحد، ويُخزَّن فلا يُعاد
# ---------------------------------------------------------------------------
async def test_age_is_fetched_once_then_served_from_storage(db, policy):
    """الإشارةُ لا تحمل عمرَ العملة (حدثُ الـfeed فيه العنوانُ والشبكةُ وتاريخُ
    المنشور وحده)، فأوّلُ لقاءٍ ينادي المنبع — والثاني لا ينادي."""
    client = _AgeClient(created=int(NOW_TS - 9 * DAY))
    stats = _stats()

    await _feed_cycle(db, policy, stats, _feed(event_id="e1"), client=client)

    assert client.calls == [["tok:1399811149"]]
    assert stats["age_resolved"] == 1
    assert stats["watch_added"] == 1
    stored = db._conn.execute(
        "SELECT token_created_at FROM token_static WHERE token_address='tok'"
    ).fetchone()[0]
    assert stored == str(int(NOW_TS - 9 * DAY))

    # إشارةٌ ثانية على العملة نفسها بعد انتهاء نافذتها: لا نداءَ ثانياً.
    db._conn.execute("UPDATE watchlist SET active=0 WHERE token_address='tok'")
    db._conn.commit()
    await _feed_cycle(db, policy, _stats(), _feed(event_id="e2"), client=client)

    assert len(client.calls) == 1


async def test_known_age_costs_no_call(db, policy):
    _age(db, "tok", 1399811149, days_old=9.0)
    client = _AgeClient(created=int(NOW_TS - 9 * DAY))

    await _feed_cycle(db, policy, _stats(), _feed(), client=client)

    assert client.calls == []


async def test_one_call_covers_every_candidate_in_the_cycle(db, policy):
    """المرشّحون الجدد 1.11 في الدورة (أقصى ما رُصد 4) والدفعة تحمل 150 —
    فنداءٌ واحدٌ يكفي، مقابل تسعة نداءات شموع في الدورة نفسها."""
    raw = {"responseObject": {"data": [
        {"id": f"e{i}", "tokenAddress": f"tok{i}", "networkId": 1399811149,
         "type": "large_buy", "createdAt": NOW, "body": {"ticker": "ABC"}}
        for i in range(4)
    ]}}
    client = _AgeClient(created=int(NOW_TS - 9 * DAY))

    await _feed_cycle(db, policy, _stats(), raw, client=client)

    assert len(client.calls) == 1
    assert sorted(client.calls[0]) == [f"tok{i}:1399811149" for i in range(4)]


async def test_lookup_failure_rejects_but_stays_audible(db, policy):
    """502 من المنبع يجعل كلّ مرشّحٍ «مجهولاً» فتتوقّف المراقبة كلّها — انقطاعٌ
    يجب أن يُسمع لا أن يُقرأ ترشيحاً عادياً: ختمُ خطأٍ وسلسلةٌ في `meta`."""
    client = _AgeClient(fail=True)
    stats = _stats()

    await _feed_cycle(db, policy, stats, _feed(), client=client)

    assert stats["age_lookup_failed"] == 1
    assert stats["age_rejected"] == 1                    # البوّابة لا تُسرّب
    assert db.get_meta("age_lookup_failed_streak") == "1"
    assert "upstream boom" in (db.get_meta("last_error_age_lookup") or "")
    assert db._conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


async def test_successful_lookup_clears_the_failure_streak(db, policy):
    failing = _AgeClient(fail=True)
    await _feed_cycle(db, policy, _stats(), _feed(event_id="e1"), client=failing)
    assert db.get_meta("age_lookup_failed_streak") == "1"

    ok = _AgeClient(created=int(NOW_TS - 9 * DAY))
    await _feed_cycle(db, policy, _stats(), _feed(event_id="e2"), client=ok)

    assert db.get_meta("age_lookup_failed_streak") == "0"
    assert db.get_meta("age_lookup_last_ok_at") == NOW


async def test_dead_address_dropped_by_provider_stays_unknown(db, policy):
    """المنبعُ يحذف العنوانَ الميّت بصمت — فيبقى مجهولاً ويُرفض، ولا يُخترع له
    عمرٌ من عنصرٍ آخر في الدفعة."""
    class _Empty(_AgeClient):
        async def _post(self, path, body):
            self.calls.append(body)
            return {"responseObject": []}

    client = _Empty()
    stats = _stats()

    await _feed_cycle(db, policy, stats, _feed(), client=client)

    assert stats["age_resolved"] == 0
    assert stats["age_rejected"] == 1
    assert stats["age_unknown"] == 1


async def test_rejected_coins_are_counted_cumulatively(db, policy):
    _age(db, "tok", 1399811149, days_old=0.5)
    stats = _stats()

    await _feed_cycle(db, policy, stats, _feed())

    assert db.get_meta("age_rejected_total") == "1"
