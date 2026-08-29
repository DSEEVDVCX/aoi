"""Read-only/dry-run repair helpers for recorder-derived data.

The live collectors preserve raw payloads. This module rebuilds only derived
columns/tables from those payloads, never deletes rows, and never overwrites a
non-null value. Use ``--dry-run`` first and ``--apply`` only after reviewing its
counts.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import RecorderDB, decode_raw  # noqa: E402


def repair_static_protocols(db: RecorderDB, *, apply: bool) -> dict[str, int]:
    """Recover ``token_static.dex_protocol`` from archived raw payloads."""
    rows = db._conn.execute(
        """SELECT token_address, network_id, raw_json
             FROM token_static
            WHERE dex_protocol IS NULL"""
    ).fetchall()
    stats = {"candidates": len(rows), "protocols_found": 0, "updated": 0, "decode_errors": 0}
    for row in rows:
        payloads = [row["raw_json"]]
        payloads.extend(
            market["raw_json"]
            for market in db._conn.execute(
                """SELECT raw_json FROM market_ticks
                     WHERE token_address=? AND network_id=?
                     ORDER BY recorded_at DESC""",
                (row["token_address"], row["network_id"]),
            )
        )
        protocol = None
        for payload in payloads:
            try:
                raw = decode_raw(payload)
            except (TypeError, ValueError, UnicodeError, OSError):
                stats["decode_errors"] += 1
                continue
            if isinstance(raw, Mapping):
                protocol = extract.filter_item_protocol(raw)
            if isinstance(protocol, str) and protocol.strip():
                break
        if not isinstance(protocol, str) or not protocol.strip():
            continue
        stats["protocols_found"] += 1
        if apply and db.set_static_protocol(
            row["token_address"], str(row["network_id"] or ""), protocol.strip()
        ):
            stats["updated"] += 1
    return stats


def _created_at(raw: Any) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    token = raw.get("token")
    token = token if isinstance(token, Mapping) else {}
    value = token.get("createdAt") or raw.get("createdAt")
    if value in (None, ""):
        return None
    return str(value)


def repair_static_created_at(db: RecorderDB, *, apply: bool) -> dict[str, int]:
    """Recover missing token ages from archived static/market payloads."""
    rows = db._conn.execute(
        """SELECT token_address, network_id, recorded_at, raw_json
             FROM token_static
            WHERE token_created_at IS NULL OR token_created_at=''"""
    ).fetchall()
    stats = {"candidates": len(rows), "created_found": 0, "updated": 0, "decode_errors": 0}
    for row in rows:
        created = None
        created_observed_at = None
        payloads = [(row["raw_json"], row["recorded_at"])]
        payloads.extend(
            (market["raw_json"], market["recorded_at"])
            for market in db._conn.execute(
                """SELECT raw_json, recorded_at FROM market_ticks
                     WHERE token_address=? AND network_id=?
                     ORDER BY recorded_at""",
                (row["token_address"], row["network_id"]),
            )
        )
        for payload, observed_at in payloads:
            try:
                created = _created_at(decode_raw(payload))
            except (TypeError, ValueError, UnicodeError, OSError):
                stats["decode_errors"] += 1
                continue
            if created:
                created_observed_at = observed_at
                break
        if not created:
            continue
        stats["created_found"] += 1
        if apply and db.set_static_created_at(
            row["token_address"], str(row["network_id"] or ""), created,
            created_observed_at or row["recorded_at"],
        ):
            stats["updated"] += 1
    return stats


def repair_thesis_from_social(db: RecorderDB, *, apply: bool) -> dict[str, int]:
    """Recover individual thesis rows from archived social snapshots."""
    rows = db._conn.execute(
        """SELECT token_address, network_id, recorded_at, raw_json
             FROM token_social
            ORDER BY recorded_at"""
    ).fetchall()
    stats = {
        "snapshots": len(rows), "items_seen": 0, "items_valid": 0,
        "unique_items": 0, "already_present": 0, "missing_items": 0,
        "inserted": 0, "decode_errors": 0,
    }
    items_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            raw = decode_raw(row["raw_json"])
        except (TypeError, ValueError, UnicodeError, OSError):
            stats["decode_errors"] += 1
            continue
        items = extract.extract_thesis_items(
            raw,
            row["token_address"],
            str(row["network_id"] or ""),
            row["recorded_at"],
        )
        stats["items_seen"] += len(extract.unwrap_thesis(raw))
        stats["items_valid"] += len(items)
        for item in items:
            items_by_id.setdefault(str(item["id"]), item)
    stats["unique_items"] = len(items_by_id)
    if items_by_id:
        ids = list(items_by_id)
        existing: set[str] = set()
        # SQLite has a bounded host-parameter count; keep the repair usable
        # against a full historical snapshot instead of building one huge IN.
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            existing.update(
                str(row[0]) for row in db._conn.execute(
                    f"SELECT id FROM token_thesis WHERE id IN ({placeholders})", batch
                )
            )
        stats["already_present"] = len(existing)
        missing = [item for item_id, item in items_by_id.items() if item_id not in existing]
        stats["missing_items"] = len(missing)
        if apply:
            # Keep write locks short: recorder/labeler/builders share this DB.
            # One transaction for hundreds of thousands of rows would make
            # their 30-second busy timeout expire and turn repair into outage.
            for start in range(0, len(missing), 500):
                stats["inserted"] += db.insert_thesis_items(
                    missing[start : start + 500]
                )
    return stats


def repair(db: RecorderDB, *, apply: bool) -> dict[str, dict[str, int]]:
    return {
        "static_protocol": repair_static_protocols(db, apply=apply),
        "static_created_at": repair_static_created_at(db, apply=apply),
        "thesis": repair_thesis_from_social(db, apply=apply),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    db = RecorderDB(args.db, config.SCHEMA_PATH)
    try:
        report = repair(db, apply=args.apply)
    finally:
        db.close()

    mode = "apply" if args.apply else "dry-run"
    print(f"mode={mode}")
    for name, stats in report.items():
        print(f"{name}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
