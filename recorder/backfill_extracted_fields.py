"""Backfill newly extracted columns from stored raw data (no network).

The following fields were **present in `raw_json` from day one** but the
extractor never read them, so they were lost to analysis only, not to the
archive:

- `signal_events`: likes · views · num_replies · pinned
  (measured coverage: likes/views in 100% of events; numReplies is rare ⇒
  stays NULL)
- `token_static`: exchanges_count/json · cmc_id · description(+len) ·
  has_banner · has_image
- `token_social`: holder_authors — a **fix**, not an addition: it used to read
  `equity`, which is zero in 100% of theses; the real position is in
  `authorTrade`

Because the raw data is stored and compressed (zlib BLOB), we re-derive
locally: **zero calls** to fomo, no rate-limit risk, and the result matches
exactly what the live recorder will record, because the script calls the very
same `extract` functions — one reference, so two definitions cannot drift.

Values absent from the source stay NULL and are never fabricated as zero
(FR-007), and the raw data is never touched.

Usage:
    py backfill_extracted_fields.py --dry-run    # report without writing
    py backfill_extracted_fields.py              # do the backfill
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import RecorderDB, decode_raw  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

BATCH = 5000

# Column → output key from `extract`. The names happen to match here, but being
# explicit prevents filling a column with a similarly named key of different
# meaning after any future rename.
SIGNAL_FIELDS = ("likes", "views", "num_replies", "pinned", "out_token_address")
STATIC_FIELDS = (
    "exchanges_count", "exchanges_json", "cmc_id",
    "description", "description_len", "has_banner", "has_image",
    # Two old columns, not new ones: they were NULL in 100% of rows because the
    # previous converter expected a boolean while the source gives an authority
    # **address**. The raw data carries the key in 40/40 snapshots, so the fix
    # is retroactive with no network.
    # Operational note: EVM rows legitimately stay NULL (not measured — FR-007),
    # so they match the `IS NULL` condition on every later run. Re-running is
    # harmless (same raw ⇒ same value), but it means this script never reaches
    # "zero missing rows".
    "mintable", "freezable",
)
# Not a "new" field but a **corrected** one: it was derived from `equity`, zero
# in 28,186/28,186 measured theses, so the column was pinned at 0 across 46,040
# rows. The correct source is `authorTrade.humanTokenAmount`, which the raw data
# carries in every snapshot ⇒ a retroactive fix with no network. We touch no
# other snapshot columns (the other aggregates were already correct).
SOCIAL_FIELDS = ("holder_authors",)


def _same(a, b) -> bool:
    """Does the derived value match the stored one? (blocks no-op writes)."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= 1e-9
        except (TypeError, ValueError):
            return False
    return a == b


def _backfill(
    db: RecorderDB,
    table: str,
    key_cols: tuple[str, ...],
    fields: tuple[str, ...],
    extract_row,
    dry: bool,
    where_sql: str | None = None,
) -> tuple[int, int, int]:
    """Returns (scanned, updated, failed). Walks candidate rows only.

    The default condition `any new column IS NULL` makes the script re-runnable:
    filled rows are skipped, and an interruption midway corrupts nothing.
    `where_sql` overrides it when the gap is not NULL — a column pinned to a
    wrong **zero** looks filled but is not, and `IS NULL` cannot catch it.

    After deriving we compare against the stored value and skip matches: rows
    the source says nothing about (EVM in mintable, say) match the condition on
    every run, so without the comparison the script would rewrite tens of
    thousands of rows with their own values forever.
    """
    missing = where_sql or " OR ".join(f"{f} IS NULL" for f in fields)
    keys = ", ".join(key_cols)
    cols = ", ".join(fields)
    total = db._conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {missing}"
    ).fetchone()[0]
    print(f"{table}: {total} candidate rows")

    # The read is drained **before** any write: the live recorder writes every
    # minute, and leaving a read cursor open during writes needlessly lengthens
    # the lock window.
    scanned = failed = 0
    pending: list[tuple] = []
    cur = db._conn.execute(
        f"SELECT {keys}, {cols}, raw_json FROM {table} WHERE {missing}"
    )
    while True:
        chunk = cur.fetchmany(BATCH)
        if not chunk:
            break
        for r in chunk:
            scanned += 1
            try:
                row = extract_row(decode_raw(r["raw_json"]))
                if row is None:
                    failed += 1
                    continue
            except Exception as exc:  # noqa: BLE001 — one corrupt row must not stop the rest
                failed += 1
                if failed <= 3:
                    print(f"  failed: {type(exc).__name__}: {exc}")
                continue
            vals = tuple(row.get(f) for f in fields)
            if all(v is None for v in vals):
                continue  # the source is genuinely silent — no write, no fabrication
            if all(_same(v, r[f]) for v, f in zip(vals, fields, strict=True)):
                continue  # matches stored — a write with no effect
            pending.append(vals + tuple(r[c] for c in key_cols))

    if dry or not pending:
        return scanned, len(pending), failed

    sets = ", ".join(f"{f}=?" for f in fields)
    where = " AND ".join(f"{c}=?" for c in key_cols)
    sql = f"UPDATE {table} SET {sets} WHERE {where}"
    for start in range(0, len(pending), BATCH):
        _write_with_retry(db, sql, pending[start:start + BATCH])
    return scanned, len(pending), failed


def _write_with_retry(db: RecorderDB, sql: str, rows: list[tuple], tries: int = 6) -> None:
    """Write a batch with increasing backoff on lock.

    The recorder and the labeler are writing right now; WAL serializes writes,
    but a large batch can exceed SQLite's timeout. Failing here loses the
    batch, and the script is re-runnable, but waiting is cheaper than
    re-deriving everything.
    """
    for attempt in range(tries):
        try:
            with db.batch():
                db._conn.executemany(sql, rows)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == tries - 1:
                raise
            wait = 2 ** attempt
            print(f"  database locked — retrying after {wait}s")
            time.sleep(wait)


def main() -> None:
    dry = "--dry-run" in sys.argv
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        s1 = _backfill(
            db, "signal_events", ("id",), SIGNAL_FIELDS,
            lambda raw: extract.extract_signal_event(raw, "1970-01-01T00:00:00+00:00"),
            dry,
        )
        print(f"  scanned {s1[0]} · updated {s1[1]} · failed {s1[2]}")

        s2 = _backfill(
            db, "token_static", ("token_address", "network_id"), STATIC_FIELDS,
            lambda raw: extract.extract_token_static(raw, "1970-01-01T00:00:00+00:00"),
            dry,
        )
        print(f"  scanned {s2[0]} · updated {s2[1]} · failed {s2[2]}")

        # `holder_authors` used to read `equity`, zero in 100% of theses, so
        # the column stayed pinned at 0 — a gap `IS NULL` **cannot see**, so we
        # walk everything and rely on the value comparison to skip what does
        # not change.
        s3 = _backfill(
            db, "token_social", ("token_address", "network_id", "recorded_at"),
            SOCIAL_FIELDS,
            lambda raw: extract.extract_social(raw, "", "", ""),
            dry, where_sql="1",
        )
        print(f"  scanned {s3[0]} · updated {s3[1]} · failed {s3[2]}")

        if dry:
            print("(dry-run — no writes)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
