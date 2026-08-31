"""Retro backfill: historical tradingActivity events (walking lastId backward in time).

The forward /feed is "newest only" (five pagination parameters ignored — confirmed
live 2026-07-28), while /feed/tradingActivity pages backward with lastId. It yields:
- multi_user_buy/sell events **with the same ids as /feed** (32/42 match live) —
  a direct retro extension of the signal set.
- individual swap_buy/swap_sell events with usdAmount **that /feed never shows
  at all** (0/42 match) — a new universe: large buys/sells at a threshold we
  choose.

What is not recovered: the leaderboard rank at event time (no rank history —
archived hourly since 2026-07-28 for the future), and likes at event time. We
do not fabricate them (FR-007).

The resume point lives in meta (activity_backfill_last_id/oldest_at): transient
outages are frequent in fomo, so each page is retried up to 3 times, then the
position is saved and the run exits safely — a restart continues from where it
stopped, and OR IGNORE inserts make duplicates harmless.

Usage:
    py backfill_activity.py --pages 20              # ~1000 events backward
    py backfill_activity.py --until 2026-06-01      # until a date is reached
    py backfill_activity.py --pages 40 --dry-run    # measure depth without writing
    py backfill_activity.py --head --pages 100   # fill the gap from the newest events
    py backfill_activity.py --reset                 # clear the resume point and start over
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import extract  # noqa: E402
from db import RecorderDB, utcnow_iso  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

_PACING = 1.5          # pacing gap between pages (seconds)
_PAGE_LIMIT = 50       # page size (the confirmed maximum)
_RETRIES = 3           # attempts per page before saving position and exiting
_RETRY_BACKOFF = (5, 15, 30)

META_LAST_ID = "activity_backfill_last_id"
META_OLDEST = "activity_backfill_oldest_at"


def _arg_value(name: str) -> str | None:
    if name in sys.argv:
        try:
            return sys.argv[sys.argv.index(name) + 1]
        except IndexError:
            return None
    return None


def _arg_int(name: str, default: int) -> int:
    v = _arg_value(name)
    if v is not None:
        try:
            return int(v)
        except ValueError:
            pass
    return default


async def _fetch_page(client: Any, last_id: str | None) -> Any:
    """One page with retry on transient errors. Raises after attempts run out."""
    from fomo_api.config import settings

    params: dict = {"limit": _PAGE_LIMIT, "threshold": 0}
    if last_id:
        params["lastId"] = last_id
    last_exc: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            return await client._get(settings.upstream_alerts_path, params)
        except Exception as exc:  # noqa: BLE001 — transient is retried, fatal propagates
            last_exc = exc
            if attempt + 1 < _RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
    raise last_exc  # type: ignore[misc]


async def walk(
    client: Any,
    db: RecorderDB,
    *,
    max_pages: int,
    until: str | None = None,
    dry_run: bool = False,
    sleep=asyncio.sleep,
) -> dict:
    """Walks backward in time from the resume point (or from the newest). Returns stats.

    Stops at: an empty page (end of history), reaching max_pages, or reaching --until.
    """
    last_id = db.get_meta(META_LAST_ID)
    oldest_seen = db.get_meta(META_OLDEST)
    stats = {"pages": 0, "events": 0, "added": 0, "types": {}, "oldest": oldest_seen,
             "resumed_from": last_id, "dry_run": dry_run, "stopped": None}
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        fetched_at = utcnow_iso()
        raw = await _fetch_page(client, last_id)
        items, has_next = extract.activity_page(raw)
        rows = [extract.extract_activity_event(e, fetched_at) for e in items]
        rows = [r for r in rows if r and r["id"] not in seen]
        if not rows:
            stats["stopped"] = "empty_page"
            break
        seen.update(r["id"] for r in rows)

        added = len(rows) if dry_run else db.insert_activity_events(rows)
        stats["pages"] = page
        stats["events"] += len(rows)
        stats["added"] += added
        for r in rows:
            stats["types"][r["event_type"]] = stats["types"].get(r["event_type"], 0) + 1
        ts_min = min((r["ts"] for r in rows if r["ts"]), default=None)
        if ts_min and (oldest_seen is None or ts_min < oldest_seen):
            oldest_seen = ts_min
        stats["oldest"] = oldest_seen
        last_id = rows[-1]["id"]

        if not dry_run:
            db.set_meta(META_LAST_ID, last_id)
            if oldest_seen:
                db.set_meta(META_OLDEST, oldest_seen)

        if until and ts_min and ts_min <= until:
            stats["stopped"] = f"until:{until}"
            break
        if not has_next:
            stats["stopped"] = "has_next_page_false"
            break
        await sleep(_PACING)
    else:
        stats["stopped"] = "max_pages"
    return stats


async def walk_head(
    client: Any,
    db: RecorderDB,
    *,
    max_pages: int,
    dry_run: bool = False,
    sleep=asyncio.sleep,
) -> dict:
    """Walk from the newest page until it overlaps stored activity.

    The historical cursor may legitimately point at the end of an old run.
    Starting from that cursor cannot discover events that arrived afterwards.
    This repair path deliberately keeps the historical cursor untouched and
    uses the durable event id as its stop boundary.
    """
    stats = {
        "pages": 0, "events": 0, "added": 0, "types": {},
        "oldest": None, "resumed_from": None, "dry_run": dry_run,
        "stopped": None,
    }
    head_last_id: str | None = None
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        fetched_at = utcnow_iso()
        raw = await _fetch_page(client, head_last_id)
        items, has_next = extract.activity_page(raw)
        rows = [extract.extract_activity_event(e, fetched_at) for e in items]
        rows = [r for r in rows if r and r["id"] not in seen]
        if not rows:
            stats["stopped"] = "empty_page"
            break
        seen.update(r["id"] for r in rows)

        ids = [str(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        existing = {
            str(row[0]) for row in db._conn.execute(
                f"SELECT id FROM activity_events WHERE id IN ({placeholders})", ids
            )
        }
        added = len(rows) if dry_run else db.insert_activity_events(rows)
        stats["pages"] = page
        stats["events"] += len(rows)
        stats["added"] += added
        for row in rows:
            stats["types"][row["event_type"]] = (
                stats["types"].get(row["event_type"], 0) + 1
            )
        ts_min = min((row["ts"] for row in rows if row["ts"]), default=None)
        if ts_min and (stats["oldest"] is None or ts_min < stats["oldest"]):
            stats["oldest"] = ts_min

        if existing:
            stats["stopped"] = "overlap"
            break
        if not has_next:
            stats["stopped"] = "has_next_page_false"
            break
        head_last_id = rows[-1]["id"]
        await sleep(_PACING)
    else:
        stats["stopped"] = "max_pages"
    return stats


def _load_client():
    from fomo_api.auth.credential_store import CredentialStore
    from fomo_api.clients.fomo_client import FomoClient

    creds = CredentialStore(config.credential_state_path()).load()
    if creds is None or not creds.access_token:
        raise RuntimeError("No valid credential — start the api service first.")
    return FomoClient(session_token=creds.access_token)


async def main() -> None:
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    head = "--head" in sys.argv
    if head and "--reset" in sys.argv:
        raise SystemExit("--head and --reset cannot be combined")
    if "--reset" in sys.argv:
        db._conn.execute(
            "DELETE FROM meta WHERE key IN (?, ?)", (META_LAST_ID, META_OLDEST)
        )
        db._conn.commit()
        print("Resume point cleared — starting from the newest.")
    client = _load_client()
    try:
        if head:
            stats = await walk_head(
                client, db,
                max_pages=_arg_int("--pages", 20),
                dry_run="--dry-run" in sys.argv,
            )
        else:
            stats = await walk(
                client, db,
                max_pages=_arg_int("--pages", 20),
                until=_arg_value("--until"),
                dry_run="--dry-run" in sys.argv,
            )
    finally:
        await client.aclose()
    tag = " (dry-run — no writes)" if stats["dry_run"] else ""
    print(f"pages: {stats['pages']} · events: {stats['events']} · added: {stats['added']}{tag}")
    print(f"oldest event: {stats['oldest']} · stopped: {stats['stopped']}")
    print("types:", dict(sorted(stats["types"].items(), key=lambda kv: -kv[1])))
    if not stats["dry_run"]:
        print(f"total activity_events now: {db.activity_count()}")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
