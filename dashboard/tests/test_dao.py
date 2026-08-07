"""اختبارات dao على قاعدة مؤقّتة (بلا شبكة، بلا مسّ recorder.db الحقيقي).

نبني قاعدة صغيرة بنفس أعمدة recorder.db، نملؤها، ثم نتحقّق أن دوال القراءة
الخالصة تعيد ما هو متوقّع — بما في ذلك منطق "حيّ خلال المهلة" ومطابقة meta.
"""
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import dao

# مخطّط مصغّر مطابق للأعمدة التي تقرؤها dao.
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE signal_events (
  id TEXT PRIMARY KEY, token_address TEXT, network_id TEXT, ts TEXT, recorded_at TEXT,
  signal_type TEXT, ticker TEXT, price_usd REAL, fdv REAL, market_cap REAL,
  num_trades INTEGER, are_top_traders INTEGER, top_trader_match_count INTEGER,
  buyers_best_rank INTEGER, buyer_handle TEXT, num_swaps INTEGER,
  is_first_buy INTEGER, buyer_pnl_pct REAL, rowid_helper INTEGER
);
CREATE TABLE market_ticks (
  token_address TEXT, network_id TEXT, recorded_at TEXT, source TEXT,
  price_usd REAL, holders INTEGER, change_24h REAL, volume_24h REAL,
  buy_count_24h INTEGER, sell_count_24h INTEGER
);
CREATE TABLE token_static (token_address TEXT, network_id TEXT, symbol TEXT);
CREATE TABLE watchlist (
  token_address TEXT, network_id TEXT, first_seen_at TEXT, source TEXT,
  watch_until TEXT, entry_signal_id TEXT, active INTEGER,
  is_control INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE watch_windows (
  token_address TEXT, network_id TEXT, first_seen_at TEXT,
  design_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE outcomes (
  kind TEXT, key TEXT, token_address TEXT, network_id TEXT,
  is_control INTEGER, status TEXT, design_version INTEGER,
  analysis_eligible INTEGER, entry_ts INTEGER
);
CREATE TABLE snapshots (id INTEGER PRIMARY KEY, recorded_at TEXT, source TEXT, raw_json TEXT);
"""

# مسار مخطّط المسجّل الحقيقي — يُستعمل في اختبار انجراف المخطّط أدناه.
REAL_SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "recorder", "schema.sql",
)


@pytest.fixture()
def db_path(tmp_path):
    p = str(tmp_path / "rec.db")
    conn = sqlite3.connect(p)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return p


def _conn(db_path):
    return dao.connect_ro(db_path)


def _seed_meta(db_path, **kv):
    c = sqlite3.connect(db_path)
    for k, v in kv.items():
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (k, str(v)))
    c.commit()
    c.close()


# --- connect_ro يمنع الكتابة ---
def test_connect_ro_is_readonly(db_path):
    conn = _conn(db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO meta(key, value) VALUES('x','1')")
    conn.close()


# --- recorder_status ---
def test_status_alive_when_recent(db_path):
    now = datetime.now(UTC)
    _seed_meta(db_path, cycles_total=12, last_cycle_at=(now - timedelta(seconds=30)).isoformat())
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000, now=now)
    assert st["alive"] is True
    assert st["cycles_total"] == 12
    assert st["seconds_since_last_cycle"] < 150
    conn.close()


def test_status_dead_when_stale(db_path):
    now = datetime.now(UTC)
    _seed_meta(db_path, cycles_total=5, last_cycle_at=(now - timedelta(seconds=600)).isoformat())
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000, now=now)
    assert st["alive"] is False
    conn.close()


def test_status_labeler_fresh_is_not_stale(db_path):
    now = datetime.now(UTC)
    _seed_meta(db_path, labeler_last_run_at=(now - timedelta(seconds=900)).isoformat())
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000, now=now)
    assert st["labeler_stale"] is False
    assert st["labeler_age_seconds"] == pytest.approx(900, abs=1)
    conn.close()


def test_status_labeler_dead_when_stale(db_path):
    """موت الموسِّم صامت — لا ينكشف إلّا من قِدَم ختمه."""
    now = datetime.now(UTC)
    _seed_meta(db_path, labeler_last_run_at=(now - timedelta(seconds=7200)).isoformat())
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000, now=now)
    assert st["labeler_stale"] is True
    conn.close()


def test_status_labeler_never_ran_is_stale(db_path):
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000, now=datetime.now(UTC))
    assert st["labeler_stale"] is True
    assert st["labeler_age_seconds"] is None
    conn.close()


def test_control_maturity_counts_only_completed_eligible_v3_controls(db_path):
    c = sqlite3.connect(db_path)
    c.executemany(
        "INSERT INTO outcomes VALUES('watch',?,?,?,?,?,?,?,?)",
        [
            ("a", "a", "56", 1, "ok", 3, 1, 100),
            ("b", "b", "56", 1, "ok", 3, 1, 200),
            ("pending", "p", "56", 1, "no_entry", 3, 1, 300),
            ("legacy", "l", "56", 1, "ok", 2, 0, 50),
            ("signal", "s", "56", 0, "ok", 3, 1, 400),
        ],
    )
    c.commit()
    c.close()

    conn = _conn(db_path)
    progress = dao.control_maturity(conn, preliminary_target=100, decision_target=500)
    conn.close()

    assert progress == {
        "completed": 2,
        "preliminary_target": 100,
        "decision_target": 500,
        "preliminary_remaining": 98,
        "decision_remaining": 498,
        "preliminary_pct": 2.0,
        "decision_pct": 0.4,
        "preliminary_ready": False,
        "decision_ready": False,
        "design_version": 3,
        "first_entry_ts": 100,
        "last_entry_ts": 200,
    }


def test_status_missing_meta_keys_default(db_path):
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000)
    assert st["alive"] is False
    assert st["cycles_total"] == 0
    assert st["errors_total"] == 0          # المفتاح غائب → 0 لا استثناء
    assert st["seconds_since_last_cycle"] is None
    conn.close()


# --- errors ---
def test_recorder_errors_absent_is_none(db_path):
    conn = _conn(db_path)
    errs = dao.recorder_errors(conn, ("feed", "trending"))
    assert {e["source"] for e in errs} == {"feed", "trending"}
    assert all(e["last_error"] is None for e in errs)
    conn.close()


def test_recorder_errors_present(db_path):
    _seed_meta(db_path, last_error_feed="boom")
    conn = _conn(db_path)
    errs = dao.recorder_errors(conn, ("feed", "trending"))
    feed = next(e for e in errs if e["source"] == "feed")
    assert feed["last_error"] == "boom"
    assert feed["stale"] is False   # لا ختم زمني ولا حدّ → يُعتبر حالياً
    conn.close()


def test_recorder_errors_stale_when_older_than_started(db_path):
    # خطأ ختمه قبل started_at (عملية مسجّل ماتت) → قديم/متعافى.
    _seed_meta(
        db_path,
        started_at="2026-07-26T11:08:00+00:00",
        last_error_feed="2026-07-26T11:07:56+00:00: UnauthorizedError: expired",
    )
    conn = _conn(db_path)
    feed = next(e for e in dao.recorder_errors(conn, ("feed",)) if e["source"] == "feed")
    assert feed["stale"] is True
    conn.close()


def test_recorder_errors_stale_when_older_than_last_ok_cycle(db_path):
    # خطأ أقدم من آخر دورة نجحت كلياً → قديم، رغم عدم إعادة تشغيل العملية.
    _seed_meta(
        db_path,
        started_at="2026-07-26T10:00:00+00:00",
        last_ok_cycle_at="2026-07-26T11:20:00+00:00",
        last_error_trending="2026-07-26T11:07:56+00:00: UnauthorizedError: expired",
    )
    conn = _conn(db_path)
    tr = next(e for e in dao.recorder_errors(conn, ("trending",)) if e["source"] == "trending")
    assert tr["stale"] is True
    conn.close()


def test_recorder_errors_active_when_newer_than_boundary(db_path):
    # خطأ أحدث من آخر دورة ناجحة ومن started_at → حالي (غير قديم).
    _seed_meta(
        db_path,
        started_at="2026-07-26T10:00:00+00:00",
        last_ok_cycle_at="2026-07-26T11:00:00+00:00",
        last_error_verified="2026-07-26T11:30:00+00:00: UnauthorizedError: expired",
    )
    conn = _conn(db_path)
    v = next(e for e in dao.recorder_errors(conn, ("verified",)) if e["source"] == "verified")
    assert v["stale"] is False
    conn.close()


# --- signals ---
def test_recent_signals_order_and_fields(db_path):
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO signal_events(id, token_address, recorded_at, signal_type, ticker, "
        "top_trader_match_count, buyers_best_rank) VALUES('a','tokA','2026-07-25T00:00:00Z','large_buy','AAA',1,4)"
    )
    c.execute(
        "INSERT INTO signal_events(id, token_address, recorded_at, signal_type, ticker) "
        "VALUES('b','tokB','2026-07-25T01:00:00Z','multi_user_buy','BBB')"
    )
    c.commit(); c.close()
    conn = _conn(db_path)
    rows = dao.recent_signals(conn, 50)
    assert [r["id"] for r in rows] == ["b", "a"]   # الأحدث أولاً
    assert rows[1]["top_trader_match_count"] == 1
    assert rows[1]["buyers_best_rank"] == 4
    conn.close()


def test_recent_signals_limit_clamped(db_path):
    conn = _conn(db_path)
    assert dao.recent_signals(conn, 0) == []       # لا صفوف، لكن لا استثناء
    conn.close()


# --- watchlist ---
def test_active_watchlist_counts_ticks(db_path):
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO watchlist(token_address, source, first_seen_at, watch_until, active) "
        "VALUES('tokA','feed','2026-07-25T00:00:00Z','2026-07-27T00:00:00Z',1)"
    )
    c.execute(
        "INSERT INTO watchlist(token_address, source, first_seen_at, watch_until, active) "
        "VALUES('tokB','feed','2026-07-25T00:00:00Z','2026-07-27T00:00:00Z',0)"  # غير نشط
    )
    for i in range(3):
        c.execute(
            "INSERT INTO market_ticks(token_address, recorded_at, source) VALUES('tokA',?, 'trending')",
            (f"2026-07-25T00:0{i}:00Z",),
        )
    c.commit(); c.close()
    conn = _conn(db_path)
    wl = dao.active_watchlist(conn)
    assert len(wl) == 1                            # النشط فقط
    assert wl[0]["token_address"] == "tokA"
    assert wl[0]["tick_count"] == 3
    assert wl[0]["combined_status"] is None
    conn.close()


# --- ticks summary ---
def test_ticks_summary_latest_per_token(db_path):
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO watchlist(token_address, source, first_seen_at, watch_until, active) "
        "VALUES('tokA','feed','t','t',1)"
    )
    c.execute("INSERT INTO market_ticks(token_address, recorded_at, price_usd, volume_24h) VALUES('tokA','2026-07-25T00:00:00Z',1.0,100.0)")
    c.execute("INSERT INTO market_ticks(token_address, recorded_at, price_usd, volume_24h) VALUES('tokA','2026-07-25T00:05:00Z',2.0,200.0)")
    c.commit(); c.close()
    conn = _conn(db_path)
    summ = dao.ticks_summary(conn)
    assert summ["total"] == 2
    assert len(summ["per_token"]) == 1
    assert summ["per_token"][0]["price_usd"] == 2.0   # الأحدث
    conn.close()


# --- counts ---
def test_table_counts(db_path):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO signal_events(id, token_address) VALUES('a','x')")
    c.execute("INSERT INTO token_static(token_address) VALUES('x')")
    c.commit(); c.close()
    conn = _conn(db_path)
    counts = dao.table_counts(conn)
    assert counts["signal_events"] == 1
    assert counts["token_static"] == 1
    assert counts["market_ticks"] == 0
    assert counts["outcomes"] == 0                 # الجدول غير موجود → 0 لا استثناء
    conn.close()


# --- storage ---
def test_storage_stats_reports_size_and_growth_rate(db_path):
    """رقابة النموّ: بدون هذا بقي انفجار الأرشيف خفيّاً حتى بلغ 720 MB."""
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO snapshots(recorded_at, source, raw_json) "
        "VALUES('2026-07-20T00:00:00+00:00','trending', ?)",
        ("x" * 50_000,),
    )
    c.execute(
        "INSERT INTO snapshots(recorded_at, source, raw_json) "
        "VALUES('2026-07-22T00:00:00+00:00','trending', ?)",
        ("y" * 50_000,),
    )
    c.commit(); c.close()
    conn = _conn(db_path)
    st = dao.storage_stats(db_path, conn)
    assert st["bytes"] > 100_000
    assert st["mb"] == round(st["bytes"] / 1e6, 1)
    assert st["span_days"] == 2.0
    assert st["mb_per_day"] == round(st["mb"] / 2.0, 1)
    conn.close()


def test_storage_stats_without_span_returns_none_rate(db_path):
    """لقطة واحدة (أو صفر) → لا مدّة، فلا معدّل مفبرك."""
    conn = _conn(db_path)
    st = dao.storage_stats(db_path, conn)
    assert st["span_days"] is None
    assert st["mb_per_day"] is None
    assert st["raw_encoding"] == "plain"           # لا مفتاح meta → افتراض واضح
    conn.close()


def test_storage_stats_reports_backup_freshness_and_disk_state(db_path, tmp_path):
    backup_dir = tmp_path / "external-backups"
    backup_dir.mkdir()
    backup = backup_dir / "recorder-20260807-010000-000000.db"
    backup.write_bytes(b"backup")
    now = datetime.fromtimestamp(backup.stat().st_mtime, UTC) + timedelta(hours=2)
    conn = _conn(db_path)

    st = dao.storage_stats(
        db_path,
        conn,
        backup_dir=str(backup_dir),
        backup_max_age_hours=36,
        disk_free_warn_bytes=0,
        now=now,
    )

    assert st["backup_configured"] is True
    assert st["latest_backup"] == backup.name
    assert st["backup_age_hours"] == 2.0
    assert st["backup_warning"] is False
    assert st["disk_warning"] is False
    assert st["disk_free_bytes"] > 0
    conn.close()


def test_storage_stats_warns_when_configured_backup_is_missing(db_path, tmp_path):
    conn = _conn(db_path)
    st = dao.storage_stats(db_path, conn, backup_dir=str(tmp_path / "missing"))
    assert st["backup_configured"] is True
    assert st["latest_backup"] is None
    assert st["backup_warning"] is True
    conn.close()


# --- bars coverage ---
def _bars_schema(db_path):
    c = sqlite3.connect(db_path)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS token_bars (
            token_address TEXT, network_id TEXT, resolution TEXT, ts INTEGER,
            o REAL, h REAL, l REAL, c REAL, v REAL, fetched_at TEXT,
            PRIMARY KEY (token_address, network_id, resolution, ts));
        CREATE TABLE IF NOT EXISTS bars_fetch_state (
            token_address TEXT, network_id TEXT, last_fetch_at TEXT,
            last_status TEXT, candles INTEGER, attempts INTEGER DEFAULT 0,
            PRIMARY KEY (token_address, network_id));
    """)
    return c


def test_bars_coverage_counts_watched_tokens_with_series(db_path):
    c = _bars_schema(db_path)
    for t in ("a", "b", "c"):
        c.execute("INSERT INTO watchlist(token_address, network_id, source, first_seen_at,"
                  " watch_until, active) VALUES(?, '56','feed','t','t',1)", (t,))
    c.execute("INSERT INTO token_bars VALUES('a','56','5',100,1,2,0.5,1.5,9,'t')")
    c.execute("INSERT INTO token_bars VALUES('a','56','5',400,1,2,0.5,1.5,9,'t')")
    c.execute("INSERT INTO bars_fetch_state VALUES('b','56','t','no_data',0,3)")
    c.commit(); c.close()

    conn = _conn(db_path)
    cov = dao.bars_coverage(conn, 0)    # live=0: عدّ كل الشموع (اختبار آلية التغطية لا الحِقبة)
    assert cov["active"] == 3
    assert cov["with_bars"] == 1
    assert cov["no_data"] == 1
    assert cov["pending"] == 1          # 'c' لم يصلها الدور بعد — ليست فشلاً
    assert cov["candles"] == 2
    assert cov["coverage_pct"] == round(100 / 3, 1)
    conn.close()


def test_bars_coverage_candles_excludes_pre_live_retro(db_path):
    """عدّ الشموع يقصر على الحِقبة الحيّة: الشموع الرجعيّة (ts < live) تاريخ سعر
    سابق للإشارة لا جمعه البوت لحظياً، فلا تُعرض كي لا تختلط ببيانات البوت."""
    live = 1_785_018_927
    c = _bars_schema(db_path)
    c.execute("INSERT INTO watchlist(token_address, network_id, source, first_seen_at,"
              " watch_until, active) VALUES('a','56','feed','t','t',1)")
    # شمعتان رجعيّتان (قبل الحدّ) + شمعة حيّة واحدة (بعده)
    c.execute("INSERT INTO token_bars VALUES('a','56','5',?,1,2,0.5,1.5,9,'t')", (live - 3600,))
    c.execute("INSERT INTO token_bars VALUES('a','56','5',?,1,2,0.5,1.5,9,'t')", (live - 60,))
    c.execute("INSERT INTO token_bars VALUES('a','56','5',?,1,2,0.5,1.5,9,'t')", (live + 60,))
    c.commit(); c.close()

    conn = _conn(db_path)
    cov = dao.bars_coverage(conn, live)
    assert cov["candles"] == 1          # الحيّة فقط — الرجعيّتان مُستبعدتان
    assert cov["with_bars"] == 1        # العملة لها سلسلة (بصرف النظر عن الحِقبة)
    conn.close()


def test_bars_coverage_without_tables_is_zero_not_error(db_path):
    """قاعدة قديمة بلا جدول شموع → أصفار، لا استثناء."""
    conn = _conn(db_path)
    cov = dao.bars_coverage(conn, 0)
    assert cov == {"active": 0, "with_bars": 0, "pending": 0, "no_data": 0,
                   "candles": 0, "coverage_pct": None}
    conn.close()


# --- أداء الإشارات: ترتيب وحصيلة ---
def _perf_fixture(db_path):
    """ثلاث عملات بنتائج معروفة: رابحة كبيرة، رابحة صغيرة، خاسرة."""
    c = _bars_schema(db_path)
    entry = 1785000000  # epoch لختم الدخول
    iso = datetime.fromtimestamp(entry, UTC).isoformat()
    specs = [
        # (token, سعر الدخول, القمّة, السعر الآن)
        ("big",  1.0, 5.0, 4.0),   # +300% الآن، قمّة +400%
        ("small", 1.0, 1.2, 1.1),  # +10%  الآن، قمّة  +20%
        ("loser", 1.0, 1.0, 0.5),  # -50%  الآن، قمّة    0%
    ]
    for tok, e, peak, last in specs:
        c.execute("INSERT INTO watchlist(token_address, network_id, source, first_seen_at,"
                  " watch_until, active) VALUES(?, '56','large_buy',?,'t',1)", (tok, iso))
        c.execute("INSERT INTO token_bars VALUES(?, '56','5',?,?,?,?,?,1,'t')",
                  (tok, entry, e, e, e, e))
        c.execute("INSERT INTO token_bars VALUES(?, '56','5',?,?,?,?,?,1,'t')",
                  (tok, entry + 300, peak, peak, peak, peak))
        c.execute("INSERT INTO token_bars VALUES(?, '56','5',?,?,?,?,?,1,'t')",
                  (tok, entry + 600, last, last, last, last))
    c.commit(); c.close()


def test_watch_performance_computes_entry_peak_and_change(db_path):
    _perf_fixture(db_path)
    conn = _conn(db_path)
    rows = {r["token_address"]: r for r in dao.watch_performance(conn, limit=10)}
    big = rows["big"]
    assert big["entry_px"] == 1.0
    assert big["peak_px"] == 5.0
    assert big["change_pct"] == pytest.approx(300.0)
    assert big["peak_pct"] == pytest.approx(400.0)
    assert big["from_peak_pct"] == pytest.approx(-20.0)   # 4.0 مقابل قمّة 5.0
    assert rows["loser"]["change_pct"] == pytest.approx(-50.0)
    conn.close()


def test_sorting_ascending_surfaces_the_worst_not_the_reversed_top(db_path):
    """الانحدار المقصود: الترتيب يجري على المجموعة كاملة قبل الاقتطاع.

    لو رُتِّب في المتصفّح بعد اقتطاع الأفضل، لأعطى العكسُ «الأفضل مقلوباً»
    فما ظهرت الخاسرة أبداً.
    """
    _perf_fixture(db_path)
    conn = _conn(db_path)
    worst_one = dao.watch_performance(conn, limit=1, sort_key="change_pct", descending=False)
    assert [r["token_address"] for r in worst_one] == ["loser"]
    best_one = dao.watch_performance(conn, limit=1, sort_key="change_pct", descending=True)
    assert [r["token_address"] for r in best_one] == ["big"]
    conn.close()


def test_sorting_accepts_only_whitelisted_keys(db_path):
    _perf_fixture(db_path)
    conn = _conn(db_path)
    # مفتاح غير معروف يعود إلى الافتراضي بدل أن يرفع استثناءً أو يُحقن
    rows = dao.watch_performance(conn, limit=3, sort_key="'; DROP TABLE watchlist--")
    assert [r["token_address"] for r in rows] == ["big", "small", "loser"]
    conn.close()


def test_sorting_keeps_missing_values_last_in_both_directions(db_path):
    _perf_fixture(db_path)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE token_static SET symbol=NULL")     # لا رموز أصلاً
    c.execute("INSERT INTO token_static(token_address, symbol) VALUES('big','ZZZ')")
    c.commit(); c.close()
    conn = _conn(db_path)
    for desc in (True, False):
        syms = [r["symbol"] for r in dao.watch_performance(conn, 10, "symbol", desc)]
        assert syms[0] == "ZZZ"                          # الموجود أوّلاً دائماً
        assert syms[1:] == [None, None]
    conn.close()


def test_performance_summary_aggregates_wins_and_losses(db_path):
    _perf_fixture(db_path)
    conn = _conn(db_path)
    s = dao.performance_summary(conn)
    assert s["count"] == 3
    assert s["winners"] == 2 and s["losers"] == 1
    assert s["win_rate_pct"] == pytest.approx(200 / 3)
    assert s["gross_gain_pct"] == pytest.approx(310.0)    # 300 + 10
    assert s["gross_loss_pct"] == pytest.approx(-50.0)
    assert s["net_pct"] == pytest.approx(260.0)
    assert s["avg_pct"] == pytest.approx(260 / 3)
    assert s["median_pct"] == pytest.approx(10.0)         # أمتن من المتوسّط
    assert s["best"]["change_pct"] == pytest.approx(300.0)
    assert s["worst"]["change_pct"] == pytest.approx(-50.0)
    # network_id لازم لبناء رابط صفحة العملة على fomo (/tokens/:chain/:address)
    assert s["best"]["network_id"] == "56"
    assert s["worst"]["network_id"] == "56"
    assert s["is_open"] is True                          # نافذة جارية لا نتيجة محقّقة
    conn.close()


def test_performance_summary_covers_all_rows_not_just_the_displayed_slice(db_path):
    """الحصيلة تُحسب على الكل — وإلّا تغيّرت الأرقام بتغيير حدّ العرض."""
    _perf_fixture(db_path)
    conn = _conn(db_path)
    assert dao.watch_performance(conn, limit=1) != dao.watch_performance(conn, limit=3)
    assert dao.performance_summary(conn)["count"] == 3
    conn.close()


def test_performance_summary_without_data_is_empty_not_error(db_path):
    conn = _conn(db_path)
    s = dao.performance_summary(conn)
    assert s["count"] == 0 and s["avg_pct"] is None and s["best"] is None
    conn.close()


# --- حارس انجراف المخطّط ---
def test_every_dao_read_runs_against_the_real_recorder_schema(tmp_path):
    """كل دالة قراءة تعمل على **مخطّط المسجّل الحقيقي**، لا على مخطّط الاختبار.

    سبب وجوده: المخطّط المصغّر هنا انجرف عن الحقيقي (كان `token_static` بلا عمود
    `symbol`)، فاستعلام يعطب في الإنتاج كان يمرّ في الاختبار. هذا الاختبار يبني
    قاعدة من `recorder/schema.sql` نفسه ويشغّل كل دالة — فأي عمود يُحذف أو
    يُعاد تسميته في المسجّل يُسقط اختبارات اللوحة فوراً.
    """
    p = str(tmp_path / "real.db")
    conn = sqlite3.connect(p)
    with open(REAL_SCHEMA, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()
    conn.close()

    c = _conn(p)
    try:
        assert dao.recorder_status(c, 150, 2000)["alive"] is False
        assert dao.recorder_errors(c, ("feed", "bars")) is not None
        assert dao.table_counts(c)["token_bars"] == 0
        assert dao.active_watch_count(c) == 0
        assert dao.recent_signals(c, 10) == []
        assert dao.active_watchlist(c) == []
        assert dao.safety_summary(c)["active"] == 0
        assert dao.ticks_summary(c)["total"] == 0
        assert dao.bars_coverage(c, 0)["candles"] == 0
        assert dao.watch_performance(c, 10) == []
        assert dao.performance_summary(c)["count"] == 0
        assert dao.token_series(c, "x", "56", 0) == []
        assert dao.signal_timeline(c, 6)["types"] == []
        assert dao.storage_stats(p, c)["bytes"] > 0
    finally:
        c.close()


def test_safety_summary_combines_provider_and_chain_fail_closed(tmp_path):
    p = str(tmp_path / "safety.db")
    conn = sqlite3.connect(p)
    with open(REAL_SCHEMA, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.execute(
        """INSERT INTO watchlist(token_address,network_id,first_seen_at,source,
                                  watch_until,active,is_control)
           VALUES('tok','56','t0','large_buy','t1',1,0)"""
    )
    conn.execute(
        """INSERT INTO token_risk_assessments(
               token_address,network_id,recorded_at,watch_first_seen_at,is_control,
               disable_buying,disable_selling,warning_count,severe_count,high_count,
               gate_status,warning_types_json,warnings_json,raw_json)
           VALUES('tok','56','t2','t0',0,0,0,0,0,0,'pass','[]','[]','{}')"""
    )
    conn.execute(
        """INSERT INTO token_chain_assessments(
               token_address,network_id,recorded_at,watch_first_seen_at,is_control,
               chain_kind,rpc_chain_id,gate_status,reason_codes_json,
               dangerous_capabilities_json,transfer_simulation_status,details_json,raw_json)
           VALUES('tok','56','t2','t0',0,'evm','56','blocked','[]',
                  '[]','failed','{}','{}')"""
    )
    conn.commit()
    conn.close()

    ro = _conn(p)
    try:
        summary = dao.safety_summary(ro)
        assert summary == {
            "active": 1,
            "assessed": 1,
            "counts": {"pass": 0, "review": 0, "blocked": 1, "unknown": 0},
        }
        row = dao.active_watchlist(ro)[0]
        assert row["provider_status"] == "pass"
        assert row["chain_status"] == "blocked"
        assert row["combined_status"] == "blocked"
    finally:
        ro.close()


# --- المجموعة الضابطة ---
def _mixed_fixture(db_path):
    """عملتا إشارة رابحتان وعملتا ضابطة خاسرتان — فرق واضح ومعروف سلفاً."""
    c = _bars_schema(db_path)
    entry = 1785000000
    iso = datetime.fromtimestamp(entry, UTC).isoformat()
    specs = [
        ("sigA", 0, 1.0, 2.0, 1.8), ("sigB", 0, 1.0, 1.5, 1.2),
        ("ctlA", 1, 1.0, 1.1, 0.9), ("ctlB", 1, 1.0, 1.0, 0.8),
    ]
    for tok, ctl, e, peak, last in specs:
        c.execute("INSERT INTO watchlist(token_address, network_id, source, first_seen_at,"
                  " watch_until, active, is_control) VALUES(?, '56',?,?,'t',1,?)",
                  (tok, "control" if ctl else "large_buy", iso, ctl))
        c.execute(
            "INSERT INTO watch_windows VALUES(?, '56', ?, 2)",
            (tok, iso),
        )
        for ts, px in ((entry, e), (entry + 300, peak), (entry + 600, last)):
            c.execute("INSERT INTO token_bars VALUES(?, '56','5',?,?,?,?,?,1,'t')",
                      (tok, ts, px, px, px, px))
    c.commit(); c.close()


def test_performance_table_excludes_control_coins(db_path):
    """الجدول عنوانه «أداء الإشارات» — الضابطة مرجع لا صفوف فيه."""
    _mixed_fixture(db_path)
    conn = _conn(db_path)
    toks = {r["token_address"] for r in dao.watch_performance(conn, limit=10)}
    assert toks == {"sigA", "sigB"}
    conn.close()


def test_summary_excludes_control_coins(db_path):
    """وإلّا خفّضت الضابطةُ الخاسرة أرقامَ أداء الإشارة وأفسدت المعنى."""
    _mixed_fixture(db_path)
    conn = _conn(db_path)
    s = dao.performance_summary(conn)
    assert s["count"] == 2 and s["winners"] == 2 and s["losers"] == 0
    conn.close()


def test_group_comparison_separates_and_diffs_the_two_groups(db_path):
    _mixed_fixture(db_path)
    conn = _conn(db_path)
    cmp = dao.group_comparison(conn)
    assert cmp["signal"]["count"] == 2 and cmp["control"]["count"] == 2
    assert cmp["signal"]["win_rate_pct"] == 100.0
    assert cmp["control"]["win_rate_pct"] == 0.0
    assert cmp["delta"]["win_rate_pct"] == 100.0
    assert cmp["signal"]["avg_pct"] == pytest.approx(50.0)     # +80% و +20%
    assert cmp["control"]["avg_pct"] == pytest.approx(-15.0)   # -10% و -20%
    assert cmp["delta"]["avg_pct"] == pytest.approx(65.0)
    # عيّنة أصغر من 20 لكل جانب → لا تُقرأ كنتيجة
    assert cmp["sufficient"] is False
    conn.close()


def test_group_comparison_without_control_yields_no_delta(db_path):
    """بلا ضابطة لا يوجد فرق — لا نفبرك صفراً يوحي بالتعادل."""
    _perf_fixture(db_path)
    conn = _conn(db_path)
    cmp = dao.group_comparison(conn)
    assert cmp["control"]["count"] == 0
    assert all(v is None for v in cmp["delta"].values())
    assert cmp["sufficient"] is False
    conn.close()


def test_dao_tolerates_a_database_without_the_is_control_column(tmp_path):
    """اللوحة قد تسبق ترحيل المسجّل — العمود الغائب لا يُسقطها."""
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.executescript(SCHEMA.replace(
        ",\n  is_control INTEGER NOT NULL DEFAULT 0", ""))
    c.executescript("""
        CREATE TABLE token_bars (token_address TEXT, network_id TEXT, resolution TEXT,
          ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, fetched_at TEXT);
        INSERT INTO watchlist(token_address, network_id, source, first_seen_at, watch_until, active)
          VALUES('t','56','large_buy','2026-07-20T00:00:00+00:00','t',1);
        INSERT INTO token_bars VALUES('t','56','5',1785000000,1,1,1,1,1,'t');
        INSERT INTO token_bars VALUES('t','56','5',1785000600,2,2,2,2,1,'t');
    """)
    c.commit(); c.close()
    conn = _conn(p)
    rows = dao.watch_performance(conn, limit=5)
    assert len(rows) == 1 and rows[0]["is_control"] is False
    assert dao.group_comparison(conn)["control"]["count"] == 0
    conn.close()


# --- طزاجة الـ feed المصدر ---
def test_status_flags_a_frozen_upstream_feed(db_path):
    """المسجّل قد يعمل بلا خطأ بينما feed المصدر متجمّد ساعات (شوهد 3 ساعات).

    بلا هذا التمييز يبدو الأرشيف "سوقاً هادئاً" وهو انقطاع مصدر — وهو فرق
    حاسم لأي تحليل زمنيّ لاحق.
    """
    now = datetime.now(UTC)
    _seed_meta(
        db_path,
        last_cycle_at=(now - timedelta(seconds=20)).isoformat(),
        last_feed_event_at=(now - timedelta(hours=3)).isoformat(),
    )
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000, now=now)
    assert st["alive"] is True            # المسجّل حيّ
    assert st["feed_stale"] is True       # لكنّ المصدر متجمّد
    assert st["feed_age_seconds"] == pytest.approx(10800, abs=5)
    conn.close()


def test_status_fresh_feed_is_not_flagged(db_path):
    now = datetime.now(UTC)
    _seed_meta(
        db_path,
        last_cycle_at=(now - timedelta(seconds=20)).isoformat(),
        last_feed_event_at=(now - timedelta(minutes=2)).isoformat(),
    )
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000, now=now)
    assert st["feed_stale"] is False
    conn.close()


def test_status_without_feed_stamp_does_not_claim_staleness(db_path):
    """غياب المفتاح ≠ تجمّد — لا ننذر بلا دليل."""
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000)
    assert st["feed_age_seconds"] is None
    assert st["feed_stale"] is False
    conn.close()
