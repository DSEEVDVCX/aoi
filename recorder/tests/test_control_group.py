"""اختبارات المجموعة الضابطة (الصنف السالب).

تثبت الخصائص التي تجعل المقارنة صالحة علمياً: الاختيار عشوائيّ لا ترتيبيّ،
لا يلمس ما أُشير إليه، لا يُخفّض عملة مُشار إليها، ويُقسَّط على الزمن.
"""
import os
import random

import pytest

import config
import recorder
from db import RecorderDB

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-07-27T00:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _cands(n, net="56"):
    return [(f"tok{i:03d}", net) for i in range(n)]


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


def test_signal_promotes_a_control_coin_and_clears_the_flag(db):
    """عملة ضابطة وردتها إشارة تصير مُشاراً إليها بنافذة جديدة."""
    db.admit_control("c1", "56", 48, NOW)
    promoted = db.upsert_watch("c1", "56", "large_buy", "sig9", 48, "2026-07-27T05:00:00+00:00")

    assert promoted is True
    row = db._conn.execute("SELECT * FROM watchlist WHERE token_address='c1'").fetchone()
    assert row["is_control"] == 0
    assert row["source"] == "large_buy"
    assert row["entry_signal_id"] == "sig9"
    assert row["first_seen_at"].startswith("2026-07-27T05:00:00")
    assert db.active_watch_count(is_control=1) == 0


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
