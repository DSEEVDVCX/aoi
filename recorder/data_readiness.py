r"""Read-only readiness report for recorder data and model inputs.

The report answers three operational questions without migrating or mutating the
database: which collectors are current, which model feature families are present,
and which integrity gates currently fail. JSON is the stable machine contract;
Markdown is a compact generated view of the same values.

Usage:
    py data_readiness.py
    py data_readiness.py --format markdown
    py data_readiness.py --output ..\docs\data-readiness-2026-08-19.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import config
import features

SCHEMA_VERSION = "data-readiness-v3"

SOURCE_SPECS: dict[str, dict[str, Any]] = {
    "feed": {
        "table": "snapshots", "timestamp": "recorded_at",
        "where": "source='feed'", "stale_after": 300,
    },
    "trending": {
        "table": "snapshots", "timestamp": "recorded_at",
        "where": "source='trending'", "stale_after": 300,
    },
    "verified": {
        "table": "snapshots", "timestamp": "recorded_at",
        "where": "source='verified'", "stale_after": 300,
    },
    "most_held": {
        "table": "snapshots", "timestamp": "recorded_at",
        "where": "source='most_held'", "stale_after": 300,
    },
    "market": {
        "table": "market_ticks", "timestamp": "recorded_at", "stale_after": 300,
    },
    "bars": {
        "table": "token_bars", "timestamp": "fetched_at", "stale_after": 1800,
        "state_table": "bars_fetch_state", "active_watch_only": True,
    },
    "social": {
        "table": "token_social", "timestamp": "recorded_at", "stale_after": 3600,
        "state_table": "social_fetch_state", "active_watch_only": True,
    },
    "holders": {
        "table": "token_holders", "timestamp": "recorded_at", "stale_after": 1800,
        "state_table": "holders_fetch_state", "active_watch_only": True,
    },
    "flow": {
        "table": "token_flow", "timestamp": "recorded_at", "stale_after": 1800,
    },
    "traders": {
        "table": "traders", "timestamp": "recorded_at", "stale_after": 172800,
        "state_table": "traders_fetch_state",
    },
    "chain": {
        "table": "chain_concentration", "timestamp": "recorded_at",
        "stale_after": 900, "state_table": "chain_fetch_state",
        "active_watch_only": True,
    },
    "chain_authority": {
        "table": "chain_authority", "timestamp": "recorded_at",
        "stale_after": 7200, "state_table": "chain_auth_state",
    },
    "evm_contract": {
        "table": "evm_contract", "timestamp": "recorded_at",
        "stale_after": 7200, "state_table": "evm_contract_state",
    },
    "activity": {
        "table": "activity_events", "timestamp": "recorded_at", "stale_after": 3600,
    },
}

FEATURE_FAMILIES: dict[str, tuple[str, ...]] = {
    "event": ("size_usd",),
    "social": ("social_thesis_total",),
    "price_history": ("ret_24h_before",),
    "market": ("liquidity",),
    "holders": ("chain_holder_count",),
    "onchain_concentration": ("onchain_top1_pct",),
    "onchain_authority": ("onchain_has_mint_authority",),
    "evm_contract": ("onchain_code_size",),
    "flow": ("flow_net_volume_5m",),
    "macro": ("sol_ret_24h",),
    "density": ("prior_signals_token",),
    # Planned in P2/P3. Keeping it in the contract makes the gap explicit.
    "trader": ("buyer_followers", "buyer_prior_win_rate"),
}


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite database in read-only mode."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _epoch(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value / 1000 if value > 1e11 else value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def classify_source(
    rows: int | None,
    age_seconds: float | None,
    states: Mapping[str, int],
    *,
    stale_after: int | None,
) -> str:
    """Classify source health without treating absent and empty as equivalent."""
    if rows is None:
        return "missing"
    if rows == 0 and states.get("unsupported", 0):
        return "unsupported"
    if rows == 0:
        if states.get("error", 0) or states.get("retry", 0):
            return "error"
        return "empty"
    if stale_after is not None and (age_seconds is None or age_seconds > stale_after):
        return "stale"
    if states.get("error", 0) or states.get("retry", 0):
        return "degraded"
    return "ok"


def _state_counts(
    connection: sqlite3.Connection, table: str | None, existing: set[str],
    *, active_watch_only: bool = False,
) -> dict[str, int]:
    if not table or table not in existing or "last_status" not in _columns(connection, table):
        return {}
    active_filter = ""
    if active_watch_only and "watchlist" in existing:
        active_filter = (
            " WHERE EXISTS (SELECT 1 FROM watchlist w "
            "WHERE w.token_address=s.token_address "
            "AND w.network_id=s.network_id AND w.active=1)"
        )
    return {
        str(row[0] if row[0] is not None else "unknown"): int(row[1])
        for row in connection.execute(
            f"SELECT s.last_status, COUNT(*) FROM {table} s"
            f"{active_filter} GROUP BY s.last_status"
        )
    }


def _source_report(
    connection: sqlite3.Connection,
    existing: set[str],
    spec: Mapping[str, Any],
    now_epoch: float,
) -> dict[str, Any]:
    table = str(spec["table"])
    if table not in existing:
        states = _state_counts(
            connection, spec.get("state_table"), existing,
            active_watch_only=bool(spec.get("active_watch_only")),
        )
        status = "missing"
        if states.get("error", 0) or states.get("retry", 0):
            status = "error"
        elif states.get("unsupported", 0):
            status = "unsupported"
        return {
            "status": status, "rows": None, "latest_at": None,
            "age_seconds": None, "states": states,
        }
    columns = _columns(connection, table)
    timestamp = str(spec["timestamp"])
    where = f" WHERE {spec['where']}" if spec.get("where") else ""
    if timestamp in columns:
        rows, latest = connection.execute(
            f"SELECT COUNT(*), MAX({timestamp}) FROM {table}{where}"
        ).fetchone()
    else:
        rows = connection.execute(f"SELECT COUNT(*) FROM {table}{where}").fetchone()[0]
        latest = None
    latest_epoch = _epoch(latest)
    age = max(0.0, now_epoch - latest_epoch) if latest_epoch is not None else None
    states = _state_counts(
        connection, spec.get("state_table"), existing,
        active_watch_only=bool(spec.get("active_watch_only")),
    )
    return {
        "status": classify_source(
            int(rows), age, states, stale_after=spec.get("stale_after")
        ),
        "rows": int(rows),
        "latest_at": latest,
        "age_seconds": round(age, 3) if age is not None else None,
        "states": states,
    }


def _table_counts(
    connection: sqlite3.Connection, existing: set[str], model_rows: list[sqlite3.Row]
) -> dict[str, int]:
    names = sorted({str(spec["table"]) for spec in SOURCE_SPECS.values()} | {
        "snapshots", "signal_events", "watchlist", "watch_windows", "outcomes",
        "training_rows", "model_training_rows", "evm_replay_state",
        "evm_backfill_state", "evm_balances",
    })
    counts = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in names
        if table in existing and table != "model_training_rows"
    }
    if "model_training_rows" in existing:
        counts["model_training_rows"] = len(model_rows)
    return counts


def _load_model_rows(
    connection: sqlite3.Connection, existing: set[str]
) -> list[sqlite3.Row]:
    """Load model-eligible rows without repeatedly evaluating the complex view.

    The production view performs correlated duplicate checks. The equivalent
    direct query selects the same eligibility population once, then keeps the
    smallest key for duplicate event identities and duplicate decision moments.
    Older schemas fall back to the view because they cannot express the contract.
    """
    required = {
        "kind", "key", "token_address", "network_id", "entry_ts", "asset_class",
        "status", "is_live", "is_independent", "feature_version",
    }
    wanted = required | {
        column for candidates in FEATURE_FAMILIES.values() for column in candidates
    } | {"split", "final_return_48h", "max_gain_48h", "max_gain_24h", "is_rug"}

    def select_projection(table: str) -> str:
        available = _columns(connection, table)
        # Avoid pulling raw payloads and unrelated feature columns into memory.
        selected = [column for column in wanted if column in available]
        return ", ".join(sorted(selected))

    if "training_rows" not in existing or not required.issubset(
        _columns(connection, "training_rows")
    ):
        if "model_training_rows" not in existing:
            return []
        projection = select_projection("model_training_rows")
        return connection.execute(
            f"SELECT {projection} FROM model_training_rows"
        ).fetchall()

    projection = select_projection("training_rows")
    rows = connection.execute(
        f"""SELECT {projection} FROM training_rows
            WHERE kind='signal' AND is_live=1 AND asset_class='meme'
              AND status='ok' AND is_independent=1
              AND feature_version=?
            ORDER BY key""",
        (features.FEATURE_VERSION,),
    ).fetchall()
    duplicate_event_ids: set[str] = set()
    if "signal_events" in existing:
        signal_columns = _columns(connection, "signal_events")
        identity = {"id", "token_address", "network_id", "ts", "signal_type"}
        if identity.issubset(signal_columns):
            # The equivalent SQL self-join is quadratic on the live feed. Read
            # only the five identity columns once and keep the first id per
            # identity in Python; the query is small compared with raw payloads.
            seen_events: set[tuple[Any, ...]] = set()
            for event in connection.execute(
                """SELECT id, token_address, network_id, ts, signal_type
                     FROM signal_events
                    ORDER BY token_address, network_id, ts, signal_type, id"""
            ):
                event_identity = (
                    event["token_address"], event["network_id"] or "",
                    event["ts"], event["signal_type"],
                )
                if event_identity in seen_events:
                    duplicate_event_ids.add(str(event["id"]))
                else:
                    seen_events.add(event_identity)
    kept: list[sqlite3.Row] = []
    decisions: set[tuple[Any, Any, Any]] = set()
    for row in rows:
        if str(row["key"]) in duplicate_event_ids:
            continue
        decision = (row["token_address"], row["network_id"] or "", row["entry_ts"])
        if decision in decisions:
            continue
        decisions.add(decision)
        kept.append(row)
    return kept


def _model_report(
    connection: sqlite3.Connection, existing: set[str], model_rows: list[sqlite3.Row]
) -> dict[str, Any]:
    table = "model_training_rows"
    if table not in existing:
        return {
            "status": "missing", "rows": 0, "tokens": 0,
            "feature_versions": {}, "families": {},
        }
    columns = _columns(connection, table)
    rows = len(model_rows)
    token_expression = (
        "COUNT(DISTINCT token_address || ':' || COALESCE(network_id,''))"
        if {"token_address", "network_id"}.issubset(columns)
        else "0"
    )
    tokens = len({
        f"{row['token_address']}:{row['network_id'] or ''}"
        for row in model_rows
    }) if token_expression != "0" else 0
    versions: dict[str, int] = {}
    if "feature_version" in columns:
        versions = _row_group_counts(model_rows, "feature_version")
    splits = _row_group_counts(model_rows, "split")
    networks = _row_group_counts(model_rows, "network_id", empty="unknown")
    days: dict[str, int] = {}
    if "entry_ts" in columns:
        days = _row_day_counts(model_rows)
    families: dict[str, dict[str, Any]] = {}
    for name, candidates in FEATURE_FAMILIES.items():
        available = [column for column in candidates if column in columns]
        if not available:
            families[name] = {
                "status": "missing", "columns": [], "rows_present": 0,
                "coverage_pct": 0.0,
            }
            continue
        present = sum(
            any(row[column] is not None for column in available) for row in model_rows
        )
        families[name] = {
            "status": "ok" if present else "empty",
            "columns": available,
            "rows_present": present,
            "coverage_pct": round(100 * present / rows, 1) if rows else 0.0,
        }
    return {
        "status": "ok" if rows else "empty",
        "rows": rows,
        "tokens": tokens,
        "feature_versions": versions,
        "splits": splits,
        "by_network": networks,
        "by_day": days,
        "families": families,
    }


def _row_group_counts(
    rows: list[sqlite3.Row], column: str, *, empty: str = "unknown",
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row[column]
        key = str(value if value not in (None, "") else empty)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _row_day_counts(rows: list[sqlite3.Row]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row["entry_ts"]
        day = datetime.fromtimestamp(int(value), UTC).date().isoformat()
        counts[day] = counts.get(day, 0) + 1
    return dict(sorted(counts.items()))


def _group_counts(
    connection: sqlite3.Connection, table: str, column: str, columns: set[str],
    *,
    empty: str = "unknown",
) -> dict[str, int]:
    if column not in columns:
        return {}
    return {
        str(row[0] if row[0] not in (None, "") else empty): int(row[1])
        for row in connection.execute(
            f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column} ORDER BY {column}"
        )
    }


def _check(key: str, status: str, value: int, detail: str) -> dict[str, Any]:
    return {"key": key, "status": status, "value": value, "detail": detail}


def _integrity_checks(
    connection: sqlite3.Connection, existing: set[str], model_rows: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    model = "model_training_rows"
    model_columns = _columns(connection, model) if model in existing else set()
    decision_columns = {"token_address", "network_id", "entry_ts"}
    if decision_columns.issubset(model_columns):
        counts: dict[tuple[Any, Any, Any], int] = {}
        for row in model_rows:
            key = (row["token_address"], row["network_id"] or "", row["entry_ts"])
            counts[key] = counts.get(key, 0) + 1
        duplicates = sum(count - 1 for count in counts.values() if count > 1)
        checks.append(_check(
            "model_duplicate_decisions", "fail" if duplicates else "pass", duplicates,
            "Extra rows sharing token, network, and decision timestamp.",
        ))
    else:
        checks.append(_check(
            "model_duplicate_decisions", "skip", 0, "Decision columns are unavailable.",
        ))

    labels = [
        column for column in ("final_return_48h", "max_gain_48h")
        if column in model_columns
    ]
    if labels:
        nulls = sum(any(row[column] is None for column in labels) for row in model_rows)
        checks.append(_check(
            "model_null_labels", "fail" if nulls else "pass", nulls,
            "Eligible model rows with missing mature outcome labels.",
        ))
    else:
        checks.append(_check("model_null_labels", "skip", 0, "Label columns unavailable."))

    if "feature_version" in model_columns:
        versions = len({row["feature_version"] for row in model_rows})
        checks.append(_check(
            "model_feature_versions", "fail" if versions != 1 else "pass", versions,
            "The model view must expose exactly one current feature version.",
        ))
    else:
        checks.append(_check(
            "model_feature_versions", "skip", 0, "Feature version column unavailable.",
        ))

    view_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (model,)
    ).fetchone()
    if view_row is None:
        checks.append(_check("model_view_contract", "skip", 0, "Model view unavailable."))
    else:
        sql = str(view_row[0] or "").lower()
        required = ("is_live", "is_independent", "asset_class", "feature_version")
        missing = sum(1 for token in required if token not in sql)
        checks.append(_check(
            "model_view_contract", "fail" if missing else "pass", missing,
            "Required eligibility guards absent from the model view definition.",
        ))

    if "outcomes" in existing:
        outcome_columns = _columns(connection, "outcomes")
        required = {"kind", "analysis_eligible", "status", "final_return_48h",
                    "max_gain_24h", "is_rug"}
        if required.issubset(outcome_columns):
            incomplete = int(connection.execute(
                """SELECT COUNT(*) FROM outcomes
                    WHERE kind='watch' AND analysis_eligible=1 AND status='ok'
                      AND (final_return_48h IS NULL OR max_gain_24h IS NULL OR is_rug IS NULL)"""
            ).fetchone()[0])
            checks.append(_check(
                "phase1_incomplete_ok_outcomes", "fail" if incomplete else "pass",
                incomplete,
                "Eligible phase-one ok outcomes with missing required metrics.",
            ))
            quarantined = int(connection.execute(
                """SELECT COUNT(*) FROM outcomes
                    WHERE kind='watch' AND status='incomplete'"""
            ).fetchone()[0])
            checks.append(_check(
                "phase1_incomplete_outcomes", "warn" if quarantined else "pass",
                quarantined,
                "Incomplete outcomes are quarantined and excluded from phase-one analysis.",
            ))
            no_bars = int(connection.execute(
                """SELECT COUNT(*) FROM outcomes
                    WHERE kind='watch' AND analysis_eligible=1 AND status='no_bars'"""
            ).fetchone()[0])
            checks.append(_check(
                "phase1_no_bars_outcomes", "warn" if no_bars else "pass", no_bars,
                "Phase-one no_bars outcomes are retained in missingness analysis.",
            ))

    if "evm_replay_state" in existing:
        columns = _columns(connection, "evm_replay_state")
        if {"status", "balance_check"}.issubset(columns):
            invalid = int(connection.execute(
                """SELECT COUNT(*) FROM evm_replay_state
                    WHERE status='done' AND COALESCE(balance_check,'') <> 'ok'"""
            ).fetchone()[0])
            checks.append(_check(
                "evm_done_balance_checks", "fail" if invalid else "pass", invalid,
                "Completed replay rows whose balance check is not ok.",
            ))
    if "evm_backfill_state" in existing and "status" in _columns(
        connection, "evm_backfill_state"
    ):
        pending = int(connection.execute(
            """SELECT COUNT(*) FROM evm_backfill_state
                WHERE COALESCE(status,'') NOT IN ('done','empty','unsupported')"""
        ).fetchone()[0])
        checks.append(_check(
            "evm_pending_backfills", "warn" if pending else "pass", pending,
            "EVM token ledgers not yet in a terminal live-backfill state.",
        ))
    return checks


def build_report(
    database_path: str | Path = config.DB_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the stable read-only readiness report."""
    requested_moment = now
    if requested_moment is not None and requested_moment.tzinfo is None:
        requested_moment = requested_moment.replace(tzinfo=UTC)
    path = Path(database_path).resolve()
    connection = connect_readonly(path)
    try:
        existing = _tables(connection)
        model_rows = _load_model_rows(connection, existing)
        # A live recorder may advance while this report is loading. Timestamp
        # after the expensive reads so fresh rows do not appear to come from
        # the future merely because report generation took time.
        moment = requested_moment or datetime.now(UTC)
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": moment.isoformat(),
            "database": {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "mode": "ro",
            },
            "tables": _table_counts(connection, existing, model_rows),
            "sources": {
                name: _source_report(connection, existing, spec, moment.timestamp())
                for name, spec in SOURCE_SPECS.items()
            },
            "model": _model_report(connection, existing, model_rows),
            "integrity_checks": _integrity_checks(connection, existing, model_rows),
        }
    finally:
        connection.close()


def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_markdown(report: Mapping[str, Any]) -> str:
    database = report["database"]
    lines = [
        "# Data Readiness Report",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Database: `{database['path']}` ({database['size_bytes']} bytes, `{database['mode']}`)",
        "",
        "## Sources",
        "",
        "| source | status | rows | latest | age seconds | states |",
        "|---|---:|---:|---|---:|---|",
    ]
    for name, source in report["sources"].items():
        states = ", ".join(f"{k}:{v}" for k, v in sorted(source["states"].items()))
        lines.append(
            f"| {name} | {source['status']} | {source['rows']} | "
            f"{source['latest_at']} | {source['age_seconds']} | {states} |"
        )
    model = report["model"]
    lines.extend([
        "",
        "## Model",
        "",
        f"Rows: **{model['rows']}**. Tokens: **{model['tokens']}**. "
        f"Feature versions: `{json.dumps(model['feature_versions'], sort_keys=True)}`.",
        "",
        "| family | status | rows present | coverage | columns |",
        "|---|---:|---:|---:|---|",
    ])
    for name, family in model["families"].items():
        lines.append(
            f"| {name} | {family['status']} | {family['rows_present']} | "
            f"{family['coverage_pct']}% | {', '.join(family['columns'])} |"
        )
    lines.extend([
        "",
        "## Integrity Checks",
        "",
        "| check | status | value | detail |",
        "|---|---:|---:|---|",
    ])
    for check in report["integrity_checks"]:
        lines.append(
            f"| {check['key']} | {check['status']} | {check['value']} | "
            f"{check['detail']} |"
        )
    return "\n".join(lines) + "\n"


def _write_output(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = build_report(args.db)
    content = render_markdown(report) if args.format == "markdown" else render_json(report)
    if args.output:
        _write_output(Path(args.output), content)
    else:
        print(content, end="")


if __name__ == "__main__":
    main()
