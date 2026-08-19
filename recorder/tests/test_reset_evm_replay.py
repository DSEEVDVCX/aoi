"""اختبارات أداة الإرجاع الانتقائيّ: ما تمسّه، وما ترفض مسّه.

الخطر هنا ليس عطباً حسابيّاً بل حذفاً زائداً: الأداة تُنفَّذ يدويّاً على قاعدة
التدريب الحيّة، فالمفحوص هو الحدود — لقطةٌ حيّة لا تُمسّ، وعملةٌ منجَزة لا تُمحى،
وشبكةٌ أخرى لا تُصاب، والعرضُ لا يكتب.
"""
import os

import pytest
import reset_evm_replay
from db import RecorderDB

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-13T12:00:00+00:00"
NET = "8453"
OTHER = "4663"
TOK = "0xaaaa000000000000000000000000000000000001"
TOK2 = "0xaaaa000000000000000000000000000000000002"


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()


def _row(token, net, *, replay):
    return {
        "token_address": token, "network_id": net, "recorded_at": NOW,
        "watch_first_seen_at": NOW, "is_control": 0, "top1_pct": 100.0,
        "is_replay": 1 if replay else 0, "raw_json": {},
    }


def _seed(db, token, net, status, *, calls=0, replay_rows=0, live_rows=0):
    db.upsert_watch(token, net, "trending", "sig", 48, NOW)
    db.set_evm_replay_state(token, net, status, NOW, calls=calls)
    for index in range(replay_rows):
        assert db.insert_chain_concentration({
            **_row(token, net, replay=True),
            "recorded_at": f"2026-08-13T10:{index:02d}:00+00:00",
        })
    for index in range(live_rows):
        assert db.insert_chain_concentration({
            **_row(token, net, replay=False),
            "recorded_at": f"2026-08-13T11:{index:02d}:00+00:00",
        })


def _seed_orphan(db, token, net, rows):
    """صفوفُ إعادةٍ بلا `evm_replay_state` — هذا ما يخلّفه مسارٌ حُذف بعد الكتابة."""
    db.upsert_watch(token, net, "trending", "sig", 48, NOW)
    for index in range(rows):
        assert db.insert_chain_concentration({
            **_row(token, net, replay=True),
            "recorded_at": f"2026-08-13T09:{index:02d}:00+00:00",
        })


def _count(db, net, replay):
    return db._conn.execute(
        """SELECT COUNT(*) FROM chain_concentration
            WHERE network_id=? AND is_replay=?""",
        (net, 1 if replay else 0),
    ).fetchone()[0]


def test_candidates_match_status_and_network_only(db):
    _seed(db, TOK, NET, "error", calls=240)
    _seed(db, TOK2, NET, "done", calls=99)
    _seed(db, TOK, OTHER, "error", calls=7)

    found = reset_evm_replay.candidates(db, (NET,), ("error",))

    assert [(r["token_address"], r["network_id"]) for r in found] == [(TOK, NET)]
    assert found[0]["calls"] == 240


def test_candidates_are_ordered_by_calls_spent(db):
    """الأثقلُ نداءً أوّلاً: هو الأجدر بأن يُقرأ سطرُه قبل الموافقة على المحو."""
    _seed(db, TOK, NET, "partial", calls=5)
    _seed(db, TOK2, NET, "partial", calls=5_660)

    found = reset_evm_replay.candidates(db, (NET,), ("partial",))

    assert [r["token_address"] for r in found] == [TOK2, TOK]


def test_candidates_count_the_replay_rows_that_a_reset_would_delete(db):
    """العدد يُعرَض قبل `--apply` لأنّه الكلفة الوحيدة غير القابلة للاسترداد."""
    _seed(db, TOK, NET, "partial", replay_rows=3, live_rows=2)

    found = reset_evm_replay.candidates(db, (NET,), ("partial",))

    assert found[0]["rows_written"] == 3      # الحيّة ليست من شأن هذه الأداة


def test_reset_returns_the_token_to_the_queue_and_leaves_live_rows(db):
    _seed(db, TOK, NET, "error", calls=240, replay_rows=3, live_rows=2)

    done = reset_evm_replay.reset(
        db, reset_evm_replay.candidates(db, (NET,), ("error",)),
    )

    assert done == {"tokens": 1, "rows_deleted": 3, "calls_freed": 240}
    # حذفُ صفّ الحالة **هو** الإرجاع: الحالة مشتقّة، وغيابُها يعني «لم تُعَد بعد».
    assert db.evm_replay_state(TOK, NET) is None
    assert _count(db, NET, replay=True) == 0
    assert _count(db, NET, replay=False) == 2


def test_reset_does_not_touch_another_network_on_the_same_token(db):
    """السبب الذي من أجله وُجدت الأداة: مسارٌ فُصل عن شبكةٍ واحدة لا عن الدفتر."""
    _seed(db, TOK, NET, "error", replay_rows=2)
    _seed(db, TOK, OTHER, "done", replay_rows=5)

    reset_evm_replay.reset(
        db, reset_evm_replay.candidates(db, (NET,), ("error",)),
    )

    assert db.evm_replay_state(TOK, OTHER)["status"] == "done"
    assert _count(db, OTHER, replay=True) == 5


def test_a_done_token_is_refused_not_silently_skipped(db, monkeypatch):
    """`done` عملٌ منجَز: محوُه يُسقط لقطاتٍ لا تُستعاد إلّا بمشيٍ كامل.

    والرفضُ برمز خروج لا بتجاهلٍ صامت: مشغّلٌ كتب `--status done --apply` وقرأ
    «مطابق: 0» يظنّ الشبكة نظيفة، فيبحث عن العطب في مكانٍ سليم.
    """
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET, "--status", "done", "--apply",
    ])
    assert reset_evm_replay.main() == 2


def test_an_unknown_network_is_refused_before_any_write(db, monkeypatch):
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", "999", "--apply",
    ])
    assert reset_evm_replay.main() == 2


def test_the_default_run_writes_nothing(db, tmp_path, monkeypatch, capsys):
    """العرضُ افتراضٌ: أداةٌ يدويّة على قاعدةٍ حيّة لا تُنفّذ بالنسيان."""
    _seed(db, TOK, NET, "error", calls=240, replay_rows=3)
    db._conn.commit()
    monkeypatch.setattr(
        reset_evm_replay.config, "DB_PATH", str(tmp_path / "t.db"),
    )
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET,
    ])

    assert reset_evm_replay.main() == 0

    assert "عرضٌ فقط" in capsys.readouterr().out
    assert db.evm_replay_state(TOK, NET)["status"] == "error"
    assert _count(db, NET, replay=True) == 3


def test_apply_resets_only_the_first_max_tokens(db, tmp_path, monkeypatch, capsys):
    """سقفٌ للدفعة: 76 عملة تُرجَع دفعةً واحدة تشتري لنفسها كلَّ ميزانيّة الدورة."""
    _seed(db, TOK, NET, "error", calls=240)
    _seed(db, TOK2, NET, "error", calls=10)
    db._conn.commit()
    monkeypatch.setattr(
        reset_evm_replay.config, "DB_PATH", str(tmp_path / "t.db"),
    )
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET, "--max", "1", "--apply",
    ])

    assert reset_evm_replay.main() == 0

    assert "أُرجعت 1 عملة" in capsys.readouterr().out
    assert db.evm_replay_state(TOK, NET) is None            # الأثقل نداءً أوّلاً
    assert db.evm_replay_state(TOK2, NET)["status"] == "error"


def test_an_orphan_is_invisible_to_candidates_which_is_why_orphans_exists(db):
    """السببُ الذي من أجله وُجدت `orphans`: `--status` لا يمسك ما لا حالةَ له."""
    _seed_orphan(db, TOK, NET, 4)

    assert reset_evm_replay.candidates(db, (NET,), ("error", "partial", "")) == []
    found = reset_evm_replay.orphans(db, (NET,))
    assert [(r["token_address"], r["rows_written"]) for r in found] == [(TOK, 4)]


def test_orphans_ignores_live_rows_and_other_networks(db):
    """اللقطةُ الحيّة ليست يتيمة: هي مقيسةٌ لحظتها ولا حالةَ إعادةٍ تُنتظَر لها."""
    _seed_orphan(db, TOK, NET, 2)
    _seed(db, TOK2, NET, "partial", replay_rows=3, live_rows=5)   # له حالة ⇒ ليس يتيماً
    _seed_orphan(db, TOK, OTHER, 7)

    found = reset_evm_replay.orphans(db, (NET,))

    assert [r["token_address"] for r in found] == [TOK]
    assert found[0]["rows_written"] == 2


def test_orphans_carry_the_candidate_shape_so_one_printer_serves_both(db):
    """`_describe` تقرأ الحقولَ بلا حراسة، فحقلٌ ناقصٌ هنا يُسقط تشغيلاً كاملاً."""
    _seed_orphan(db, TOK, NET, 3)
    _seed(db, TOK2, NET, "error", replay_rows=2)

    stray = reset_evm_replay.orphans(db, (NET,))
    picked = reset_evm_replay.candidates(db, (NET,), ("error",))

    assert set(stray[0]) == set(picked[0])
    assert reset_evm_replay._describe(stray)          # لا KeyError ولا None في حساب


def test_an_orphan_is_reported_even_when_not_requested(db, tmp_path, monkeypatch, capsys):
    """الإظهارُ إفادةٌ لا يكلّف شيئاً: يتيمٌ صامتٌ يبقى في الدفتر إلى الأبد."""
    _seed_orphan(db, TOK, NET, 6)
    db._conn.commit()
    monkeypatch.setattr(reset_evm_replay.config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET,
    ])

    assert reset_evm_replay.main() == 0

    out = capsys.readouterr().out
    assert "يتامى (خبرٌ فقط، لا تُحذف): 1 عملة · 6 صفّاً" in out
    assert "audit_evm_ledger.py" in out              # الطريقُ إلى الحكم
    assert _count(db, NET, replay=True) == 6         # خبرٌ لا حذف


def test_no_flag_deletes_an_orphan_because_its_rows_are_the_measured_part(db,
        tmp_path, monkeypatch, capsys):
    """اليُتمُ فقدُ حالةٍ لا فسادُ صفوف؛ ومصدرُه مسارٌ مقصود (`db.admit`).

    كان هناك `--orphans` يشملها في الحذف، وكان ذلك خطأً: `audit_evm_ledger.py`
    قرأ 77 لقطةً على 4663 و8453 فجاءت تغطيتُها 100.000000% — فالحذفُ كان
    سيُتلف بياناتِ تدريبٍ مقيسةً صحيحة ويُلزم مشياً جديداً. وما ينقصها صفُّ
    حالةٍ يكتبه المشيُ التالي من نفسه.
    """
    _seed_orphan(db, TOK, NET, 6)
    db._conn.commit()
    monkeypatch.setattr(reset_evm_replay.config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET, "--status", "error", "--apply",
    ])

    assert reset_evm_replay.main() == 0
    assert _count(db, NET, replay=True) == 6

    # والحجّةُ نفسُها مُزالة: لا بابَ خلفيّ يعيدها بلا مراجعة
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET, "--orphans", "--apply",
    ])
    with pytest.raises(SystemExit):
        reset_evm_replay.main()
    assert _count(db, NET, replay=True) == 6
