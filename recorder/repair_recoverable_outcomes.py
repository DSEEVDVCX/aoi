"""Recompute recoverable watch outcomes from locally stored bars.

The labeler intentionally writes an outcome once.  This utility handles the
safe exception where a later bar backfill makes an old ``no_bars`` or
``incomplete`` watch outcome computable.  It is read-only by default and never
fabricates missing candles or changes signal/activity outcomes.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import labeler  # noqa: E402
from db import RecorderDB  # noqa: E402

_LABEL_COLUMNS = (
    "entry_px", "entry_lag_s", "max_gain_1h", "max_gain_4h", "max_gain_24h",
    "max_gain_48h", "max_drawdown_48h", "final_return_48h", "time_to_peak_h",
    "candles_48h", "suspect_bars", "last_bar_lag_h", "bars_truncated", "is_rug",
    "status",
)


def candidates(db: RecorderDB) -> list[dict]:
    rows = db._conn.execute(
        """SELECT kind, key, token_address, network_id, entry_ts,
                  design_version, analysis_eligible, status,
                  (SELECT admission_price_usd FROM watch_windows w
                    WHERE w.token_address=o.token_address
                      AND w.network_id=o.network_id
                      AND CAST(strftime('%s', w.first_seen_at) AS INTEGER)=o.entry_ts
                    ORDER BY w.first_seen_at LIMIT 1) AS admission_price_usd
             FROM outcomes
             AS o
            WHERE kind='watch' AND status IN ('no_bars','incomplete')
            ORDER BY entry_ts, key"""
    ).fetchall()
    return [dict(row) for row in rows]


def _labels(db: RecorderDB, row: Mapping[str, object]) -> dict:
    entry = int(row["entry_ts"] or 0)
    token = str(row["token_address"] or "")
    network = str(row["network_id"] or "")
    bars = db.bars_for(token, network, entry, entry + config.LABEL_WINDOW_HOURS * 3600)
    return labeler.compute_labels(
        bars, entry, admission_price_usd=row.get("admission_price_usd")
    )


def plan(db: RecorderDB) -> list[dict]:
    result = []
    for row in candidates(db):
        labels = _labels(db, row)
        # A local bar backfill may turn no_bars/incomplete into ok. It must not
        # turn an old label into no_entry because that would be less informed.
        if labels["status"] in (row["status"], "no_entry"):
            continue
        result.append({
            "kind": row["kind"],
            "key": row["key"],
            "before": row["status"],
            "after": labels["status"],
            "labels": labels,
            "analysis_eligible": int(row["design_version"] or 0) >= 3
            and labels["status"] != "incomplete",
        })
    return result


def repair(db: RecorderDB, *, apply: bool) -> list[dict]:
    changes = plan(db)
    if not apply or not changes:
        return changes
    assignments = ", ".join(f"{column}=?" for column in _LABEL_COLUMNS)
    sql = f"UPDATE outcomes SET {assignments}, analysis_eligible=?, exclusion_reason=? WHERE kind=? AND key=?"
    with db.batch():
        for change in changes:
            labels = change["labels"]
            eligible = 1 if change["analysis_eligible"] else 0
            reason = None if eligible else "incomplete_metrics"
            values = [labels[column] for column in _LABEL_COLUMNS]
            db._conn.execute(
                "DELETE FROM training_rows WHERE kind=? AND key=?",
                (change["kind"], change["key"]),
            )
            db._conn.execute(sql, (*values, eligible, reason, change["kind"], change["key"]))
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        changes = repair(db, apply=args.apply)
        for change in changes:
            print(f"{change['kind']} {change['key']}: {change['before']} -> {change['after']}")
        if args.apply:
            print(f"repaired: {len(changes)}")
        else:
            print(f"would repair: {len(changes)} (dry-run: no database changes)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
