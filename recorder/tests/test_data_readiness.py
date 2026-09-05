import json
import sqlite3
from datetime import UTC, datetime, timedelta

import data_readiness
import features
import pytest

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _iso(delta_seconds: int = 0) -> str:
    return (NOW + timedelta(seconds=delta_seconds)).isoformat()


def _database(tmp_path):
    path = tmp_path / "readiness.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY,
            recorded_at TEXT NOT NULL,
            source TEXT NOT NULL,
            raw_json BLOB NOT NULL
        );
        CREATE TABLE signal_events (
            id TEXT PRIMARY KEY,
            token_address TEXT,
            network_id TEXT,
            recorded_at TEXT
        );
        CREATE TABLE market_ticks (
            token_address TEXT,
            network_id TEXT,
            recorded_at TEXT
        );
        CREATE TABLE token_bars (
            token_address TEXT,
            network_id TEXT,
            fetched_at TEXT,
            h_suspect INTEGER DEFAULT 0,
            l_suspect INTEGER DEFAULT 0,
            c_suspect INTEGER DEFAULT 0
        );
        CREATE TABLE token_flow (
            token_address TEXT,
            network_id TEXT,
            recorded_at TEXT
        );
        CREATE TABLE activity_events (
            id TEXT PRIMARY KEY,
            recorded_at TEXT
        );
        CREATE TABLE traders (
            trader_id TEXT PRIMARY KEY,
            recorded_at TEXT
        );
        CREATE TABLE bars_fetch_state (
            token_address TEXT,
            network_id TEXT,
            last_status TEXT
        );
        CREATE TABLE social_fetch_state (
            token_address TEXT,
            network_id TEXT,
            last_status TEXT
        );
        CREATE TABLE training_rows (
            kind TEXT,
            key TEXT,
            token_address TEXT,
            network_id TEXT,
            entry_ts INTEGER,
            split TEXT,
            feature_version INTEGER,
            final_return_48h REAL,
            max_gain_48h REAL,
            size_usd REAL,
            social_thesis_total INTEGER,
            ret_24h_before REAL,
            liquidity REAL,
            chain_holder_count INTEGER,
            onchain_top1_pct REAL,
            onchain_has_mint_authority INTEGER,
            onchain_code_size INTEGER,
            flow_net_volume_5m REAL,
            sol_ret_24h REAL,
            prior_signals_token INTEGER
        );
        CREATE VIEW model_training_rows AS SELECT * FROM training_rows;
        CREATE TABLE outcomes (status TEXT, kind TEXT);
        CREATE TABLE evm_replay_state (
            token_address TEXT,
            network_id TEXT,
            status TEXT,
            balance_check TEXT
        );
        CREATE TABLE evm_backfill_state (
            token_address TEXT,
            network_id TEXT,
            status TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO snapshots(recorded_at, source, raw_json) VALUES(?,?,?)",
        [(_iso(-60), "feed", b"{}"), (_iso(-60), "feed", b"{}")],
    )
    connection.execute(
        "INSERT INTO market_ticks VALUES(?,?,?)", ("token", "1", _iso(-7200))
    )
    connection.execute(
        "INSERT INTO bars_fetch_state VALUES(?,?,?)", ("bad", "1", "error")
    )
    connection.execute(
        "INSERT INTO social_fetch_state VALUES(?,?,?)", ("unsupported", "1", "unsupported")
    )
    rows = [
        (
            "signal", "a", "same", "1", 100, "train", 12, 0.1, 0.2, 1000.0, 4,
            0.1, 10_000.0, 30, 5.0, 0, 1200, 500.0, 0.02, 1,
        ),
        (
            "signal", "b", "same", "1", 100, "val", 11, None, 0.3, None, None,
            None, 20_000.0, None, None, None, None, None, None, 2,
        ),
    ]
    connection.executemany(
        "INSERT INTO training_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.execute("INSERT INTO outcomes VALUES('ok','signal')")
    connection.execute("INSERT INTO outcomes VALUES('no_bars','signal')")
    connection.execute(
        "INSERT INTO evm_replay_state VALUES(?,?,?,?)",
        ("token", "8453", "done", "negative"),
    )
    connection.execute(
        "INSERT INTO evm_backfill_state VALUES(?,?,?)",
        ("token", "8453", "partial"),
    )
    connection.commit()
    connection.close()
    return path


def test_connect_readonly_rejects_writes(tmp_path):
    path = _database(tmp_path)

    connection = data_readiness.connect_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO outcomes VALUES('ok','signal')")
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("rows", "age", "states", "expected"),
    [
        (1, 10, {}, "ok"),
        (1, 999, {}, "stale"),
        (0, None, {"error": 1}, "error"),
        (0, None, {"unsupported": 1}, "unsupported"),
        (0, None, {}, "empty"),
        (None, None, {}, "missing"),
    ],
)
def test_classify_source_states(rows, age, states, expected):
    assert data_readiness.classify_source(rows, age, states, stale_after=300) == expected


def test_classify_source_marks_partial_failures_as_degraded():
    assert data_readiness.classify_source(
        100, 10, {"ok": 95, "error": 5}, stale_after=300
    ) == "degraded"


def test_classify_source_marks_frozen_source_as_stale_even_with_errors():
    assert data_readiness.classify_source(
        100, 10_000, {"ok": 95, "error": 5}, stale_after=300
    ) == "stale"


def test_report_ignores_inactive_watch_errors_but_keeps_active_errors(tmp_path):
    path = _database(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE watchlist (token_address TEXT, network_id TEXT, active INTEGER)"
    )
    connection.executemany(
        "INSERT INTO watchlist VALUES(?,?,?)",
        [("bad", "1", 0), ("live", "1", 1)],
    )
    connection.execute(
        "INSERT INTO bars_fetch_state VALUES(?,?,?)", ("live", "1", "error")
    )
    connection.execute(
        "INSERT INTO token_bars VALUES(?,?,?,?,?,?)",
        ("live", "1", _iso(-60), 0, 0, 0),
    )
    connection.commit()
    connection.close()

    report = data_readiness.build_report(path, now=NOW)

    assert report["sources"]["bars"]["status"] == "degraded"
    assert report["sources"]["bars"]["states"] == {"error": 1}

    connection = sqlite3.connect(path)
    connection.execute("UPDATE watchlist SET active=0 WHERE token_address='live'")
    connection.commit()
    connection.close()
    report = data_readiness.build_report(path, now=NOW)
    assert report["sources"]["bars"]["status"] == "ok"
    assert report["sources"]["bars"]["states"] == {}


def test_build_report_measures_sources_features_and_integrity(tmp_path):
    path = _database(tmp_path)

    report = data_readiness.build_report(path, now=NOW)

    assert report["schema_version"] == "data-readiness-v3"
    assert report["database"]["mode"] == "ro"
    assert report["tables"]["snapshots"] == 2
    assert report["sources"]["feed"]["status"] == "ok"
    assert report["sources"]["market"]["status"] == "stale"
    assert report["sources"]["bars"]["status"] == "error"
    assert report["sources"]["social"]["status"] == "unsupported"
    assert report["sources"]["holders"]["status"] == "missing"
    assert report["sources"]["flow"]["status"] == "empty"

    model = report["model"]
    assert model["rows"] == 2
    assert model["tokens"] == 1
    assert model["feature_versions"] == {"11": 1, "12": 1}
    assert model["splits"] == {"train": 1, "val": 1}
    assert model["by_network"] == {"1": 2}
    assert model["by_day"] == {"1970-01-01": 2}
    assert model["families"]["event"]["rows_present"] == 1
    assert model["families"]["market"]["rows_present"] == 2
    assert model["families"]["flow"]["rows_present"] == 1
    assert model["families"]["trader"]["status"] == "missing"

    checks = {check["key"]: check for check in report["integrity_checks"]}
    assert checks["model_duplicate_decisions"]["status"] == "fail"
    assert checks["model_duplicate_decisions"]["value"] == 1
    assert checks["model_null_labels"]["status"] == "fail"
    assert checks["model_feature_versions"]["status"] == "fail"
    assert checks["model_view_contract"]["status"] == "fail"
    assert checks["evm_done_balance_checks"]["status"] == "fail"
    assert checks["evm_pending_backfills"]["status"] == "warn"


def test_report_flags_incomplete_phase_one_outcomes(tmp_path):
    path = _database(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE outcomes")
    connection.execute(
        """CREATE TABLE outcomes (
            kind TEXT, analysis_eligible INTEGER, status TEXT,
            final_return_48h REAL, max_gain_24h REAL, is_rug INTEGER
        )"""
    )
    connection.execute(
        "INSERT INTO outcomes VALUES('watch', 1, 'ok', -0.2, NULL, 0)"
    )
    connection.execute(
        "INSERT INTO outcomes VALUES('watch', 1, 'no_bars', NULL, NULL, NULL)"
    )
    connection.commit()
    connection.close()

    report = data_readiness.build_report(path, now=NOW)
    checks = {check["key"]: check for check in report["integrity_checks"]}

    assert checks["phase1_incomplete_ok_outcomes"] == {
        "key": "phase1_incomplete_ok_outcomes",
        "status": "fail",
        "value": 1,
        "detail": "Eligible phase-one ok outcomes with missing required metrics.",
    }
    assert checks["phase1_incomplete_outcomes"] == {
        "key": "phase1_incomplete_outcomes",
        "status": "pass",
        "value": 0,
        "detail": "Incomplete outcomes are quarantined and excluded from phase-one analysis.",
    }
    assert checks["phase1_no_bars_outcomes"]["status"] == "warn"
    assert checks["phase1_no_bars_outcomes"]["value"] == 1


def test_renderers_emit_stable_json_and_markdown(tmp_path):
    report = data_readiness.build_report(_database(tmp_path), now=NOW)

    payload = json.loads(data_readiness.render_json(report))
    markdown = data_readiness.render_markdown(report)

    assert payload["generated_at"] == NOW.isoformat()
    assert payload["sources"]["feed"]["rows"] == 2
    assert "# Data Readiness Report" in markdown
    assert "| feed | ok | 2 |" in markdown
    assert "model_duplicate_decisions" in markdown


def test_report_tolerates_source_table_without_expected_timestamp(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE market_ticks(token_address TEXT)")
    connection.execute("INSERT INTO market_ticks VALUES('token')")
    connection.commit()
    connection.close()

    report = data_readiness.build_report(path, now=NOW)

    assert report["sources"]["market"] == {
        "status": "stale",
        "rows": 1,
        "latest_at": None,
        "age_seconds": None,
        "states": {},
    }


def test_load_model_rows_uses_direct_eligibility_and_deduplicates(tmp_path):
    path = tmp_path / "eligible.db"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE training_rows (
            kind TEXT, key TEXT, token_address TEXT, network_id TEXT, entry_ts INTEGER,
            asset_class TEXT, status TEXT, is_live INTEGER, is_independent INTEGER,
            feature_version INTEGER, split TEXT, final_return_48h REAL, max_gain_48h REAL
        );
        CREATE TABLE signal_events (
            id TEXT PRIMARY KEY, token_address TEXT, network_id TEXT, ts TEXT,
            signal_type TEXT
        );
        CREATE VIEW model_training_rows AS
            SELECT * FROM training_rows WHERE is_live=1 AND is_independent=1
            AND asset_class='meme' AND feature_version>=2;
        """
    )
    rows = [
        ("signal", "a", "tok", "1", 100, "meme", "ok", 1, 1,
         features.FEATURE_VERSION, "train", 0.1, 0.2),
        ("signal", "b", "tok", "1", 100, "meme", "ok", 1, 1,
         features.FEATURE_VERSION, "train", 0.1, 0.2),
        ("signal", "c", "old", "1", 90, "meme", "ok", 0, 1,
         features.FEATURE_VERSION, "train", 0.1, 0.2),
    ]
    connection.executemany("INSERT INTO training_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    connection.executemany(
        "INSERT INTO signal_events VALUES(?,?,?,?,?)",
        [
            ("a", "tok", "1", "2026-01-01T00:00:00Z", "large_buy"),
            ("b", "tok", "1", "2026-01-01T00:00:00Z", "large_buy"),
            ("c", "old", "1", "2026-01-01T00:00:00Z", "large_buy"),
        ],
    )
    connection.commit()

    loaded = data_readiness._load_model_rows(connection, data_readiness._tables(connection))

    assert [row["key"] for row in loaded] == ["a"]
    connection.close()
