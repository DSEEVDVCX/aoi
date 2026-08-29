"""Backfill token_static for coins watched before the filterTokens path wrote it.

The creation date used to arrive only from the public lists (trending / verified /
most_held), and a public list selects on *popularity, not age*: measured on the
live database, 406 watched coins appeared on one of those lists and every single
one has a static row, while 215 never appeared and none of them has a row at all
-- a clean 100% split. Those 215 coins therefore have no creation date, which is
not a property of the coins but of how we fetched.

``run_filter_tokens_cycle`` now closes this for new watches at zero network cost,
but only forward. This script closes the historical hole the same way: filterTokens
answers about *our own* addresses regardless of popularity, and its item shape is
identical to a trending item's (same top-level keys, same keys inside ``token``),
so ``extract.extract_token_static`` parses it unchanged.

Measured recoverability before writing anything: 212 of 215 (98.6%) still come
back with an age, 0 were dropped by the provider, 3 return without one. Dead
addresses are dropped silently by the upstream, so a shrinking answer is expected
here and does not fail the batch.

Dry-run by default; ``--apply`` writes. Writes go through ``upsert_static``
(INSERT OR IGNORE), so an existing row can never be overwritten.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import RecorderDB, utcnow_iso  # noqa: E402

import recorder  # noqa: E402

BATCH = 150

MISSING = """
SELECT w.token_address AS tok, w.network_id AS net
  FROM watch_windows w
  LEFT JOIN token_static t
    ON t.token_address=w.token_address AND t.network_id=w.network_id
 WHERE t.token_address IS NULL
 GROUP BY 1, 2
 ORDER BY 1, 2
"""


def find(db: RecorderDB) -> list[tuple[str, str]]:
    return [(r["tok"], r["net"]) for r in db._conn.execute(MISSING)]


async def repair(db: RecorderDB, *, apply: bool) -> dict[str, int]:
    targets = find(db)
    stats = {"targets": len(targets), "written": 0, "no_age": 0, "dropped": 0}
    if not targets or not apply:
        return stats

    client = recorder._load_client()
    recorded_at = utcnow_iso()
    for start in range(0, len(targets), BATCH):
        chunk = targets[start : start + BATCH]
        symbols = [f"{addr}:{net}" for addr, net in chunk]
        raw = await recorder._fetch_filter_tokens_raw(client, symbols)
        # Bind by address, never by position: the upstream drops dead addresses
        # silently, so the index slides and the order lies.
        by_addr = {}
        for item in extract.unwrap_token_list(raw):
            a = extract.token_list_address(item)
            if a:
                by_addr[a.lower()] = item
        matched = 0
        with db.batch():
            for addr, _net in chunk:
                item = by_addr.get(addr.lower())
                if item is None:
                    stats["dropped"] += 1
                    continue
                row = extract.extract_token_static(item, recorded_at)
                if row is None:
                    stats["dropped"] += 1
                    continue
                if not row.get("token_created_at"):
                    stats["no_age"] += 1
                db.upsert_static(row)
                stats["written"] += 1
                matched += 1
        print(f"  batch {start // BATCH + 1}: asked {len(symbols)}, "
              f"matched {matched}")
        if start + BATCH < len(targets):
            await asyncio.sleep(config.FILTER_TOKENS_PACING_SECONDS)
    return stats


async def run(apply: bool) -> int:
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        targets = find(db)
        print(f"watched coins with no token_static row: {len(targets)}")
        if not apply:
            print("dry-run: no database changes (pass --apply to write)")
            return 0
        stats = await repair(db, apply=True)
        print(f"\nstatic rows written : {stats['written']}")
        print(f"  of those, no age  : {stats['no_age']}")
        print(f"dropped by provider : {stats['dropped']}")
        left = len(find(db))
        print(f"\nstill without a static row: {left}")
    finally:
        db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
