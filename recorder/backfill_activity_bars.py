"""Fetch bars for retro activity_events so they can be labeled.

For each token in activity_events we fetch 5-minute bars via getBarsNew around
its event times ([ts − 1 hour, ts + 48h + margin]), making them labelable with
the same compute_labels.

**Time clusters**: one token's events are grouped into merged windows (two
events whose windows intersect = one fetch), then split into ≤72-hour chunks per
call (900-bar cap = 75 hours). A token with scattered events over 9 months is
not fetched for the whole span — only around its events.

**OHLCV decay is a measured property**: a token with no price series at fomo
(dead/decayed) is labeled no_data and excluded after MAX_ATTEMPTS — its share
is the **survivorship bias of the retro set**, read in the report, not hidden.
The labeler never labels an event until its token reaches status='ok' (the
activities_pending_label gate), so there is no eternal no_entry before bars
arrive.

Resumable: completed (ok) tokens are skipped; error/no_data tokens are retried
up to the cap. The labeler running every 15 minutes picks up completions
automatically.

Usage:
    py backfill_activity_bars.py                 # all tokens (resumable)
    py backfill_activity_bars.py --max-calls 200 # call cap for this round
    py backfill_activity_bars.py --dry-run       # size report, no network, no writes
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

from recorder import _fetch_bars_raw  # noqa: E402  (same request body confirmed live)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

_PACING = 1.5
_CHUNK_SECONDS = 72 * 3600          # below the 900-bar cap (75h), with margin
_PRE_EVENT_SECONDS = 3600           # one hour of context before the event (entry bar at/after ts)
_MAX_ATTEMPTS = config.BARS_MAX_NO_DATA_ATTEMPTS


def event_clusters(ts_epochs: list[int], window_h: int, margin_s: int) -> list[tuple[int, int]]:
    """Ascending event times (epoch) → merged fetch windows [from, to].

    Two events ≤ (48h window + margin) apart have intersecting windows ⇒ one
    fetch. Otherwise a new cluster starts. Each window = [first event − 1 hour,
    last event + 48h + margin].
    """
    if not ts_epochs:
        return []
    w = window_h * 3600
    out: list[tuple[int, int]] = []
    start = prev = ts_epochs[0]
    for t in ts_epochs[1:]:
        if t > prev + w + margin_s:
            out.append((start - _PRE_EVENT_SECONDS, prev + w + margin_s))
            start = t
        prev = t
    out.append((start - _PRE_EVENT_SECONDS, prev + w + margin_s))
    return out


def _chunks(start: int, end: int, span: int = _CHUNK_SECONDS) -> list[tuple[int, int]]:
    out = []
    while start < end:
        out.append((start, min(start + span, end)))
        start += span
    return out


def _epoch(iso: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _load_client():
    from fomo_api.auth.credential_store import CredentialStore
    from fomo_api.clients.fomo_client import FomoClient

    creds = CredentialStore(config.credential_state_path()).load()
    if creds is None or not creds.access_token:
        raise RuntimeError("No valid credential — start the api service first.")
    return FomoClient(session_token=creds.access_token)


async def run(
    client: Any,
    db: RecorderDB,
    *,
    max_calls: int,
    dry_run: bool = False,
    sleep=asyncio.sleep,
) -> dict:
    stats = {"tokens_done": 0, "tokens_no_data": 0, "tokens_error": 0,
             "calls": 0, "candles": 0, "skipped_ok": 0, "skipped_dead": 0,
             "dry_run": dry_run}
    tokens = db.activity_event_tokens()

    for t in tokens:
        addr, net = t["token_address"], str(t["network_id"] or "")
        state = db.activity_bars_state(addr, net)
        if state and state["last_status"] == "ok":
            stats["skipped_ok"] += 1
            continue
        if state and state["last_status"] == "no_data" and state["attempts"] >= _MAX_ATTEMPTS:
            stats["skipped_dead"] += 1
            continue

        events = db.activity_events_for_token(addr, net)
        clusters = event_clusters(
            [_epoch(e["ts"]) for e in events],
            config.LABEL_WINDOW_HOURS, config.LABEL_MARGIN_SECONDS,
        )
        if dry_run:
            stats["calls"] += sum(len(_chunks(f, to)) for f, to in clusters)
            stats["tokens_done"] += 1
            continue

        candles, got_data, failed = 0, False, False
        for f, to in clusters:
            for cf, ct in _chunks(f, to):
                if stats["calls"] >= max_calls:
                    stats["stopped_at"] = f"{addr}:{net}"
                    return stats
                try:
                    raw = await _fetch_bars_raw(client, addr, net, cf, ct)
                    rows = extract.extract_bars(
                        raw, addr, net, config.BARS_RESOLUTION, utcnow_iso()
                    )
                    candles += db.insert_bars(rows)
                    got_data = got_data or bool(rows)
                except Exception as exc:  # noqa: BLE001 — one token must not sink the round
                    failed = True
                    db.set_meta(
                        "last_error_activity_bars",
                        f"{utcnow_iso()}: {addr[:12]}…: {type(exc).__name__}: {exc}",
                    )
                stats["calls"] += 1
                await sleep(_PACING)

        if got_data:
            db.recompute_bar_flags(addr, net, config.BARS_RESOLUTION)
            db.set_activity_bars_state(addr, net, "ok", candles, utcnow_iso())
            stats["tokens_done"] += 1
        elif failed:
            db.set_activity_bars_state(addr, net, "error", 0, utcnow_iso())
            stats["tokens_error"] += 1
        else:
            db.set_activity_bars_state(addr, net, "no_data", 0, utcnow_iso())
            stats["tokens_no_data"] += 1
        stats["candles"] += candles
    return stats


async def main() -> None:
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    dry = "--dry-run" in sys.argv
    max_calls = 10**9
    if "--max-calls" in sys.argv:
        try:
            max_calls = int(sys.argv[sys.argv.index("--max-calls") + 1])
        except (IndexError, ValueError):
            pass
    if dry:
        # no client, no network: size report only
        stats = await run(None, db, max_calls=max_calls, dry_run=True)  # type: ignore[arg-type]
        total = db.activity_count()
        print(f"events: {total} · tokens to process this round: {stats['tokens_done']}"
              f" (done: {stats['skipped_ok']} · dead: {stats['skipped_dead']})")
        print(f"estimated calls: {stats['calls']} ≈ {stats['calls'] * 2.2 / 60:.0f} minutes at {_PACING}s pacing")
        db.close()
        return
    client = _load_client()
    try:
        stats = await run(client, db, max_calls=max_calls)
    finally:
        await client.aclose()
    print(stats)
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
