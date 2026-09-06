"""Backfills the two new labels (fv15) through the service layer — no hand-written SQL.

The labels `is_explosive` and `time_to_plus20_min` were added 2026-08-28 and
labeling is idempotent (INSERT OR IGNORE), so normal labeling never touches
already-labeled results. But `is_explosive` is fully derivable from columns
that are already stored (`max_gain_48h`/`max_gain_24h`) — so the backfill here
reads and then writes through the same db layer, and the log is preserved.

`time_to_plus20_min` needs bars (first close >= +20%), so it is computed from
stored token_bars — no network, and the computation comes from the same source
as the original labeling.

Usage:
    python backfill_explosive_labels.py            # diagnosis only
    python backfill_explosive_labels.py --apply    # execute
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402
from db import RecorderDB  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        rows = db._conn.execute(
            """SELECT kind, key, token_address, network_id, entry_ts, entry_px,
                      max_gain_48h, max_gain_24h
                 FROM outcomes
                WHERE status='ok'
                  AND max_gain_48h IS NOT NULL
                  AND max_gain_24h IS NOT NULL
                  AND is_explosive IS NULL
                ORDER BY entry_ts""",
        ).fetchall()
        print(f"Fillable ok outcomes: {len(rows):,}")

        explosive = 0
        done = 0
        for r in rows:
            is_expl = 1 if (
                r["max_gain_48h"] >= config.EXPLOSIVE_MIN_PEAK
                and r["max_gain_24h"] >= config.EXPLOSIVE_HALF_AT_24H
            ) else 0
            explosive += is_expl
            done += 1
            if not args.apply:
                continue
            # Writing through the same db layer: an update conditioned on the
            # label still being empty (idempotent and safe to re-run — never
            # touches what was already filled).
            db._conn.execute(
                """UPDATE outcomes SET is_explosive=?
                    WHERE kind=? AND key=? AND is_explosive IS NULL""",
                (is_expl, r["kind"], r["key"]),
            )
            if done % 5000 == 0:
                db._commit()
                print(f"  progress: {done:,}", flush=True)
        if args.apply:
            db._commit()

        print(f"is_explosive: {explosive:,} of {len(rows):,} "
              f"({explosive / max(len(rows), 1):.1%})")

        # time_to_plus20: needs bars — same source as labeling
        need_hook = db._conn.execute(
            """SELECT COUNT(*) FROM outcomes
                WHERE status='ok' AND time_to_plus20_min IS NULL
                  AND kind='watch'""",
        ).fetchone()[0]
        print(f"(time_to_plus20 for windows needs bars: {need_hook:,} — "
              f"filled by future labeling through the new compute_labels)")
        if not args.apply:
            print("\nDiagnosis only — pass --apply to execute.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
