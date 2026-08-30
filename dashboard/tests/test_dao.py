"""اختبارات dao على قاعدة مؤقّتة (بلا شبكة، بلا مسّ recorder.db الحقيقي).

نبني قاعدة صغيرة بنفس أعمدة recorder.db، نملؤها، ثم نتحقّق أن دوال القراءة
الخالصة تعيد ما هو متوقّع — بما في ذلك منطق "حيّ خلال المهلة" ومطابقة meta.
"""
import json
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
CREATE TABLE chain_concentration (
  token_address TEXT, network_id TEXT, recorded_at TEXT,
  top1_pct REAL, top5_pct REAL, top10_pct REAL, top20_pct REAL,
  holder_count INTEGER
);
CREATE TABLE token_holders (
  token_address TEXT, network_id TEXT, recorded_at TEXT, source TEXT,
  top10_pct REAL, holder_count INTEGER
);
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


# --- التوسيم والنتائج (labeling_outcomes) ---
def _outcomes_schema(c):
    """جدول outcomes بأعمدته الحقيقيّة كما يقرؤه `labeling_outcomes`.

    الجدولُ المصغّر في SCHEMA أعلاه يُنشأ في كلّ قاعدة اختبار، فنُكمل أعمدته
    الناقصة بدل إنشائه مرّتين. `IF NOT EXISTS` يحرس قواعدَ أخرى (كاختبار
    انجراف المخطّط) تبني الجدول الكامل من `recorder/schema.sql` أوّلاً.
    """
    c.execute(
        """CREATE TABLE IF NOT EXISTS outcomes (
          kind TEXT, key TEXT, token_address TEXT, network_id TEXT,
          signal_type TEXT, is_control INTEGER, is_independent INTEGER,
          entry_ts INTEGER, entry_px REAL, entry_lag_s REAL,
          max_gain_1h REAL, max_gain_4h REAL, max_gain_24h REAL,
          max_gain_48h REAL, max_drawdown_48h REAL, final_return_48h REAL,
          time_to_peak_h REAL, candles_48h INTEGER, last_bar_lag_h REAL,
          bars_truncated INTEGER, is_rug INTEGER, split TEXT, status TEXT,
          labeled_at TEXT, suspect_bars INTEGER, design_version INTEGER,
          analysis_eligible INTEGER, exclusion_reason TEXT,
          is_explosive INTEGER, time_to_plus20_min REAL
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS training_rows (
          kind TEXT, key TEXT, token_address TEXT, network_id TEXT,
          entry_ts INTEGER, split TEXT, is_explosive INTEGER,
          built_at TEXT, feature_version INTEGER
        )"""
    )
    # إكمال الأعمدة التي يغيب عنها الجدولُ المصغّر (INSERT بأسماء أعمدة
    # يفشل على عمودٍ غير موجود).
    needed = {
        "outcomes": {
            "signal_type": "TEXT",
            "max_gain_48h": "REAL",
            "final_return_48h": "REAL",
            "labeled_at": "TEXT",
            "exclusion_reason": "TEXT",
            "is_explosive": "INTEGER",
        },
    }
    for table, columns in needed.items():
        existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns.items():
            if column not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _seed_outcome(c, **kv):
    base = dict(
        kind="watch", key=kv.get("key", "k"), token_address="t", network_id="56",
        signal_type="large_buy", is_control=0, entry_ts=1_800_000_000,
        final_return_48h=0.5, max_gain_48h=1.5, status="ok",
        design_version=3, analysis_eligible=1, is_explosive=0,
        labeled_at="2026-08-29T00:00:00+00:00",
    )
    base.update(kv)
    cols = ",".join(base)
    marks = ",".join("?" * len(base))
    c.execute(
        f"INSERT INTO outcomes({cols}) VALUES({marks})", tuple(base.values())
    )


def test_labeling_percentages_are_hundred_not_fraction(db_path):
    """النِّسَب في القاعدة كسور (0.5 = +50%) — والحدود بالمئويّات.

    انكشاف وحدةٍ خاطئة (0.5 بدل 50) يفسد كلّ قراءة في الواجهة.
    """
    c = sqlite3.connect(db_path)
    _outcomes_schema(c)
    _seed_outcome(c, key="s1", is_control=0, final_return_48h=0.5)
    _seed_outcome(c, key="s2", is_control=0, final_return_48h=-0.25)
    c.commit()
    c.close()

    conn = _conn(db_path)
    out = dao.labeling_outcomes(conn, live_start_ts=0, eta_days=14)
    conn.close()

    assert out["signal"]["count"] == 2
    assert out["signal"]["avg_return_pct"] == 12.5       # (0.5 - 0.25) / 2
    assert out["signal"]["median_return_pct"] == 12.5
    assert out["signal"]["avg_gain_pct"] == 150.0       # 1.5 كسر → 150%


def test_labeling_separates_signal_from_control_and_gates(db_path):
    """البوابة تعدّ الضابطة الناضجة وحدها؛ الإشارة لا تدخلها."""
    c = sqlite3.connect(db_path)
    _outcomes_schema(c)
    _seed_outcome(c, key="s1", is_control=0, is_explosive=1)
    _seed_outcome(c, key="s2", is_control=0)
    _seed_outcome(c, key="c1", is_control=1)
    _seed_outcome(c, key="c2", is_control=1, is_explosive=1)
    _seed_outcome(c, key="c3", is_control=1, status="no_bars")  # لا تدخل
    c.commit()
    c.close()

    conn = _conn(db_path)
    out = dao.labeling_outcomes(
        conn, live_start_ts=0, gate_targets=(1, 3)
    )
    conn.close()

    assert out["signal"]["count"] == 2
    assert out["signal"]["explosive"] == 1
    assert out["control"]["count"] == 2                 # no_bars مُقصاة
    assert out["gate"]["completed"] == 2
    assert out["gate"]["preliminary_ready"] is True     # 2 >= 1
    assert out["gate"]["decision_ready"] is False       # 2 < 3
    assert out["gate"]["remaining"] == 1


def test_labeling_eta_from_mature_days_only(db_path):
    """اليومان الأخيران (نافذة 48س غير مكتملة) لا يدخلان معدّل البوابة."""
    c = sqlite3.connect(db_path)
    _outcomes_schema(c)
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    now_ts = int(now.timestamp())
    # يومٌ ناضج قبل 3 أيام: 10 ضوابط، واليوم الحاليّ: 10 (لم تنضج بعد)
    for i in range(10):
        _seed_outcome(c, key=f"m{i}", is_control=1,
                      entry_ts=now_ts - 3 * 86400)
    for i in range(10):
        _seed_outcome(c, key=f"y{i}", is_control=1,
                      entry_ts=now_ts - 3600)
    c.commit()
    c.close()

    conn = _conn(db_path)
    out = dao.labeling_outcomes(
        conn, live_start_ts=0, gate_targets=(100, 500), eta_days=14, now=now,
    )
    conn.close()

    # 20 مكتملة، والمعدّل من اليوم الناضج وحده (10/يوم)، لا من 20/2.
    assert out["gate"]["completed"] == 20
    assert out["gate"]["avg_per_day"] == 10.0
    assert out["gate"]["remaining"] == 480
    assert out["gate"]["eta"]["days"] == pytest.approx(48.0)


def test_labeling_excludes_pre_live_era(db_path):
    """ما قبل الحِقبة الحيّة جمعٌ رجعيّ مُقصى — لا يدخل الحصيلة."""
    c = sqlite3.connect(db_path)
    _outcomes_schema(c)
    _seed_outcome(c, key="old", entry_ts=100)            # قبل الحدّ
    _seed_outcome(c, key="new", entry_ts=1_800_000_000)  # بعده
    c.commit()
    c.close()

    conn = _conn(db_path)
    out = dao.labeling_outcomes(conn, live_start_ts=1_000_000_000)
    conn.close()

    assert out["signal"]["count"] == 1


def test_labeling_training_versions_and_building_flag(db_path):
    """الإصدار الأعلى «حاليّ»؛ بناؤه الحديث علامة «قيد البناء»."""
    c = sqlite3.connect(db_path)
    _outcomes_schema(c)
    c.executemany(
        "INSERT INTO training_rows(kind, key, split, is_explosive, built_at, feature_version)"
        " VALUES('watch', ?, ?, ?, ?, ?)",
        [
            ("a", "train", 1, "2026-08-29T10:00:00+00:00", 16),
            ("b", "train", 0, "2026-08-20T10:00:00+00:00", 12),
            ("c", "test", 1, "2026-08-20T10:00:00+00:00", 12),
        ],
    )
    c.commit()
    c.close()

    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    conn = _conn(db_path)
    out = dao.labeling_outcomes(conn, live_start_ts=0, now=now)
    conn.close()

    assert out["training"]["available"] is True
    assert [v["feature_version"] for v in out["training"]["versions"]] == [16, 12]
    cur = out["training"]["current"]
    assert cur["feature_version"] == 16
    assert cur["building"] is True                    # بُني قبل ساعتين


def test_labeling_status_counts_and_exclusions(db_path):
    c = sqlite3.connect(db_path)
    _outcomes_schema(c)
    _seed_outcome(c, key="ok1", kind="watch", status="ok")
    _seed_outcome(c, key="ne1", kind="signal", status="no_entry",
                  exclusion_reason="signal_outcome_not_phase1_watch")
    _seed_outcome(c, key="nb1", kind="watch", status="no_bars",
                  exclusion_reason="age_unknown_at_entry")
    c.commit()
    c.close()

    conn = _conn(db_path)
    out = dao.labeling_outcomes(conn, live_start_ts=0)
    conn.close()

    by_key = {(r["kind"], r["status"]): r["count"] for r in out["status_counts"]}
    assert by_key == {("watch", "ok"): 1, ("signal", "no_entry"): 1, ("watch", "no_bars"): 1}
    reasons = {e["reason"]: e["count"] for e in out["exclusions"]}
    assert reasons["signal_outcome_not_phase1_watch"] == 1
    assert reasons["age_unknown_at_entry"] == 1


def test_labeling_without_outcomes_table(db_path):
    """الغِياب حالةٌ صالحة — لا انهيار.

    الجدولُ المصغّر في SCHEMA موجودٌ دائماً لكن تنقصه أعمدة التوسيم، فيمسكه
    حرسُ الأعمدة — وهو بحدّ ذاته سلوكٌ محروس: لوحةٌ تسبق ترحيلَ المسجّل
    تعرض «لا بيانات» لا انهياراً.
    """
    conn = _conn(db_path)
    out = dao.labeling_outcomes(conn, live_start_ts=0)
    conn.close()
    assert out["live"] is False
    assert "is_explosive" in out.get("missing_columns", [])


def test_last_labeled_at_reads_the_latest_stamp(db_path):
    c = sqlite3.connect(db_path)
    _outcomes_schema(c)
    _seed_outcome(c, key="a", labeled_at="2026-08-28T00:00:00+00:00")
    _seed_outcome(c, key="b", labeled_at="2026-08-29T00:00:00+00:00")
    c.commit()
    c.close()

    conn = _conn(db_path)
    assert dao.last_labeled_at(conn) == "2026-08-29T00:00:00+00:00"
    conn.close()


def test_status_missing_meta_keys_default(db_path):
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000)
    assert st["alive"] is False
    assert st["cycles_total"] == 0
    assert st["errors_total"] == 0          # المفتاح غائب → 0 لا استثناء
    assert st["seconds_since_last_cycle"] is None
    conn.close()


def test_status_exposes_per_network_evm_admission(db_path):
    _seed_meta(
        db_path,
        evm_admission_network_state=json.dumps({
            "143": {"percent": 100, "paused": False, "reason": "ok"},
            "8453": {"percent": 0, "paused": True, "reason": "active_retry"},
        }),
        evm_admission_paused_networks=json.dumps(["8453"]),
    )
    conn = _conn(db_path)
    status = dao.recorder_status(conn, 150, 2000)
    conn.close()

    assert status["evm_admission_networks"]["143"]["percent"] == 100
    assert status["evm_admission_networks"]["8453"]["reason"] == "active_retry"
    assert status["evm_admission_paused_networks"] == ["8453"]


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


def test_chain_error_heals_from_its_own_stamp_when_the_recorder_is_dead(db_path):
    """العطبُ المقيس يوم 2026-08-17: مات المسجّل ٣س١٤د فتجمّد حدُّ التقادم
    (`last_ok_cycle_at` لا يكتبه غيره)، فبقيت شارة chain حمراء وخطؤها قد شُفي
    و`FomoChain` تُتمّ دوراتها النظيفة. الآن ختمُ الطابور نفسه هو الحدّ."""
    _seed_meta(
        db_path,
        started_at="2026-08-17T10:00:00+00:00",
        last_ok_cycle_at="2026-08-17T16:00:00+00:00",     # المسجّل مات هنا
        last_error_chain="2026-08-17T18:23:34+00:00: ChainRPCError: -32600",
        chain_last_ok_at="2026-08-17T19:30:00+00:00",     # وFomoChain تعمل
    )
    conn = _conn(db_path)
    row = next(
        e for e in dao.recorder_errors(
            conn, ("chain",), ok_stamps={"chain": ("chain_last_ok_at",)},
        ) if e["source"] == "chain"
    )
    assert row["stale"] is True
    assert row["ok_at"] == "2026-08-17T19:30:00+00:00"
    conn.close()


def test_one_queue_stamp_does_not_heal_another_queues_error(db_path):
    """نجاح طابور التركّز لا يعني نجاح طابور الصلاحيات — ختمٌ لكلٍّ منهما."""
    _seed_meta(
        db_path,
        chain_last_ok_at="2026-08-17T19:30:00+00:00",
        last_error_chain_auth="2026-08-17T18:00:00+00:00: HTTP 522",
    )
    conn = _conn(db_path)
    stamps = {"chain": ("chain_last_ok_at",), "chain_auth": ("chain_auth_last_ok_at",)}
    row = next(
        e for e in dao.recorder_errors(conn, ("chain_auth",), ok_stamps=stamps)
        if e["source"] == "chain_auth"
    )
    assert row["stale"] is False
    assert row["ok_at"] is None
    conn.close()


def test_recorder_boundary_cannot_heal_a_source_with_a_missing_own_stamp(db_path):
    """مصدرٌ مستقل فشل قبل أول نجاح: ختم المسجّل لا يثبت تعافيه.

    كان fallback القديم يفيد أثناء ترحيل الأختام، لكنه يخفي الآن فشل أول تشغيل:
    `activity_head` كتب 401 بينما المسجّل سليم، فكان سيظهر الخطأ قديمًا بلا نجاح
    واحد للعامل نفسه. وجود المصدر في `ok_stamps` عقدٌ fail-closed.
    """
    _seed_meta(
        db_path,
        last_ok_cycle_at="2026-08-17T19:00:00+00:00",
        last_error_evm="2026-08-17T16:41:20+00:00: ReadTimeout",
    )
    conn = _conn(db_path)
    row = next(
        e for e in dao.recorder_errors(
            conn, ("evm",), ok_stamps={"evm": ("evm_last_ok_at",)},
        ) if e["source"] == "evm"
    )
    assert row["stale"] is False
    assert row["ok_at"] is None
    conn.close()


# --- provider keys ---
def _seed_pool(db_path, owner, pools, at="2026-08-17T20:00:00+00:00"):
    _seed_meta(db_path, **{
        f"provider_keys_{owner}": json.dumps({"at": at, "owner": owner, "pools": pools}),
    })


def test_provider_keys_warns_when_a_pool_has_no_spare_key(db_path):
    """مفتاحٌ واحد: التدوير موجود في الكود ولا ينفع بحوضٍ من واحد ⇒ تحذير."""
    _seed_pool(db_path, "chain", {
        "helius": {"keys": 1, "blocked": 0, "available": 1, "index": 0, "rotations": 0},
        "nodereal": {"keys": 3, "blocked": 0, "available": 3, "index": 1, "rotations": 4},
    })
    conn = _conn(db_path)
    rows = dao.provider_keys(conn, now=datetime(2026, 8, 17, 20, 1, tzinfo=UTC))
    by_provider = {r["provider"]: r for r in rows}
    assert by_provider["helius"]["level"] == "warn"
    assert by_provider["nodereal"]["level"] == "good"
    assert by_provider["nodereal"]["rotations"] == 4
    conn.close()


def test_provider_keys_flags_a_pool_whose_keys_are_all_cooling_down(db_path):
    _seed_pool(db_path, "chain", {
        "helius": {"keys": 2, "blocked": 2, "available": 0, "index": 0, "rotations": 9},
    })
    conn = _conn(db_path)
    rows = dao.provider_keys(conn, now=datetime(2026, 8, 17, 20, 1, tzinfo=UTC))
    assert rows[0]["level"] == "bad"
    conn.close()


def test_provider_keys_flags_a_provider_disabled_for_the_process_lifetime(db_path):
    """نفادُ رصيدِ مزوّدٍ (402) يُسكِته لبقيّة العمر: أحواضٌ سليمة ومزوّدٌ ميت."""
    _seed_pool(db_path, "chain", {
        "nodereal": {"keys": 2, "blocked": 0, "available": 2, "disabled": True},
    })
    conn = _conn(db_path)
    rows = dao.provider_keys(conn, now=datetime(2026, 8, 17, 20, 1, tzinfo=UTC))
    assert (rows[0]["level"], rows[0]["disabled"]) == ("bad", True)
    conn.close()


def test_provider_keys_keeps_two_pools_of_one_provider_apart(db_path):
    """حوضان لنفس المزوّد في عمليّتين: دمجُهما يخفي عطبَ إحداهما تحت الأخرى."""
    _seed_pool(db_path, "chain", {"helius": {"keys": 2, "available": 2}})
    _seed_pool(db_path, "replay", {"helius": {"keys": 2, "available": 0}})
    conn = _conn(db_path)
    rows = dao.provider_keys(conn, now=datetime(2026, 8, 17, 20, 1, tzinfo=UTC))
    assert [(r["owner"], r["level"]) for r in rows] == [("chain", "good"), ("replay", "bad")]
    conn.close()


def test_provider_keys_marks_a_frozen_report_as_stale(db_path):
    """تقريرٌ متجمّد = العمليّة المالكة لم تُتمّ دورة؛ أعدادُه ماضٍ لا حاضر."""
    _seed_pool(db_path, "chain", {"helius": {"keys": 2, "available": 2}},
               at="2026-08-17T10:00:00+00:00")
    conn = _conn(db_path)
    rows = dao.provider_keys(conn, now=datetime(2026, 8, 17, 20, 0, tzinfo=UTC))
    assert rows[0]["stale"] is True
    assert rows[0]["age_seconds"] == 36000
    conn.close()


def test_provider_keys_ignores_a_corrupt_report_instead_of_failing(db_path):
    """سطرٌ نصفُ مكتوب لا يُفرغ اللوحة كلّها."""
    _seed_meta(db_path, provider_keys_chain="{not json")
    conn = _conn(db_path)
    assert dao.provider_keys(conn) == []
    conn.close()


def test_network_summary_reports_chain_coverage(db_path):
    c = sqlite3.connect(db_path)
    c.executemany(
        "INSERT INTO watchlist VALUES(?,?,?,?,?,?,?,?)",
        [
            ("sol", "1399811149", "t", "large_buy", "t2", "s", 1, 0),
            ("bsc", "56", "t", "large_buy", "t2", "s", 1, 0),
            ("old", "56", "t", "large_buy", "t2", "s", 0, 0),
        ],
    )
    c.executemany(
        "INSERT INTO token_holders VALUES(?,?,?,?,?,?)",
        [
            ("sol", "1399811149", "2026-08-14T00:00:00+00:00", "token_details", 30, 500),
            ("bsc", "56", "2026-08-14T00:00:00+00:00", "token_details", 31, 100),
        ],
    )
    c.executemany(
        "INSERT INTO chain_concentration VALUES(?,?,?,?,?,?,?,?)",
        [
            ("sol", "1399811149", "2026-08-14T00:00:00+00:00", 10, 20, 30, 40, None),
            ("bsc", "56", "2026-08-13T00:00:00+00:00", 1, 2, 3, 4, 90),
            ("bsc", "56", "2026-08-14T00:00:00+00:00", 11, 21, 31, 41, 100),
            ("old", "56", "2026-08-14T00:00:00+00:00", 12, 22, 32, 42, 200),
        ],
    )
    c.commit()
    c.close()

    conn = _conn(db_path)
    result = {row["network_id"]: row for row in dao.network_summary(conn)}
    conn.close()

    assert result["56"]["active_watches"] == 1
    assert result["56"]["historical_watches"] == 1
    assert result["56"]["concentration_rows"] == 1
    assert result["56"]["holder_count_rows"] == 1
    assert result["56"]["details_holder_rows"] == 1
    assert result["1399811149"]["top20_rows"] == 1
    assert result["1399811149"]["historical_watches"] == 0
    assert result["1399811149"]["details_holder_rows"] == 1


def test_network_summary_tolerates_pre_chain_schema(tmp_path):
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE watchlist (token_address TEXT, network_id TEXT, active INTEGER)")
    c.execute("INSERT INTO watchlist VALUES('token', '56', 1)")
    c.commit()
    c.close()

    conn = _conn(p)
    result = dao.network_summary(conn)
    conn.close()

    assert result == [{
        "network_id": "56", "active_watches": 1, "concentration_rows": 0,
        "historical_watches": 0,
        "top1_rows": 0, "top5_rows": 0, "top10_rows": 0, "top20_rows": 0,
        "historical_concentration_rows": 0, "historical_top1_rows": 0,
        "historical_top5_rows": 0, "historical_top10_rows": 0, "historical_top20_rows": 0,
        "holder_count_rows": 0, "details_holder_rows": 0,
        "details_top10_rows": 0, "tick_rows": 0,
        "latest_concentration": None, "latest_details": None, "latest_tick": None,
    }]


def test_network_summary_tolerates_missing_holder_count_column(tmp_path):
    p = str(tmp_path / "old-chain.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE watchlist (token_address TEXT, network_id TEXT, active INTEGER)")
    c.execute("""CREATE TABLE chain_concentration (
        token_address TEXT, network_id TEXT, recorded_at TEXT,
        top1_pct REAL, top5_pct REAL, top10_pct REAL, top20_pct REAL
    )""")
    c.execute("INSERT INTO watchlist VALUES('token', '56', 1)")
    c.execute("INSERT INTO chain_concentration VALUES('token','56','t',1,2,3,4)")
    c.commit()
    c.close()

    conn = _conn(p)
    row = dao.network_summary(conn)[0]
    conn.close()

    assert row["concentration_rows"] == 1
    assert row["top20_rows"] == 1
    assert row["holder_count_rows"] == 0


def test_network_summary_excludes_null_network_ticks(db_path):
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO market_ticks(token_address, network_id, recorded_at) VALUES('x', NULL, 't')"
    )
    c.commit()
    c.close()

    conn = _conn(db_path)
    rows = dao.network_summary(conn)
    conn.close()

    assert rows == []


# --- طزاجةُ الشبكات: القفزةُ الحيّة وطبقتُها فوق المخزَّن ---
def test_latest_tick_per_active_network_matches_full_scan(db_path):
    """القفزةُ الرخيصة تعطي نفسَ ختمِ المسحِ الكامل للشبكات العاملة.

    هذا هو الاختبارُ الذي يحرس التبديلَ نفسه: لو انحرفت القفزةُ عن التجميع
    الكامل لصار سطرُ «آخر سوق» يكذب بلا أن يُلاحظ — والفرقُ ثوانٍ لا ساعات.
    """
    c = sqlite3.connect(db_path)
    c.executemany(
        "INSERT INTO watchlist VALUES(?,?,?,?,?,?,?,?)",
        [
            ("sol", "1399811149", "t", "large_buy", "t2", "s", 1, 0),
            ("bsc", "56", "t", "large_buy", "t2", "s", 1, 0),
        ],
    )
    c.executemany(
        "INSERT INTO market_ticks(token_address, network_id, recorded_at) VALUES(?,?,?)",
        [
            ("sol", "1399811149", "2026-08-18T10:00:00+00:00"),
            ("sol", "1399811149", "2026-08-18T11:00:00+00:00"),
            ("bsc", "56", "2026-08-18T09:00:00+00:00"),
        ],
    )
    c.commit()
    c.close()

    conn = _conn(db_path)
    live = dao.latest_tick_per_active_network(conn)
    full = {r["network_id"]: r["latest_tick"] for r in dao.network_summary(conn)}
    conn.close()

    assert live == {
        "1399811149": "2026-08-18T11:00:00+00:00",
        "56": "2026-08-18T09:00:00+00:00",
    }
    assert live == {k: v for k, v in full.items() if k in live}


def test_latest_tick_per_active_network_ignores_inactive_and_blank(db_path):
    """يعمى عمداً عن المراقبةِ المنتهية وعن شبكةٍ فارغةِ المعرِّف."""
    c = sqlite3.connect(db_path)
    c.executemany(
        "INSERT INTO watchlist VALUES(?,?,?,?,?,?,?,?)",
        [
            ("old", "56", "t", "large_buy", "t2", "s", 0, 0),
            ("blank", "", "t", "large_buy", "t2", "s", 1, 0),
            ("none", None, "t", "large_buy", "t2", "s", 1, 0),
        ],
    )
    c.executemany(
        "INSERT INTO market_ticks(token_address, network_id, recorded_at) VALUES(?,?,?)",
        [
            ("old", "56", "2026-08-18T10:00:00+00:00"),
            ("blank", "", "2026-08-18T10:00:00+00:00"),
            ("none", None, "2026-08-18T10:00:00+00:00"),
        ],
    )
    c.commit()
    c.close()

    conn = _conn(db_path)
    assert dao.latest_tick_per_active_network(conn) == {}
    conn.close()


def test_latest_tick_per_active_network_tolerates_pre_chain_schema(tmp_path):
    """قاعدةٌ بلا `market_ticks` تعيد قاموساً فارغاً لا تنهار."""
    p = str(tmp_path / "no-ticks.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE watchlist (token_address TEXT, network_id TEXT, active INTEGER)")
    c.execute("INSERT INTO watchlist VALUES('token','56',1)")
    c.commit()
    c.close()

    conn = _conn(p)
    assert dao.latest_tick_per_active_network(conn) == {}
    conn.close()


def test_with_live_latest_tick_lifts_stale_stamp():
    rows = [{"network_id": "56", "latest_tick": "2026-08-18T09:00:00+00:00", "tick_rows": 7}]
    merged = dao.with_live_latest_tick(rows, {"56": "2026-08-18T11:00:00+00:00"})
    assert merged[0]["latest_tick"] == "2026-08-18T11:00:00+00:00"
    assert merged[0]["tick_rows"] == 7          # بقيّةُ الحقول تمرّ كما هي


def test_with_live_latest_tick_never_regresses():
    """المخزَّنُ الأحدثُ يبقى: الحيُّ أعمى عن عملةٍ توقّفت بعد آخر لقطةٍ لها."""
    rows = [{"network_id": "56", "latest_tick": "2026-08-18T12:00:00+00:00"}]
    merged = dao.with_live_latest_tick(rows, {"56": "2026-08-18T09:00:00+00:00"})
    assert merged[0]["latest_tick"] == "2026-08-18T12:00:00+00:00"


def test_with_live_latest_tick_fills_missing_stamp():
    rows = [{"network_id": "56", "latest_tick": None}]
    merged = dao.with_live_latest_tick(rows, {"56": "2026-08-18T09:00:00+00:00"})
    assert merged[0]["latest_tick"] == "2026-08-18T09:00:00+00:00"


def test_with_live_latest_tick_keeps_networks_without_live_stamp():
    """شبكةٌ بلا مراقبةٍ نشطة تبقى في القائمة بختمها المخزَّن — لا تُحذف."""
    rows = [
        {"network_id": "143", "latest_tick": "2026-08-15T00:00:00+00:00"},
        {"network_id": "56", "latest_tick": "2026-08-18T09:00:00+00:00"},
    ]
    merged = dao.with_live_latest_tick(rows, {"56": "2026-08-18T11:00:00+00:00"})
    assert [r["network_id"] for r in merged] == ["143", "56"]
    assert merged[0]["latest_tick"] == "2026-08-15T00:00:00+00:00"


def test_with_live_latest_tick_does_not_mutate_source_rows():
    """الصفوفُ الواردة قد تكون في ذاكرةٍ مؤقّتة يتشاركها طلباتٌ متوازية."""
    rows = [{"network_id": "56", "latest_tick": "2026-08-18T09:00:00+00:00"}]
    merged = dao.with_live_latest_tick(rows, {"56": "2026-08-18T11:00:00+00:00"})
    assert rows[0]["latest_tick"] == "2026-08-18T09:00:00+00:00"
    assert merged[0] is not rows[0]


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
    c.commit()
    c.close()
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
    c.commit()
    c.close()
    conn = _conn(db_path)
    wl = dao.active_watchlist(conn)
    assert len(wl) == 1                            # النشط فقط
    assert wl[0]["token_address"] == "tokA"
    assert wl[0]["tick_count"] == 3
    conn.close()


def test_active_watchlist_first_ever_from_watch_windows(db_path):
    """أوّل التقاط من السجلّ غير القابل للكتابة فوقه، لا من الصفّ الحيّ.

    `upsert_watch` يكتب فوق `watchlist.first_seen_at` عند كل إعادة قبول، فعملة
    مُلتقطة منذ أسبوع تظهر بعمر ساعات. `watch_windows` يحفظ كل نافذة.
    """
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO watchlist(token_address, network_id, source, first_seen_at,"
        " watch_until, active) VALUES('tokA','56','feed',"
        "'2026-08-08T23:17:00Z','2026-08-10T23:17:00Z',1)"
    )
    for ts in ("2026-08-02T22:42:13Z", "2026-08-05T10:00:00Z", "2026-08-08T23:17:00Z"):
        c.execute("INSERT INTO watch_windows VALUES('tokA','56',?,3)", (ts,))
    c.commit()
    c.close()
    conn = _conn(db_path)
    row = dao.active_watchlist(conn)[0]
    assert row["first_ever_at"] == "2026-08-02T22:42:13Z"   # أوّل نافذة
    assert row["first_seen_at"] == "2026-08-08T23:17:00Z"   # الدورة الحالية تبقى
    conn.close()


def test_active_watchlist_first_ever_ignores_other_token(db_path):
    """النافذة تُنسب بالعنوان والشبكة معاً، فلا تُخلط عملة بأخرى."""
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO watchlist(token_address, network_id, source, first_seen_at,"
        " watch_until, active) VALUES('tokA','56','feed',"
        "'2026-08-08T00:00:00Z','2026-08-10T00:00:00Z',1)"
    )
    c.execute("INSERT INTO watch_windows VALUES('tokB','56','2026-07-01T00:00:00Z',3)")
    c.execute("INSERT INTO watch_windows VALUES('tokA','99','2026-07-02T00:00:00Z',3)")
    c.execute("INSERT INTO watch_windows VALUES('tokA','56','2026-08-08T00:00:00Z',3)")
    c.commit()
    c.close()
    conn = _conn(db_path)
    row = dao.active_watchlist(conn)[0]
    assert row["first_ever_at"] == "2026-08-08T00:00:00Z"
    conn.close()


def test_active_watchlist_first_ever_falls_back_without_window(db_path):
    """عملة بلا نافذة مسجّلة (ما قبل الجدول) تعود إلى طابعها الحيّ لا NULL."""
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO watchlist(token_address, network_id, source, first_seen_at,"
        " watch_until, active) VALUES('tokA','56','feed',"
        "'2026-08-08T00:00:00Z','2026-08-10T00:00:00Z',1)"
    )
    c.commit()
    c.close()
    conn = _conn(db_path)
    row = dao.active_watchlist(conn)[0]
    assert row["first_ever_at"] is None       # MIN على لا شيء
    assert row["first_seen_at"] == "2026-08-08T00:00:00Z"
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
    c.commit()
    c.close()
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
    c.commit()
    c.close()
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
    c.commit()
    c.close()
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
    c.commit()
    c.close()

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
    c.commit()
    c.close()

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
    c.commit()
    c.close()


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
    c.commit()
    c.close()
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
        assert dao.ticks_summary(c)["total"] == 0
        assert dao.network_summary(c) == []
        assert dao.bars_coverage(c, 0)["candles"] == 0
        assert dao.watch_performance(c, 10) == []
        assert dao.performance_summary(c)["count"] == 0
        assert dao.token_series(c, "x", "56", 0) == []
        assert dao.signal_timeline(c, 6)["types"] == []
        assert dao.storage_stats(p, c)["bytes"] > 0
        # التوسيم والنتائج — على المخطّط الحقيقيّ (عمود entry_ts موجودٌ فيه)
        labeling = dao.labeling_outcomes(c, live_start_ts=0)
        assert labeling["live"] is True
        assert labeling["signal"]["count"] == 0
        assert labeling["control"]["count"] == 0
        assert labeling["gate"]["completed"] == 0
        assert dao.last_labeled_at(c) is None
    finally:
        c.close()


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
    c.commit()
    c.close()


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
    c.commit()
    c.close()
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


def test_a_healthy_recorder_no_longer_heals_a_queue_that_has_not_succeeded(db_path):
    """صحّةُ المسجّل لا تشفي خطأَ طابورٍ له ختمُه ولم ينجح بعد.

    قِيس 2026-08-19: حُجب مسارُ التجّار 21 ساعة والمسجّل يُتمّ دوراتِه بـ
    `errors: 0` كلَّ دقيقة. مع `max(own, recorder)` كان أيُّ خطأٍ يُكتب هناك
    يُعلَن «متعافياً» بعد دقيقةٍ من كتابته — أي أنّ نفسَ الصحّةِ الكاذبة التي
    أخفت الانقطاع كانت ستُخفي إعلانَه أيضاً.
    """
    _seed_meta(
        db_path,
        last_ok_cycle_at="2026-08-20T13:00:00+00:00",       # المسجّل بخير الآن
        traders_last_ok_at="2026-08-19T14:53:51+00:00",     # وهذا المسار لا
        last_error_traders="2026-08-20T12:00:00+00:00: ShutoutSuspected: 50",
    )
    conn = _conn(db_path)
    row = next(
        e for e in dao.recorder_errors(
            conn, ("traders",), ok_stamps={"traders": ("traders_last_ok_at",)},
        ) if e["source"] == "traders"
    )
    assert row["stale"] is False
    assert row["ok_at"] == "2026-08-19T14:53:51+00:00"
    conn.close()


def test_an_own_stamp_newer_than_the_error_still_heals_it(db_path):
    """والشفاءُ يبقى ممكناً: ختمٌ خاصٌّ أحدثُ من الخطأ يُقادمه كما كان."""
    _seed_meta(
        db_path,
        last_ok_cycle_at="2026-08-20T13:00:00+00:00",
        traders_last_ok_at="2026-08-20T13:05:00+00:00",
        last_error_traders="2026-08-20T12:00:00+00:00: ShutoutSuspected: 50",
    )
    conn = _conn(db_path)
    row = next(
        e for e in dao.recorder_errors(
            conn, ("traders",), ok_stamps={"traders": ("traders_last_ok_at",)},
        ) if e["source"] == "traders"
    )
    assert row["stale"] is True
    conn.close()
