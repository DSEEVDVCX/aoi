"""اختبارات طبقة قاعدة البيانات على قاعدة مؤقّتة (بلا شبكة).

يغطّي: إنشاء المخطّط، idempotency للإشارات/الـ ticks/الثوابت، منطق watchlist
(إضافة، حدّ، انتهاء 48 ساعة)، وعدّادات meta.
"""
import json
import os
import sqlite3

import pytest

from db import RecorderDB, decode_raw, encode_raw

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _signal(id_="s1", token="tok1", stype="multi_user_buy"):
    return {
        "id": id_, "token_address": token, "network_id": "56", "ts": "2026-07-25T00:00:00Z",
        "recorded_at": "2026-07-25T00:00:01Z", "signal_type": stype, "ticker": "T",
        "price_usd": 1.0, "fdv": None, "market_cap": None, "num_trades": 5,
        "unique_traders": None, "minutes": None, "price_change_pct": None,
        "total_volume": None, "are_top_traders": 1, "top_trader_ids_json": "[]",
        "top_trader_match_count": 2, "buyers_best_rank": 3,
        "buyer_id": None, "buyer_handle": None, "num_swaps": None,
        "is_first_buy": None, "buyer_pnl_pct": None, "avg_cost": None,
        "raw_json": "{}",
    }


def _tick(token="tok1", ts="2026-07-25T00:00:00Z", source="trending"):
    return {
        "token_address": token, "network_id": "56", "recorded_at": ts, "source": source,
        "price_usd": 1.0, "holders": 100, "change_24h": 5.0, "volume_24h": 9000.0,
        "raw_json": "{}",
    }


def test_schema_creates_all_tables(db):
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r["name"] for r in rows}
    for t in ("watchlist", "signal_events", "market_ticks", "token_static",
              "snapshots", "token_risk_assessments", "risk_fetch_state",
              "token_chain_assessments", "chain_fetch_state",
              "outcomes", "meta"):
        assert t in names


def test_insert_signal_idempotent(db):
    assert db.insert_signal(_signal()) is True
    assert db.insert_signal(_signal()) is False   # نفس id → يُتجاهل
    n = db._conn.execute("SELECT COUNT(*) c FROM signal_events").fetchone()["c"]
    assert n == 1


def test_insert_signal_rejects_semantic_duplicate_with_new_id(db):
    assert db.insert_signal(_signal(id_="event-1")) is True
    duplicate = _signal(id_="event-2")
    duplicate["recorded_at"] = "2026-07-25T00:00:02Z"
    assert db.insert_signal(duplicate) is False
    assert db._conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0] == 1


def test_insert_tick_idempotent_by_composite_key(db):
    assert db.insert_tick(_tick()) is True
    assert db.insert_tick(_tick()) is False           # نفس المفتاح المركّب
    assert db.insert_tick(_tick(ts="2026-07-25T00:01:00Z")) is True  # ختم مختلف
    n = db._conn.execute("SELECT COUNT(*) c FROM market_ticks").fetchone()["c"]
    assert n == 2


def test_tick_columns_persist(db):
    db.insert_tick(_tick())
    row = db._conn.execute("SELECT * FROM market_ticks").fetchone()
    assert row["holders"] == 100
    assert row["change_24h"] == 5.0
    assert row["volume_24h"] == 9000.0


def test_upsert_static_written_once(db):
    st = {"token_address": "tok1", "network_id": "56", "recorded_at": "t",
          "symbol": "AAA", "mintable": 1, "freezable": 0, "raw_json": "{}"}
    db.upsert_static(st)
    assert db.static_exists("tok1", "56") is True
    st2 = dict(st, symbol="BBB")
    db.upsert_static(st2)  # INSERT OR IGNORE → يبقى الأصل
    row = db._conn.execute("SELECT symbol FROM token_static WHERE token_address='tok1'").fetchone()
    assert row["symbol"] == "AAA"


def test_watchlist_add_and_count(db):
    now = "2026-07-25T00:00:00+00:00"
    assert db.upsert_watch("tok1", "56", "multi_user_buy", "s1", 48, now) is True
    assert db.upsert_watch("tok1", "56", "large_buy", "s2", 48, now) is False  # موجودة ونشطة
    assert db.active_watch_count() == 1


def test_active_watch_is_not_extended_by_new_signal(db):
    """عملة نشطة: إشارة جديدة لا تمدّد نافذتها (أوّل ظهور يبقى المرجع)."""
    db.upsert_watch("tok1", "56", "multi_user_buy", "s1", 48, "2026-07-25T00:00:00+00:00")
    db.upsert_watch("tok1", "56", "large_buy", "s2", 48, "2026-07-25T12:00:00+00:00")
    row = db._conn.execute(
        "SELECT first_seen_at, watch_until, source, entry_signal_id FROM watchlist"
    ).fetchone()
    assert row["first_seen_at"].startswith("2026-07-25T00:00:00")
    assert row["watch_until"].startswith("2026-07-27T00:00:00")
    assert row["source"] == "multi_user_buy"
    assert row["entry_signal_id"] == "s1"


def test_expired_watch_is_readmitted_by_a_new_signal(db):
    """العملة المعطّلة تعود بنافذة جديدة.

    الانحدار المقصود: مع INSERT OR IGNORE كان الصفّ المعطّل يبتلع كل إشارة
    لاحقة إلى الأبد، فتنزف قائمة المراقبة حتى الصفر وتتوقّف الـ ticks.
    """
    db.upsert_watch("tok1", "56", "multi_user_buy", "s1", 48, "2026-07-25T00:00:00+00:00")
    db.set_bars_state("tok1", "56", "ok", 3, "2026-07-27T00:00:00+00:00")
    assert db.deactivate_expired("2026-07-27T00:00:01+00:00") == 1
    assert db.active_watch_count() == 0

    readmitted = db.upsert_watch(
        "tok1", "56", "large_buy", "s9", 48, "2026-07-28T00:00:00+00:00"
    )
    assert readmitted is True
    assert db.active_watch_count() == 1
    row = db._conn.execute(
        "SELECT first_seen_at, watch_until, source, entry_signal_id FROM watchlist"
    ).fetchone()
    assert row["first_seen_at"].startswith("2026-07-28T00:00:00")
    assert row["watch_until"].startswith("2026-07-30T00:00:00")
    assert row["source"] == "large_buy"
    assert row["entry_signal_id"] == "s9"
    # لا صفّ مكرّر — المفتاح الأساسي ما يزال محترماً.
    assert db._conn.execute("SELECT COUNT(*) c FROM watchlist").fetchone()["c"] == 1


def test_readmission_clears_terminal_no_data_fetch_state(db):
    db.upsert_watch("tok1", "56", "large_buy", "s1", 48, "2026-07-25T00:00:00+00:00")
    for _ in range(3):
        db.set_bars_state("tok1", "56", "no_data", 0, "2026-07-27T00:00:00+00:00")
    assert db.deactivate_expired("2026-07-27T00:00:01+00:00") == 1

    db.upsert_watch("tok1", "56", "large_buy", "s2", 48, "2026-07-28T00:00:00+00:00")

    state = db._conn.execute(
        "SELECT * FROM bars_fetch_state WHERE token_address='tok1'"
    ).fetchone()
    assert state is None
    assert len(db.bars_fetch_due(10, "2026-07-28T01:00:00+00:00", 3)) == 1


def test_watchlist_watch_until_is_48h(db):
    now = "2026-07-25T00:00:00+00:00"
    db.upsert_watch("tok1", "56", "multi_user_buy", "s1", 48, now)
    row = db._conn.execute("SELECT watch_until FROM watchlist").fetchone()
    assert row["watch_until"].startswith("2026-07-27T00:00:00")


def test_deactivate_expired(db):
    entry = "2026-07-25T00:00:00+00:00"
    db.upsert_watch("tok1", "56", "multi_user_buy", "s1", 48, entry)
    # قبل الانتهاء لا شيء يُعطّل
    assert db.deactivate_expired("2026-07-26T00:00:00+00:00") == 0
    assert db.active_watch_count() == 1
    # بعد 48 ساعة يُعطّل
    db.set_bars_state("tok1", "56", "ok", 3, "2026-07-27T00:00:00+00:00")
    assert db.deactivate_expired("2026-07-27T00:00:01+00:00") == 1
    assert db.active_watch_count() == 0


def test_active_watches_returns_rows(db):
    db.upsert_watch("tok1", "56", "multi_user_buy", "s1", 48, "2026-07-25T00:00:00+00:00")
    ws = db.active_watches()
    assert len(ws) == 1
    assert ws[0]["token_address"] == "tok1"


def test_meta_counter(db):
    db.bump_counter("cycles_total")
    db.bump_counter("cycles_total")
    assert db.get_meta("cycles_total") == "2"


def test_snapshot_insert(db):
    db.insert_snapshot("trending", {"a": 1}, "2026-07-25T00:00:00Z")
    row = db._conn.execute("SELECT id, source, raw_json FROM snapshots").fetchone()
    assert row["source"] == "trending"
    # يُخزَّن مضغوطاً (BLOB) لا نصّاً — لكنّه يعود كاملاً عبر decode_raw.
    assert isinstance(row["raw_json"], bytes)
    assert decode_raw(row["raw_json"]) == {"a": 1}
    assert db.read_snapshot(row["id"]) == {"a": 1}


def test_compression_is_lossless_and_smaller(db):
    """ضغط الخام بلا خسارة — بايت واحد لا يُفقد من الأرشيف."""
    payload = {"tokens": [{"address": f"0x{i:040x}", "priceUSD": i * 1.5} for i in range(200)]}
    plain = json.dumps(payload, ensure_ascii=False).encode()
    assert decode_raw(encode_raw(payload)) == payload
    assert len(encode_raw(payload)) < len(plain) / 2


def test_decode_raw_reads_legacy_plaintext_rows(db):
    """الصفوف المكتوبة نصّاً قبل تفعيل الضغط تبقى مقروءة."""
    db._conn.execute(
        "INSERT INTO snapshots(recorded_at, source, raw_json) VALUES(?, ?, ?)",
        ("2026-07-25T00:00:00Z", "trending", '{"legacy": true}'),
    )
    db._conn.commit()
    row = db._conn.execute("SELECT id FROM snapshots").fetchone()
    assert db.read_snapshot(row["id"]) == {"legacy": True}


def test_tick_and_signal_raw_json_round_trip(db):
    db.insert_signal(dict(_signal(), raw_json='{"ev": 1}'))
    db.insert_tick(dict(_tick(), raw_json='{"tick": 2}'))
    sig = db._conn.execute("SELECT raw_json FROM signal_events").fetchone()
    tick = db._conn.execute("SELECT raw_json FROM market_ticks").fetchone()
    assert decode_raw(sig["raw_json"]) == {"ev": 1}
    assert decode_raw(tick["raw_json"]) == {"tick": 2}


def test_batch_commits_once_and_rolls_back_on_error(db):
    with db.batch():
        db.insert_tick(_tick(ts="2026-07-25T00:10:00Z"))
        db.insert_tick(_tick(ts="2026-07-25T00:11:00Z"))
    assert db._conn.execute("SELECT COUNT(*) c FROM market_ticks").fetchone()["c"] == 2

    with pytest.raises(RuntimeError):
        with db.batch():
            db.insert_tick(_tick(ts="2026-07-25T00:12:00Z"))
            raise RuntimeError("boom")
    # الدفعة الفاشلة تُرجَع كاملة
    assert db._conn.execute("SELECT COUNT(*) c FROM market_ticks").fetchone()["c"] == 2


def test_prune_snapshots_removes_only_old_rows(db):
    db.insert_snapshot("trending", {"n": 1}, "2026-07-20T00:00:00+00:00")
    db.insert_snapshot("trending", {"n": 2}, "2026-07-26T00:00:00+00:00")
    assert db.prune_snapshots("2026-07-25T00:00:00+00:00") == 1
    rows = db._conn.execute("SELECT id FROM snapshots").fetchall()
    assert len(rows) == 1
    assert db.read_snapshot(rows[0]["id"]) == {"n": 2}


# --- token_bars + جدولة السحب الدوّارة ---
def _bar(ts, c=1.0, token="tok1", net="56"):
    return {"token_address": token, "network_id": net, "resolution": "5", "ts": ts,
            "o": 1.0, "h": 2.0, "l": 0.5, "c": c, "v": 10.0, "fetched_at": "t0"}


def test_insert_bars_writes_rows(db):
    assert db.insert_bars([_bar(100), _bar(400)]) == 2
    assert db.bars_count("tok1", "56") == 2
    assert db.insert_bars([]) == 0


def test_insert_bars_replaces_in_progress_candle(db):
    """الشمعة الأخيرة تكون قيد التكوّن وقت السحب — القيمة الأحدث هي الصحيحة."""
    db.insert_bars([_bar(100, c=1.0)])
    db.insert_bars([_bar(100, c=1.7)])          # نفس الختم، مُراجَع
    assert db.bars_count("tok1", "56") == 1
    row = db._conn.execute("SELECT c FROM token_bars WHERE ts=100").fetchone()
    assert row["c"] == 1.7


def test_bars_fetch_due_prefers_never_fetched(db):
    """العملة التي لم تُسحب قطّ تسبق الجميع (backfill الدخول أولاً)."""
    db.upsert_watch("old", "56", "large_buy", "s1", 48, "2026-07-26T00:00:00+00:00")
    db.upsert_watch("new", "56", "large_buy", "s2", 48, "2026-07-26T00:00:00+00:00")
    db.set_bars_state("old", "56", "ok", 10, "2026-07-26T01:00:00+00:00")

    due = db.bars_fetch_due(10, "2026-07-26T12:00:00+00:00", 3)
    assert [d["token_address"] for d in due] == ["new", "old"]


def test_bars_fetch_due_respects_refresh_window_and_limit(db):
    for t in ("a", "b", "c"):
        db.upsert_watch(t, "56", "large_buy", "s", 48, "2026-07-26T00:00:00+00:00")
        db.set_bars_state(t, "56", "ok", 5, "2026-07-26T11:00:00+00:00")
    # كلّها سُحبت بعد عتبة القِدم → لا شيء مستحقّ
    assert db.bars_fetch_due(10, "2026-07-26T10:00:00+00:00", 3) == []
    # عتبة أحدث → كلّها مستحقّة، لكنّ الشريحة محدودة
    assert len(db.bars_fetch_due(2, "2026-07-26T12:00:00+00:00", 3)) == 2


def test_bars_fetch_due_drops_tokens_with_repeated_no_data(db):
    """عملة بلا سلسلة سعرية تُستبعد بدل إهدار محاولات عليها كل دورة."""
    db.upsert_watch("dead", "56", "large_buy", "s", 48, "2026-07-26T00:00:00+00:00")
    for _ in range(3):
        db.set_bars_state("dead", "56", "no_data", 0, "2026-07-26T01:00:00+00:00")
    assert db.bars_fetch_due(10, "2026-07-26T12:00:00+00:00", 3) == []
    # لكن خطأ عابر لا يُستبعد — قد يتعافى
    db.set_bars_state("dead", "56", "error", 0, "2026-07-26T01:00:00+00:00")
    assert len(db.bars_fetch_due(10, "2026-07-26T12:00:00+00:00", 3)) == 1


def test_bars_fetch_due_ignores_inactive_watches(db):
    db.upsert_watch("gone", "56", "large_buy", "s", 48, "2026-07-24T00:00:00+00:00")
    db.set_bars_state("gone", "56", "ok", 3, "2026-07-27T00:00:00+00:00")
    db.deactivate_expired("2026-07-27T00:00:00+00:00")
    assert db.bars_fetch_due(10, "2026-07-27T12:00:00+00:00", 3) == []


def test_expired_watch_stays_active_until_final_bars_fetch(db):
    db.upsert_watch("late", "56", "large_buy", "s", 48, "2026-07-25T00:00:00+00:00")

    assert db.deactivate_expired("2026-07-27T01:00:00+00:00") == 0
    assert db.active_watch_count() == 1

    db.set_bars_state("late", "56", "ok", 3, "2026-07-27T01:00:00+00:00")
    assert db.deactivate_expired("2026-07-27T01:01:00+00:00") == 1
    assert db.active_watch_count() == 0


def test_set_bars_state_accumulates_attempts(db):
    db.set_bars_state("tok1", "56", "no_data", 0, "t1")
    db.set_bars_state("tok1", "56", "ok", 42, "t2")
    row = db._conn.execute("SELECT * FROM bars_fetch_state").fetchone()
    assert row["attempts"] == 2
    assert row["last_status"] == "ok"
    assert row["candles"] == 42
    assert row["last_fetch_at"] == "t2"


# --- ترحيل الأعمدة على قاعدة قائمة ---
def test_migration_adds_is_control_to_a_preexisting_watchlist(tmp_path):
    """CREATE TABLE IF NOT EXISTS لا يمسّ جدولاً موجوداً.

    بلا الترحيل، عمود يُضاف إلى schema.sql لا يظهر أبداً في قاعدة أُنشئت قبله
    فتعطب الاستعلامات في الإنتاج بينما تمرّ على قاعدة اختبار جديدة.
    """
    p = str(tmp_path / "old.db")
    old = sqlite3.connect(p)
    old.executescript("""
        CREATE TABLE watchlist (
            token_address TEXT NOT NULL, network_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, source TEXT NOT NULL,
            watch_until TEXT NOT NULL, entry_signal_id TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (token_address, network_id));
        INSERT INTO watchlist VALUES('legacy','56','t','large_buy','t2','sig',1);
    """)
    old.commit()
    old.close()

    db = RecorderDB(p, SCHEMA)
    try:
        cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(watchlist)")}
        assert "is_control" in cols
        row = db._conn.execute("SELECT * FROM watchlist").fetchone()
        assert row["token_address"] == "legacy"      # الصفّ القديم سليم
        assert row["is_control"] == 0                # وافتراضه "مُشار إليها"
        assert db.active_watch_count(is_control=0) == 1
    finally:
        db.close()


def test_migration_is_idempotent(tmp_path):
    p = str(tmp_path / "twice.db")
    for _ in range(3):
        d = RecorderDB(p, SCHEMA)
        d.close()
    d = RecorderDB(p, SCHEMA)
    try:
        cols = [r["name"] for r in d._conn.execute("PRAGMA table_info(watchlist)")]
        assert cols.count("is_control") == 1
    finally:
        d.close()


def test_migration_does_not_restore_deleted_legacy_controls(tmp_path):
    p = str(tmp_path / "windows.db")
    old = sqlite3.connect(p)
    old.executescript("""
        CREATE TABLE watchlist (
            token_address TEXT NOT NULL, network_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, source TEXT NOT NULL,
            watch_until TEXT NOT NULL, entry_signal_id TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            is_control INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (token_address, network_id));
        INSERT INTO watchlist VALUES
            ('legacy','56','2026-07-26T00:00:00+00:00','control',
             '2026-07-28T00:00:00+00:00',NULL,0,1);
    """)
    old.commit()
    old.close()

    db = RecorderDB(p, SCHEMA)
    try:
        assert db._conn.execute("SELECT COUNT(*) FROM watch_windows").fetchone()[0] == 0
    finally:
        db.close()


def test_migration_quarantines_legacy_watch_outcomes(tmp_path):
    p = str(tmp_path / "legacy-outcomes.db")
    db = RecorderDB(p, SCHEMA)
    db._conn.execute(
        """INSERT INTO outcomes(
               kind,key,token_address,network_id,is_control,entry_ts,status,labeled_at)
           VALUES('watch','old-control','ctl','56',1,1,'ok','t')"""
    )
    db._conn.commit()
    db.close()

    migrated = RecorderDB(p, SCHEMA)
    try:
        row = migrated._conn.execute(
            "SELECT design_version,analysis_eligible,exclusion_reason FROM outcomes"
        ).fetchone()
        assert row["design_version"] == 1
        assert row["analysis_eligible"] == 0
        assert row["exclusion_reason"] == "superseded_comparison_design"
    finally:
        migrated.close()


# --- إعادة بناء العدد التاريخي للأطروحات ---
def _th(tid, created, token="tok1"):
    return {"id": tid, "token_address": token, "network_id": "56",
            "created_at": created, "user_handle": "u", "user_id": "uid",
            "num_likes": 1, "num_replies": 0, "equity": 0.0, "trade_id": None,
            "comment": "gm", "fetched_at": "now", "raw_json": "{}"}


def test_insert_thesis_items_is_idempotent_by_id(db):
    assert db.insert_thesis_items([_th("a", "2026-07-25T10:00:00Z")]) == 1
    assert db.insert_thesis_items([_th("a", "2026-07-25T10:00:00Z")]) == 0
    assert db.insert_thesis_items([]) == 0


def test_thesis_count_before_reconstructs_history(db):
    """السؤال الذي وُجد الجدول لأجله: كم أطروحة كانت لحظة الإشارة؟"""
    db.insert_thesis_items([
        _th("a", "2026-07-25T10:00:00Z"),
        _th("b", "2026-07-25T12:00:00Z"),
        _th("c", "2026-07-26T09:00:00Z"),
    ])
    assert db.thesis_count_before("tok1", "2026-07-25T09:00:00Z") == 0
    assert db.thesis_count_before("tok1", "2026-07-25T11:00:00Z") == 1
    assert db.thesis_count_before("tok1", "2026-07-25T23:00:00Z") == 2
    assert db.thesis_count_before("tok1", "2026-07-27T00:00:00Z") == 3
    assert db.thesis_count_before("other", "2026-07-27T00:00:00Z") == 0
