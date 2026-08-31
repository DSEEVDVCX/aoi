"""Re-labels outcomes poisoned by impossible tails (no network).

Labeling is idempotent by design: a row is labeled once and never revisited.
But rows computed before the 2026-07-30 fix may carry a `max_gain` built on an
impossible tail from upstream (+3.78 billion percent was observed). This
script deletes those rows **only** so the labeler recomputes them from the
same bars with the new flags — no fabrication, no manual value edits.

Safe: the label is deterministically derived from `token_bars` (the source
remains), so deletion is fully recoverable with one labeler cycle. It never
touches a healthy row.

Usage:
    py relabel_suspect.py --dry-run     # what would be deleted and recomputed
    py relabel_suspect.py               # execute
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from db import RecorderDB  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

# A suspect row: its window contains a flagged bar, or its metrics are
# economically impossible (x100 = +10,000% within 48 hours on a meme coin is
# not theoretically impossible, but with a flagged tail inside the window the
# likelier explanation is upstream corruption, not a price).
_SUSPECT_SQL = """
SELECT o.kind, o.key, o.token_address, o.entry_ts, o.max_gain_48h,
       (SELECT COUNT(*) FROM token_bars b
         WHERE b.token_address = o.token_address
           AND b.network_id = o.network_id
           AND b.resolution = '5'
           AND b.ts > o.entry_ts
           AND b.ts <= o.entry_ts + ?
           AND (b.h_suspect = 1 OR b.l_suspect = 1 OR b.c_suspect = 1)) AS flagged
  FROM outcomes o
 WHERE o.status = 'ok'
"""


def find(db: RecorderDB) -> list[dict]:
    window = config.LABEL_WINDOW_HOURS * 3600
    rows = db._conn.execute(_SUSPECT_SQL, (window,)).fetchall()
    return [dict(r) for r in rows if r["flagged"]]


def main() -> None:
    dry = "--dry-run" in sys.argv
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        bad = find(db)
        print(f"Outcome rows whose window contains a flagged tail: {len(bad)}")
        for r in bad[:20]:
            print(f"  {r['kind']:<9} {r['token_address'][:14]}… "
                  f"max_gain_48h={r['max_gain_48h']} flagged_bars={r['flagged']}")
        if dry:
            print("(dry-run — no deletion)")
            return
        if not bad:
            print("Nothing to relabel.")
            return
        # A textual copy of the rows before deleting them: the label is
        # deterministically derived from the bars so deletion is recoverable,
        # but keeping the old values allows a before/after comparison.
        snap = os.path.join(HERE, "relabel_suspect_before.json")
        pairs = ", ".join(["(?, ?)"] * len(bad))
        full = [
            dict(r) for r in db._conn.execute(
                f"SELECT * FROM outcomes WHERE (kind, key) IN ({pairs})",
                [v for r in bad for v in (r["kind"], r["key"])],
            ).fetchall()
        ]
        with open(snap, "w", encoding="utf-8") as fh:
            json.dump(full, fh, ensure_ascii=False, indent=1)
        print(f"Snapshot of the old values: {snap}")
        with db.batch():
            db._conn.executemany(
                "DELETE FROM outcomes WHERE kind=? AND key=?",
                [(r["kind"], r["key"]) for r in bad],
            )
        print(f"Deleted {len(bad)} outcomes — the labeler will recompute them "
              f"in its next cycle (every 15 minutes) or run: py run_labeler.py 1")
    finally:
        db.close()


if __name__ == "__main__":
    main()
