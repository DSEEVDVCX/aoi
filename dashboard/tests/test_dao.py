"""dao tests against a temporary database (no network, no touching the real recorder.db).

We build a small database with the same columns as recorder.db, fill it, then
check that the pure read functions return what's expected — including the
"alive within the deadline" logic and meta matching.
"""
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import dao

# A miniature schema matching the columns dao reads.
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

# The real recorder schema path — used in the schema-drift test below.
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


# --- connect_ro blocks writes ---
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
    """The labeler's death is silent — it's only exposed by its stamp going old."""
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


# --- labeling and outcomes (labeling_outcomes) ---
def _outcomes_schema(c):
    """The outcomes table with its real columns as `labeling_outcomes` reads it.

    The miniature table in SCHEMA above is created in every test database, so
    we complete its missing columns instead of creating it twice. `IF NOT
    EXISTS` guards other databases (like the schema-drift test) that build
    the full table from `recorder/schema.sql` first.
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
    # Complete the columns the miniature table lacks (an INSERT with column
    # names fails on a nonexistent column).
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
    """The ratios in the database are fractions (0.5 = +50%) — and the thresholds are percentages.

    A wrong unit slipping through (0.5 instead of 50) corrupts every reading
    in the interface.
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
    assert out["signal"]["avg_gain_pct"] == 150.0       # 1.5 as a fraction → 150%


def test_labeling_separates_signal_from_control_and_gates(db_path):
    """The gate counts only the mature control; signals don't enter it."""
    c = sqlite3.connect(db_path)
    _outcomes_schema(c)
    _seed_outcome(c, key="s1", is_control=0, is_explosive=1)
    _seed_outcome(c, key="s2", is_control=0)
    _seed_outcome(c, key="c1", is_control=1)
    _seed_outcome(c, key="c2", is_control=1, is_explosive=1)
    _seed_outcome(c, key="c3", is_control=1, status="no_bars")  # doesn't enter
    c.commit()
    c.close()

    conn = _conn(db_path)
    out = dao.labeling_outcomes(
        conn, live_start_ts=0, gate_targets=(1, 3)
    )
    conn.close()

    assert out["signal"]["count"] == 2
    assert out["signal"]["explosive"] == 1
    assert out["control"]["count"] == 2                 # no_bars is excluded
    assert out["gate"]["completed"] == 2
    assert out["gate"]["preliminary_ready"] is True     # 2 >= 1
    assert out["gate"]["decision_ready"] is False       # 2 < 3
    assert out["gate"]["remaining"] == 1


def test_labeling_eta_from_mature_days_only(db_path):
    """The last two days (an incomplete 48h window) don't enter the gate's rate."""
    c = sqlite3.connect(db_path)
    _outcomes_schema(c)
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    now_ts = int(now.timestamp())
    # a mature day 3 days ago: 10 controls, and the current day: 10 (not mature yet)
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

    # 20 completed, and the rate from the mature day alone (10/day), not from 20/2.
    assert out["gate"]["completed"] == 20
    assert out["gate"]["avg_per_day"] == 10.0
    assert out["gate"]["remaining"] == 480
    assert out["gate"]["eta"]["days"] == pytest.approx(48.0)


def test_labeling_excludes_pre_live_era(db_path):
    """Before the live era is retro collection, excluded — it doesn't enter the tally."""
    c = sqlite3.connect(db_path)
    _outcomes_schema(c)
    _seed_outcome(c, key="old", entry_ts=100)            # before the boundary
    _seed_outcome(c, key="new", entry_ts=1_800_000_000)  # after it
    c.commit()
    c.close()

    conn = _conn(db_path)
    out = dao.labeling_outcomes(conn, live_start_ts=1_000_000_000)
    conn.close()

    assert out["signal"]["count"] == 1


def test_labeling_training_versions_and_building_flag(db_path):
    """The highest version is "current"; a recent build of it is a "building" flag."""
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
    assert cur["building"] is True                    # built two hours ago


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
    """Absence is a valid state — no crash.

    The miniature table in SCHEMA always exists but lacks the labeling
    columns, so the column guard catches it — which is itself a guarded
    behavior: a dashboard ahead of the recorder's migration shows "no data",
    not a crash.
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
    assert st["errors_total"] == 0          # key absent → 0, not an exception
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
    assert feed["stale"] is False   # no timestamp and no boundary → considered current
    conn.close()


def test_recorder_errors_stale_when_older_than_started(db_path):
    # an error stamped before started_at (a recorder process that died) → stale/recovered.
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
    # an error older than the last fully successful cycle → stale, despite no process restart.
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
    """The incident measured on 2026-08-17: the recorder died for 3h14m, so the
    staleness boundary froze (`last_ok_cycle_at` is written by nothing else),
    and the chain badge stayed red while its error had healed and `FomoChain`
    was completing its clean cycles. Now the queue's own stamp is the
    boundary."""
    _seed_meta(
        db_path,
        started_at="2026-08-17T10:00:00+00:00",
        last_ok_cycle_at="2026-08-17T16:00:00+00:00",     # the recorder died here
        last_error_chain="2026-08-17T18:23:34+00:00: ChainRPCError: -32600",
        chain_last_ok_at="2026-08-17T19:30:00+00:00",     # and FomoChain is working
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
    """The concentration queue's success doesn't mean the auth queue's success — a stamp for each."""
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
    """An independent source that failed before its first success: the
    recorder's stamp doesn't prove its recovery.

    The old fallback helped during the stamp migration, but now it hides a
    first-run failure: `activity_head` wrote 401 while the recorder was
    healthy, and the error would have shown as stale with not one success for
    the worker itself. Having the source in `ok_stamps` is a fail-closed
    contract.
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
    """One key: rotation exists in the code and doesn't help with a pool of one ⇒ warning."""
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
    """A provider running out of credits (402) is silenced for the rest of its lifetime: healthy pools and a dead provider."""
    _seed_pool(db_path, "chain", {
        "nodereal": {"keys": 2, "blocked": 0, "available": 2, "disabled": True},
    })
    conn = _conn(db_path)
    rows = dao.provider_keys(conn, now=datetime(2026, 8, 17, 20, 1, tzinfo=UTC))
    assert (rows[0]["level"], rows[0]["disabled"]) == ("bad", True)
    conn.close()


def test_provider_keys_keeps_two_pools_of_one_provider_apart(db_path):
    """Two pools for one provider in two processes: merging them hides one's failure under the other."""
    _seed_pool(db_path, "chain", {"helius": {"keys": 2, "available": 2}})
    _seed_pool(db_path, "replay", {"helius": {"keys": 2, "available": 0}})
    conn = _conn(db_path)
    rows = dao.provider_keys(conn, now=datetime(2026, 8, 17, 20, 1, tzinfo=UTC))
    assert [(r["owner"], r["level"]) for r in rows] == [("chain", "good"), ("replay", "bad")]
    conn.close()


def test_provider_keys_marks_a_frozen_report_as_stale(db_path):
    """A frozen report = the owning process didn't complete a cycle; its numbers are the past, not the present."""
    _seed_pool(db_path, "chain", {"helius": {"keys": 2, "available": 2}},
               at="2026-08-17T10:00:00+00:00")
    conn = _conn(db_path)
    rows = dao.provider_keys(conn, now=datetime(2026, 8, 17, 20, 0, tzinfo=UTC))
    assert rows[0]["stale"] is True
    assert rows[0]["age_seconds"] == 36000
    conn.close()


def test_provider_keys_ignores_a_corrupt_report_instead_of_failing(db_path):
    """A half-written line doesn't empty the whole dashboard."""
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


# --- networks freshness: the live lookup and its overlay on the cached rows ---
def test_latest_tick_per_active_network_matches_full_scan(db_path):
    """The cheap lookup gives the same stamp as the full scan for active networks.

    This is the test that guards the switch itself: if the lookup drifted
    from the full aggregation, the "last market" line would lie unnoticed —
    and the difference is seconds, not hours.
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
    """Deliberately blind to an ended watch and to a network with a blank identifier."""
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
    """A database without `market_ticks` returns an empty dict, not a crash."""
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
    assert merged[0]["tick_rows"] == 7          # the rest of the fields pass through as they are


def test_with_live_latest_tick_never_regresses():
    """The newer cached one stays: the live lookup is blind to a coin that stopped after its last snapshot."""
    rows = [{"network_id": "56", "latest_tick": "2026-08-18T12:00:00+00:00"}]
    merged = dao.with_live_latest_tick(rows, {"56": "2026-08-18T09:00:00+00:00"})
    assert merged[0]["latest_tick"] == "2026-08-18T12:00:00+00:00"


def test_with_live_latest_tick_fills_missing_stamp():
    rows = [{"network_id": "56", "latest_tick": None}]
    merged = dao.with_live_latest_tick(rows, {"56": "2026-08-18T09:00:00+00:00"})
    assert merged[0]["latest_tick"] == "2026-08-18T09:00:00+00:00"


def test_with_live_latest_tick_keeps_networks_without_live_stamp():
    """A network with no active watch stays in the list with its cached stamp — it isn't deleted."""
    rows = [
        {"network_id": "143", "latest_tick": "2026-08-15T00:00:00+00:00"},
        {"network_id": "56", "latest_tick": "2026-08-18T09:00:00+00:00"},
    ]
    merged = dao.with_live_latest_tick(rows, {"56": "2026-08-18T11:00:00+00:00"})
    assert [r["network_id"] for r in merged] == ["143", "56"]
    assert merged[0]["latest_tick"] == "2026-08-15T00:00:00+00:00"


def test_with_live_latest_tick_does_not_mutate_source_rows():
    """The incoming rows may be in a cache shared by parallel requests."""
    rows = [{"network_id": "56", "latest_tick": "2026-08-18T09:00:00+00:00"}]
    merged = dao.with_live_latest_tick(rows, {"56": "2026-08-18T11:00:00+00:00"})
    assert rows[0]["latest_tick"] == "2026-08-18T09:00:00+00:00"
    assert merged[0] is not rows[0]


def test_recorder_errors_active_when_newer_than_boundary(db_path):
    # an error newer than the last successful cycle and started_at → current (not stale).
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
    assert [r["id"] for r in rows] == ["b", "a"]   # newest first
    assert rows[1]["top_trader_match_count"] == 1
    assert rows[1]["buyers_best_rank"] == 4
    conn.close()


def test_recent_signals_limit_clamped(db_path):
    conn = _conn(db_path)
    assert dao.recent_signals(conn, 0) == []       # no rows, but no exception
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
        "VALUES('tokB','feed','2026-07-25T00:00:00Z','2026-07-27T00:00:00Z',0)"  # inactive
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
    assert len(wl) == 1                            # the active one only
    assert wl[0]["token_address"] == "tokA"
    assert wl[0]["tick_count"] == 3
    conn.close()


def test_active_watchlist_first_ever_from_watch_windows(db_path):
    """The first catch comes from the log that can't be overwritten, not from the live row.

    `upsert_watch` overwrites `watchlist.first_seen_at` on every re-admission,
    so a coin caught a week ago shows an age of hours. `watch_windows` keeps
    every window.
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
    assert row["first_ever_at"] == "2026-08-02T22:42:13Z"   # the first window
    assert row["first_seen_at"] == "2026-08-08T23:17:00Z"   # the current cycle stays
    conn.close()


def test_active_watchlist_first_ever_ignores_other_token(db_path):
    """A window is attributed by address and network together, so one coin isn't mixed with another."""
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
    """A coin with no recorded window (from before the table) falls back to its live stamp, not NULL."""
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
    assert row["first_ever_at"] is None       # MIN over nothing
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
    assert summ["per_token"][0]["price_usd"] == 2.0   # the newest
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
    assert counts["outcomes"] == 0                 # table absent → 0, not an exception
    conn.close()


# --- storage ---
def test_storage_stats_reports_size_and_growth_rate(db_path):
    """Growth monitoring: without this, the archive's explosion stayed hidden until it reached 720 MB."""
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
    """A single snapshot (or zero) → no span, so no fabricated rate."""
    conn = _conn(db_path)
    st = dao.storage_stats(db_path, conn)
    assert st["span_days"] is None
    assert st["mb_per_day"] is None
    assert st["raw_encoding"] == "plain"           # no meta key → an explicit default
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
    cov = dao.bars_coverage(conn, 0)    # live=0: count every candle (testing the coverage mechanism, not the era)
    assert cov["active"] == 3
    assert cov["with_bars"] == 1
    assert cov["no_data"] == 1
    assert cov["pending"] == 1          # 'c' hasn't been reached by a cycle yet — not a failure
    assert cov["candles"] == 2
    assert cov["coverage_pct"] == round(100 / 3, 1)
    conn.close()


def test_bars_coverage_candles_excludes_pre_live_retro(db_path):
    """The candle count is limited to the live era: retro candles (ts < live)
    are price history before the signal, not the bot collecting live, so they
    aren't displayed lest they mix with the bot's data."""
    live = 1_785_018_927
    c = _bars_schema(db_path)
    c.execute("INSERT INTO watchlist(token_address, network_id, source, first_seen_at,"
              " watch_until, active) VALUES('a','56','feed','t','t',1)")
    # two retro candles (before the boundary) + one live candle (after it)
    c.execute("INSERT INTO token_bars VALUES('a','56','5',?,1,2,0.5,1.5,9,'t')", (live - 3600,))
    c.execute("INSERT INTO token_bars VALUES('a','56','5',?,1,2,0.5,1.5,9,'t')", (live - 60,))
    c.execute("INSERT INTO token_bars VALUES('a','56','5',?,1,2,0.5,1.5,9,'t')", (live + 60,))
    c.commit()
    c.close()

    conn = _conn(db_path)
    cov = dao.bars_coverage(conn, live)
    assert cov["candles"] == 1          # the live one only — the two retro ones are excluded
    assert cov["with_bars"] == 1        # the coin has a series (regardless of era)
    conn.close()


def test_bars_coverage_without_tables_is_zero_not_error(db_path):
    """An old database with no candle table → zeros, not an exception."""
    conn = _conn(db_path)
    cov = dao.bars_coverage(conn, 0)
    assert cov == {"active": 0, "with_bars": 0, "pending": 0, "no_data": 0,
                   "candles": 0, "coverage_pct": None}
    conn.close()


# --- signal performance: sorting and the tally ---
def _perf_fixture(db_path):
    """Three coins with known outcomes: a big winner, a small winner, and a loser."""
    c = _bars_schema(db_path)
    entry = 1785000000  # epoch of the entry stamp
    iso = datetime.fromtimestamp(entry, UTC).isoformat()
    specs = [
        # (token, entry price, peak, price now)
        ("big",  1.0, 5.0, 4.0),   # +300% now, peak +400%
        ("small", 1.0, 1.2, 1.1),  # +10% now, peak +20%
        ("loser", 1.0, 1.0, 0.5),  # -50% now, peak 0%
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
    assert big["from_peak_pct"] == pytest.approx(-20.0)   # 4.0 against a peak of 5.0
    assert rows["loser"]["change_pct"] == pytest.approx(-50.0)
    conn.close()


def test_sorting_ascending_surfaces_the_worst_not_the_reversed_top(db_path):
    """The deliberate regression: sorting runs on the full set before truncation.

    If it were sorted in the browser after truncating the best, ascending
    would give "the best inverted" and the loser would never appear.
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
    # an unknown key falls back to the default instead of raising or being injected
    rows = dao.watch_performance(conn, limit=3, sort_key="'; DROP TABLE watchlist--")
    assert [r["token_address"] for r in rows] == ["big", "small", "loser"]
    conn.close()


def test_sorting_keeps_missing_values_last_in_both_directions(db_path):
    _perf_fixture(db_path)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE token_static SET symbol=NULL")     # no symbols at all
    c.execute("INSERT INTO token_static(token_address, symbol) VALUES('big','ZZZ')")
    c.commit()
    c.close()
    conn = _conn(db_path)
    for desc in (True, False):
        syms = [r["symbol"] for r in dao.watch_performance(conn, 10, "symbol", desc)]
        assert syms[0] == "ZZZ"                          # the present one always first
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
    assert s["median_pct"] == pytest.approx(10.0)         # sturdier than the mean
    assert s["best"]["change_pct"] == pytest.approx(300.0)
    assert s["worst"]["change_pct"] == pytest.approx(-50.0)
    # network_id is needed to build the coin's page link on fomo (/tokens/:chain/:address)
    assert s["best"]["network_id"] == "56"
    assert s["worst"]["network_id"] == "56"
    assert s["is_open"] is True                          # a running window, not a realized result
    conn.close()


def test_performance_summary_covers_all_rows_not_just_the_displayed_slice(db_path):
    """The tally is computed over everything — otherwise the numbers would change with the display limit."""
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


# --- the schema-drift guard ---
def test_every_dao_read_runs_against_the_real_recorder_schema(tmp_path):
    """Every read function runs against the **real recorder schema**, not the test schema.

    Why it exists: the miniature schema here drifted from the real one
    (`token_static` had no `symbol` column), so a query that broke in
    production passed in the test. This test builds a database from
    `recorder/schema.sql` itself and runs every function — so any column
    deleted or renamed in the recorder drops the dashboard's tests
    immediately.
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
        # labeling and outcomes — on the real schema (it has the entry_ts column)
        labeling = dao.labeling_outcomes(c, live_start_ts=0)
        assert labeling["live"] is True
        assert labeling["signal"]["count"] == 0
        assert labeling["control"]["count"] == 0
        assert labeling["gate"]["completed"] == 0
        assert dao.last_labeled_at(c) is None
    finally:
        c.close()


# --- the control group ---
def _mixed_fixture(db_path):
    """Two winning signal coins and two losing control coins — a clear, known-in-advance difference."""
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
    """The table's title is "signal performance" — the control is a reference, not rows in it."""
    _mixed_fixture(db_path)
    conn = _conn(db_path)
    toks = {r["token_address"] for r in dao.watch_performance(conn, limit=10)}
    assert toks == {"sigA", "sigB"}
    conn.close()


def test_summary_excludes_control_coins(db_path):
    """Otherwise the losing control would lower the signal performance numbers and corrupt the meaning."""
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
    assert cmp["signal"]["avg_pct"] == pytest.approx(50.0)     # +80% and +20%
    assert cmp["control"]["avg_pct"] == pytest.approx(-15.0)   # -10% and -20%
    assert cmp["delta"]["avg_pct"] == pytest.approx(65.0)
    # a sample smaller than 20 per side → not read as a result
    assert cmp["sufficient"] is False
    conn.close()


def test_group_comparison_without_control_yields_no_delta(db_path):
    """Without a control there is no difference — we don't fabricate a zero suggesting a tie."""
    _perf_fixture(db_path)
    conn = _conn(db_path)
    cmp = dao.group_comparison(conn)
    assert cmp["control"]["count"] == 0
    assert all(v is None for v in cmp["delta"].values())
    assert cmp["sufficient"] is False
    conn.close()


def test_dao_tolerates_a_database_without_the_is_control_column(tmp_path):
    """The dashboard may run ahead of the recorder's migration — a missing column doesn't take it down."""
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


# --- the upstream feed's freshness ---
def test_status_flags_a_frozen_upstream_feed(db_path):
    """The recorder can run without an error while the source feed is frozen for hours (observed 3 hours).

    Without this distinction the archive looks like "a quiet market" when
    it's a source outage — a decisive difference for any later time-series
    analysis.
    """
    now = datetime.now(UTC)
    _seed_meta(
        db_path,
        last_cycle_at=(now - timedelta(seconds=20)).isoformat(),
        last_feed_event_at=(now - timedelta(hours=3)).isoformat(),
    )
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000, now=now)
    assert st["alive"] is True            # the recorder is alive
    assert st["feed_stale"] is True       # but the source is frozen
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
    """A missing key ≠ frozen — we don't alarm without evidence."""
    conn = _conn(db_path)
    st = dao.recorder_status(conn, 150, 2000)
    assert st["feed_age_seconds"] is None
    assert st["feed_stale"] is False
    conn.close()


def test_a_healthy_recorder_no_longer_heals_a_queue_that_has_not_succeeded(db_path):
    """The recorder's health doesn't heal a queue's error when the queue has
    its own stamp and hasn't succeeded yet.

    Measured 2026-08-19: the traders path was blocked for 21 hours while the
    recorder completed its cycles with `errors: 0` every minute. With
    `max(own, recorder)`, any error written there was declared "recovered" a
    minute after being written — meaning the same false health that hid the
    outage would have hidden its announcement too.
    """
    _seed_meta(
        db_path,
        last_ok_cycle_at="2026-08-20T13:00:00+00:00",       # the recorder is fine now
        traders_last_ok_at="2026-08-19T14:53:51+00:00",     # and this path isn't
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
    """And healing stays possible: an own stamp newer than the error outdates it, as before."""
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
