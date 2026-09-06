"""Build the training table from the labeled outcomes (stage 2) — no network.

For every row in `outcomes`, `features.build_training_row` builds a full-feature row
pinned at t=0 and writes it to `training_rows`. Incremental: rows already built are
skipped unless `--rebuild` is given.

Filtering follows PLAN §2.1: `asset_class='meme'` is mandatory for training (the
archive holds BTC and tokenized stocks), but the script builds **all** classes and
writes `asset_class` into the row — separation happens at query time, not at
collection time, so we keep the ability to analyze the other classes.

Usage:
    py build_training_rows.py --dry-run          # report: how many rows, and which features are empty
    py build_training_rows.py                    # incremental build
    py build_training_rows.py --model-candidates-only  # only the independent live signals
    py build_training_rows.py --rebuild          # rebuild everything
    py build_training_rows.py --limit 200        # limited batch
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import features  # noqa: E402
from db import RecorderDB  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass


def _arg_int(name: str, default: int) -> int:
    if name in sys.argv:
        try:
            return int(sys.argv[sys.argv.index(name) + 1])
        except (IndexError, ValueError):
            pass
    return default


def pending_outcomes(
    db: RecorderDB,
    rebuild: bool,
    limit: int,
    model_candidates_only: bool = False,
) -> list[dict]:
    filters: list[str] = []
    params: list[object] = []
    if model_candidates_only:
        filters.append(
            "AND o.kind = 'signal' AND o.is_independent = 1 AND o.entry_ts >= ?"
        )
        params.append(config.LIVE_START_TS)
    rebuild_required = db.get_meta("evm_ledger_rebuild_required") == "1"
    rebuild_started = db.get_meta("evm_training_rebuild_started") == "1"
    if rebuild_required and not rebuild_started:
        evm_networks = sorted({
            *(str(network) for network in config.EVM_NETWORKS),
            *(str(network) for network in config.EVM_REPLAY_NETWORKS),
        })
        if evm_networks:
            filters.append(
                "AND COALESCE(o.network_id, '') NOT IN ("
                + ", ".join("?" for _ in evm_networks) + ")"
            )
            params.extend(evm_networks)
    filters.append(
        """AND NOT EXISTS (SELECT 1 FROM training_rows r
             WHERE r.kind = o.kind AND r.key = o.key AND r.feature_version = ?)"""
    )
    params.extend((features.FEATURE_VERSION, int(limit)))
    rows = db._conn.execute(
        f"""SELECT o.* FROM outcomes o
             WHERE o.status IN ('ok', 'no_bars')
             {' '.join(filters)}
             ORDER BY o.entry_ts LIMIT ?""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def build(
    db: RecorderDB,
    rebuild: bool,
    limit: int,
    dry: bool,
    model_candidates_only: bool = False,
) -> dict:
    if rebuild and model_candidates_only:
        raise ValueError("--rebuild and --model-candidates-only cannot be combined")
    if rebuild and not dry:
        # Rebuild is a one-time reset. Subsequent invocations can resume by
        # selecting rows missing the current feature version.
        db._conn.execute("DELETE FROM training_rows")
        db._conn.commit()
    expected_rebuild_state = db.evm_training_rebuild_state()
    stats = {"built": 0, "skipped_no_event": 0}
    rows: list[dict] = []
    for out in pending_outcomes(db, rebuild, limit, model_candidates_only):
        row = features.build_training_row(db, out)
        if row is None:
            stats["skipped_no_event"] += 1
            continue
        rows.append(row)
        stats["built"] += 1
    if rows and not dry:
        cols = features.ROW_COLUMNS
        with db.batch():
            db.assert_evm_training_rebuild_state(expected_rebuild_state)
            db._conn.executemany(
                f"INSERT OR REPLACE INTO training_rows({', '.join(cols)}) "
                f"VALUES({', '.join(f':{c}' for c in cols)})",
                [{c: r.get(c) for c in cols} for r in rows],
            )
    stats["rows"] = rows
    return stats


def coverage_report(rows: list[dict]) -> None:
    """Presence ratio per feature — a completely empty one exposes a broken or uncovered family."""
    if not rows:
        return
    n = len(rows)
    filled = {
        c: sum(1 for r in rows if r.get(c) is not None) for c in features.FEATURE_COLUMNS
    }
    print(f"\nFeature coverage over {n} rows (sorted ascending):")
    for c, k in sorted(filled.items(), key=lambda kv: kv[1]):
        pct = 100 * k / n
        mark = "  ← completely empty" if k == 0 else ("  ← weak coverage" if pct < 25 else "")
        print(f"  {c:<28} {pct:5.1f}%{mark}")


def main() -> None:
    dry = "--dry-run" in sys.argv
    rebuild = "--rebuild" in sys.argv
    model_candidates_only = "--model-candidates-only" in sys.argv
    limit = _arg_int("--limit", 10**9)
    batch_size = max(1, _arg_int("--batch-size", 500))
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        if dry:
            stats = build(db, rebuild, limit, True, model_candidates_only)
            rows = stats.pop("rows")
        else:
            stats = {"built": 0, "skipped_no_event": 0}
            rows: list[dict] = []
            remaining = limit
            first = True
            while remaining > 0:
                take = min(batch_size, remaining)
                part = build(
                    db, rebuild if first else False, take, False,
                    model_candidates_only,
                )
                first = False
                part_rows = part.pop("rows")
                stats["built"] += part["built"]
                stats["skipped_no_event"] += part["skipped_no_event"]
                rows.extend(part_rows)
                processed = part["built"] + part["skipped_no_event"]
                if processed:
                    print(
                        f"progress built={stats['built']} "
                        f"skipped={stats['skipped_no_event']}",
                        flush=True,
                    )
                if processed < take or processed == 0:
                    break
                remaining -= processed
        print(f"rows built: {stats['built']} · no source event: {stats['skipped_no_event']}")
        if rows:
            classes: dict[str, int] = {}
            for r in rows:
                cls = r.get("asset_class") or "unknown"
                classes[cls] = classes.get(cls, 0) + 1
            print("Breakdown by class:", dict(sorted(classes.items(), key=lambda kv: -kv[1])))
            coverage_report(rows)
        if dry:
            print("\n(dry-run — nothing written)")
        else:
            total = db._conn.execute("SELECT COUNT(*) FROM training_rows").fetchone()[0]
            print(f"\ntotal training_rows: {total}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
