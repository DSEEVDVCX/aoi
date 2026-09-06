"""Quarantine outcomes marked ``ok`` while required labels are missing."""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from db import RecorderDB  # noqa: E402


def find(db: RecorderDB) -> list[dict]:
    rows = db._conn.execute(
        """SELECT kind, key, status, analysis_eligible, exclusion_reason
             FROM outcomes
            WHERE status='ok'
              AND (final_return_48h IS NULL OR max_gain_24h IS NULL OR is_rug IS NULL)
            ORDER BY kind, key"""
    ).fetchall()
    return [dict(row) for row in rows]


def repair(db: RecorderDB, *, apply: bool) -> int:
    rows = find(db)
    stale_rows = db._conn.execute(
        """SELECT o.kind, o.key FROM outcomes o
             JOIN training_rows r ON r.kind=o.kind AND r.key=o.key
            WHERE o.status='incomplete'"""
    ).fetchall()
    if not apply:
        return len(rows)
    if not rows and not stale_rows:
        return 0
    with db.batch():
        db._conn.executemany(
            "DELETE FROM training_rows WHERE kind=? AND key=?",
            [(row["kind"], row["key"]) for row in (*rows, *stale_rows)],
        )
        if rows:
            db._conn.executemany(
                """UPDATE outcomes
                      SET status='incomplete', analysis_eligible=0,
                          exclusion_reason='incomplete_metrics'
                    WHERE kind=? AND key=?""",
                [(row["kind"], row["key"]) for row in rows],
            )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        rows = find(db)
        print(f"incomplete outcomes found: {len(rows)}")
        if args.apply:
            print(f"repaired: {repair(db, apply=True)}")
        else:
            print("dry-run: no database changes")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
