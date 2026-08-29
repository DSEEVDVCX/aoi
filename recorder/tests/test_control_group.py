"""اختبارات المجموعة الضابطة (الصنف السالب).

تثبت الخصائص التي تجعل المقارنة صالحة علمياً: الاختيار عشوائيّ لا ترتيبيّ،
لا يلمس ما أُشير إليه، لا يُخفّض عملة مُشار إليها، ويُقسَّط على الزمن.
"""
import math
import os
import random
from datetime import datetime

import config
import pytest
from db import RecorderDB

import recorder

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-07-27T00:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _cands(n, net="56"):
    return [(f"tok{i:03d}", net) for i in range(n)]


def _age_all(target, n=300, nets=("56", "1399811149", "8453", "4663")):
    """يمنح كلّ مرشّحٍ محتمل عمراً **معروفاً وقديماً** (ثلاثون يوماً).

    موضوعُ هذا الملفّ ميكانيكا الاختيار — عشوائيّته، مزيجُ شبكاته، استبعادُ
    المُشار إليه، شرطُ السعر — لا العمر؛ وبوّابةُ العمر تُختبر وحدها في
    `test_control_age_gate.py`. وبلا تاريخِ إنشاءٍ يرفض الضابطُ كلَّ مرشّحٍ
    كمجهولِ العمر، فتُقاس الميكانيكا على مجموعةٍ خالية وتنجح بلا معنى.
    """
    created = str(int(datetime.fromisoformat(NOW).timestamp()) - 30 * 86400)
    names = [f"tok{i:03d}" for i in range(n)]
    # مرشّحون بأسماءٍ صريحة في اختباراتٍ بعينها — لا يلتقطها نمطُ `tok###`.
    names += ["control", "tokA", "tokNew", "bad", "priced", "valid",
              "base", "base0", "c1", "s1"]
    with target.batch():
        for net in nets:
            for name in names:
                target.upsert_static({
                    "token_address": name, "network_id": net,
                    "recorded_at": NOW, "token_created_at": created,
                    "raw_json": "{}",
                })


@pytest.fixture(autouse=True)
def _known_old_ages(db):
    _age_all(db)


def test_control_rows_are_marked_and_counted_separately(db):
    assert db.admit_control("c1", "56", 48, NOW) is True
    db.upsert_watch("s1", "56", "large_buy", "sig1", 48, NOW)

    assert db.active_watch_count() == 2
    assert db.active_watch_count(is_control=1) == 1
    assert db.active_watch_count(is_control=0) == 1
    row = db._conn.execute("SELECT * FROM watchlist WHERE token_address='c1'").fetchone()
    assert row["source"] == "control"
    assert row["entry_signal_id"] is None
    assert row["is_control"] == 1


def test_control_admission_is_idempotent(db):
    assert db.admit_control("c1", "56", 48, NOW) is True
    assert db.admit_control("c1", "56", 48, "2026-07-27T06:00:00+00:00") is False
    row = db._conn.execute("SELECT first_seen_at FROM watchlist").fetchone()
    assert row["first_seen_at"] == NOW          # النافذة لا تُعاد ضبطها


def test_signal_promotes_a_control_coin_without_erasing_control_window(db):
    """ترقية العملة لا تمحو نافذة الضابطة الأصلية من سجل المقارنة."""
    db.admit_control("c1", "56", 48, NOW)
    promoted = db.upsert_watch("c1", "56", "large_buy", "sig9", 48, "2026-07-27T05:00:00+00:00")

    assert promoted is True
    row = db._conn.execute("SELECT * FROM watchlist WHERE token_address='c1'").fetchone()
    assert row["is_control"] == 0
    assert row["source"] == "large_buy"
    assert row["entry_signal_id"] == "sig9"
    assert row["first_seen_at"].startswith("2026-07-27T05:00:00")
    assert db.active_watch_count(is_control=1) == 0
    windows = db._conn.execute(
        "SELECT source, is_control, first_seen_at, design_version FROM watch_windows "
        "WHERE token_address='c1' ORDER BY first_seen_at"
    ).fetchall()
    assert [(w["source"], w["is_control"]) for w in windows] == [
        ("control", 1),
        ("large_buy", 0),
    ]
    assert [w["design_version"] for w in windows] == [2, 2]


def test_reactivated_signal_creates_a_second_immutable_window(db):
    db.upsert_watch("c1", "56", "large_buy", "sig1", 48, NOW)
    db.set_bars_state("c1", "56", "ok", 3, "2026-07-29T00:00:00+00:00")
    db.deactivate_expired("2026-07-30T00:00:00+00:00")
    assert db.upsert_watch(
        "c1", "56", "large_buy", "sig2", 48, "2026-07-30T01:00:00+00:00"
    ) is True

    windows = db._conn.execute(
        "SELECT entry_signal_id, first_seen_at FROM watch_windows "
        "WHERE token_address='c1' ORDER BY first_seen_at"
    ).fetchall()
    assert [(w["entry_signal_id"], w["first_seen_at"]) for w in windows] == [
        ("sig1", NOW),
        ("sig2", "2026-07-30T01:00:00+00:00"),
    ]


def test_control_never_downgrades_a_signalled_coin(db):
    """الاتجاه المعاكس ممنوع: المُشار إليها لا تصير ضابطة أبداً."""
    db.upsert_watch("s1", "56", "large_buy", "sig1", 48, NOW)
    assert db.admit_control("s1", "56", 48, "2026-07-27T05:00:00+00:00") is False
    row = db._conn.execute("SELECT * FROM watchlist WHERE token_address='s1'").fetchone()
    assert row["is_control"] == 0
    assert row["entry_signal_id"] == "sig1"


def test_sample_is_capped_per_cycle_not_taken_all_at_once(db):
    """التقسيط يمنع أن تكون العيّنة كلّها من لحظة سوقية واحدة."""
    added = recorder.admit_control_sample(db, _cands(50), NOW, rng=random.Random(0))
    assert added == config.CONTROL_PER_CYCLE
    assert db.active_watch_count(is_control=1) == config.CONTROL_PER_CYCLE


def test_sample_stops_at_target_size(db):
    rng = random.Random(1)
    for _ in range(200):
        recorder.admit_control_sample(db, _cands(300), NOW, rng=rng)
        if db.active_watch_count(is_control=1) >= config.CONTROL_GROUP_SIZE:
            break
    assert db.active_watch_count(is_control=1) == config.CONTROL_GROUP_SIZE
    # بلغ الهدف → لا إدخال إضافي
    assert recorder.admit_control_sample(db, _cands(300), NOW, rng=rng) == 0


def test_sample_excludes_signalled_tokens_even_if_not_watched(db):
    """عملة ورد عليها حدث إشارة لا تصلح ضابطة، ولو لم تدخل المراقبة."""
    db.insert_signal({
        "id": "s1", "token_address": "tok000", "network_id": "56", "ts": NOW,
        "recorded_at": NOW, "signal_type": "large_buy", "ticker": None,
        "price_usd": None, "fdv": None, "market_cap": None, "num_trades": None,
        "unique_traders": None, "minutes": None, "price_change_pct": None,
        "total_volume": None, "are_top_traders": None, "top_trader_ids_json": "[]",
        "top_trader_match_count": None, "buyers_best_rank": None, "buyer_id": None,
        "buyer_handle": None, "num_swaps": None, "is_first_buy": None,
        "buyer_pnl_pct": None, "avg_cost": None, "raw_json": "{}",
    })
    for _ in range(60):
        recorder.admit_control_sample(db, _cands(3), NOW, rng=random.Random(7))
    picked = {r["token_address"] for r in db._conn.execute(
        "SELECT token_address FROM watchlist WHERE is_control=1")}
    assert "tok000" not in picked
    assert picked <= {"tok001", "tok002"}


def test_sample_excludes_already_known_tokens(db):
    db.upsert_watch("tok001", "56", "large_buy", "s", 48, NOW)
    db.admit_control("tok002", "56", 48, NOW)
    for _ in range(20):
        recorder.admit_control_sample(db, _cands(3), NOW, rng=random.Random(3))
    rows = db._conn.execute(
        "SELECT token_address, is_control FROM watchlist ORDER BY token_address").fetchall()
    assert {r["token_address"] for r in rows} == {"tok000", "tok001", "tok002"}
    assert dict((r["token_address"], r["is_control"]) for r in rows)["tok001"] == 0


def test_sample_is_random_not_positional(db, tmp_path):
    """الأخذ من رأس قائمة الرواج يختار الأعلى حجماً، فيصير الفرق فرقَ حجمٍ
    لا فرقَ إشارة. نتحقّق أنّ بذوراً مختلفة تعطي اختيارات مختلفة."""
    picks = []
    for seed in range(6):
        d = RecorderDB(str(tmp_path / f"s{seed}.db"), SCHEMA)
        _age_all(d, 80)          # قواعد خاصّة بهذا الاختبار ⇒ تعمير صريح
        recorder.admit_control_sample(d, _cands(80), NOW, rng=random.Random(seed))
        picks.append(tuple(sorted(
            r["token_address"] for r in d._conn.execute(
                "SELECT token_address FROM watchlist"))))
        d.close()
    assert len(set(picks)) > 1                     # ليست دائماً نفس العملات
    heads = {p for p in picks if p and p[0] == "tok000"}
    assert len(heads) < len(picks)                 # ليست دائماً رأس القائمة


def test_no_candidates_or_target_met_is_a_noop(db):
    assert recorder.admit_control_sample(db, [], NOW) == 0


def test_control_sample_follows_signal_network_mix(db):
    for i in range(3):
        db.upsert_watch(f"sol{i}", "1399811149", "large_buy", f"s{i}", 48, NOW)
    db.upsert_watch("base0", "8453", "large_buy", "sb", 48, NOW)

    added = recorder.admit_control_sample(
        db,
        _cands(20, "1399811149") + _cands(20, "8453"),
        NOW,
        rng=random.Random(4),
    )

    assert added == config.CONTROL_PER_CYCLE
    networks = {
        row["network_id"] for row in db._conn.execute(
            "SELECT network_id FROM watchlist WHERE is_control=1"
        )
    }
    assert networks == {"1399811149"}


def test_production_candidates_without_current_price_are_rejected(db):
    candidates = [("priced", "56", None), ("valid", "56", 0.001)]

    assert recorder.admit_control_sample(db, candidates, NOW, rng=random.Random(1)) == 1
    row = db._conn.execute(
        "SELECT token_address FROM watchlist WHERE is_control=1"
    ).fetchone()
    assert row["token_address"] == "valid"


@pytest.mark.parametrize("price", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_control_candidates_require_positive_finite_price(db, price):
    assert recorder.admit_control_sample(
        db, [("bad", "56", price)], NOW, rng=random.Random(1)
    ) == 0


def test_network_matching_converges_across_cycles(db):
    for i in range(3):
        db.upsert_watch(f"sol{i}", "1399811149", "large_buy", f"s{i}", 48, NOW)
    db.upsert_watch("base", "8453", "large_buy", "sb", 48, NOW)
    candidates = _cands(50, "1399811149") + _cands(50, "8453")

    recorder.admit_control_sample(db, candidates, NOW, rng=random.Random(2))
    recorder.admit_control_sample(db, candidates, NOW, rng=random.Random(3))

    counts = {
        row["network_id"]: row["n"] for row in db._conn.execute(
            "SELECT network_id, COUNT(*) AS n FROM watchlist "
            "WHERE is_control=1 GROUP BY network_id"
        )
    }
    assert counts == {"1399811149": 3, "8453": 1}


def test_signal_comparison_requires_same_cycle_market_candidate(db):
    db.insert_signal({
        "id": "eligible", "token_address": "tokA", "network_id": "56",
        "ts": NOW, "recorded_at": NOW, "signal_type": "large_buy",
        "raw_json": "{}",
    })
    db.insert_signal({
        "id": "outside", "token_address": "tokB", "network_id": "56",
        "ts": NOW, "recorded_at": NOW, "signal_type": "large_buy",
        "raw_json": "{}",
    })

    added = recorder.admit_signal_comparison_windows(
        db, [("tokA", "56", 0.002, "trending")], NOW
    )

    assert added == 1
    row = db._conn.execute(
        "SELECT * FROM watch_windows WHERE design_version=?",
        (config.CONTROL_DESIGN_VERSION,),
    ).fetchone()
    assert row["token_address"] == "tokA"
    assert row["admission_price_usd"] == pytest.approx(0.002)
    assert row["admission_source"] == "trending"
    assert row["is_control"] == 0


def test_same_cycle_operational_window_is_finalized_as_v3(db):
    db.insert_signal({
        "id": "new", "token_address": "tokNew", "network_id": "56",
        "ts": NOW, "recorded_at": NOW, "signal_type": "large_buy",
        "raw_json": "{}",
    })
    db.upsert_watch(
        "tokNew", "56", "large_buy", "new", 48, NOW,
        admission_price_usd=0.001,
    )

    assert recorder.admit_signal_comparison_windows(
        db, [("tokNew", "56", 0.002, "verified")], NOW
    ) == 1
    rows = db._conn.execute(
        "SELECT design_version,admission_price_usd,admission_source "
        "FROM watch_windows WHERE token_address='tokNew'"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(3, 0.002, "verified")]


def test_young_active_watch_does_not_gain_a_v3_comparison_window(db):
    created = int(datetime.fromisoformat(NOW).timestamp()) - 3600
    db.upsert_static({
        "token_address": "young", "network_id": "56", "recorded_at": NOW,
        "token_created_at": str(created), "raw_json": "{}",
    })
    db.upsert_watch("young", "56", "large_buy", "old", 48, NOW)
    db.insert_signal({
        "id": "new", "token_address": "young", "network_id": "56",
        "ts": NOW, "recorded_at": NOW, "signal_type": "large_buy",
        "raw_json": "{}",
    })

    assert recorder.admit_signal_comparison_windows(
        db, [("young", "56", 0.002, "verified")], NOW,
        admitted_signals=set(),
    ) == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM watch_windows WHERE design_version>=3"
    ).fetchone()[0] == 0


def test_new_controls_use_current_comparison_design(db):
    recorder.admit_control_sample(
        db, [("control", "56", 0.001)], NOW, rng=random.Random(1)
    )
    row = db._conn.execute(
        "SELECT design_version FROM watch_windows WHERE is_control=1"
    ).fetchone()
    assert row["design_version"] == config.CONTROL_DESIGN_VERSION
