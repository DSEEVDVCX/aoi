"""Archive 1-minute bars for decision windows — for every main-group token.

The owner (2026-08-28): "Run it for every token, not just the new ones — I am
working with another AI to extract the best training method and I do not want
to wait for the control arm." Archiving opens the outer initial-envelope
analysis: minute precision for `time_to_plus20` and simulation, and the first
15 minutes after the signal (the early-explosion announcement window).

Live measurement before building (2026-08-28):
- `resolution=1` works on /proxy/getBarsNew (200 bars/call live).
- **Archive is available**: a 34-day-old token returned 500 bars from its
  signal window.
- 704 main-group tokens × ~6 calls (500/call × a 48h window) = 4,224 calls for
  the full set.

Design: resumable (historical_bars_state with resolution='1', position kept
in cursor_to), no hand-written SQL (writes via db.insert_bars), and polite
timing that respects the limit (one call every PACING seconds). Tokens the
source does not return (no_data) are not retried on every run.

Usage:
    python backfill_bars_1m.py              # diagnose
    python backfill_bars_1m.py --apply      # execute (auto-resume)
    python backfill_bars_1m.py --apply --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402
from db import RecorderDB, utcnow_iso  # noqa: E402

RES = "1"
PAGE = 500                    # measured: the most the source returns at 1-minute resolution
PACING = 1.0                  # seconds between calls — cautious with the fomo balance
WINDOW_H = 48                 # the decision window


async def _load_client():
    """A live fomo client: local-server token from disk (same path as the recorder)."""
    import recorder

    return recorder._build_client(recorder._load_access_token())


def _targets(db: RecorderDB) -> list[dict]:
    """Every main-group token: the earliest decision per token — its window is the range.

    The control arm and active ones too: the owner asked for "every token" — we
    include watched entries (kind=watch) since their windows are themselves an
    analyzable decision, and signal-only ones as well.
    """
    rows = db._conn.execute(
        """SELECT o.token_address, o.network_id, MIN(o.entry_ts) AS entry_ts
             FROM outcomes o
             JOIN training_rows t ON t.kind = o.kind AND t.key = o.key
            WHERE t.is_live = 1 AND t.is_independent = 1
              AND t.asset_class = 'meme' AND t.status = 'ok'
              AND o.status = 'ok'
            GROUP BY o.token_address, o.network_id
            ORDER BY entry_ts""",
    ).fetchall()
    return [dict(r) for r in rows]


def _stamp(db: RecorderDB, key: str, value: str) -> None:
    """Record lifecycle metadata without turning bookkeeping into a new failure."""
    try:
        db.note_error(key, value)
    except Exception:  # noqa: BLE001 — health bookkeeping must not hide archive work
        pass


def _state(db: RecorderDB, token: str, net: str) -> dict | None:
    row = db._conn.execute(
        """SELECT cursor_to, oldest_ts, last_status, candles, calls, attempts
             FROM historical_bars_state
            WHERE token_address=? AND network_id=? AND resolution=?""",
        (token, net, RES),
    ).fetchone()
    return dict(row) if row else None


def _save_state(db: RecorderDB, token: str, net: str, *, cursor_to: int | None,
                oldest_ts: int | None, status: str, candles: int, calls: int) -> None:
    db._conn.execute(
        """INSERT INTO historical_bars_state(
               token_address, network_id, resolution, cursor_to, oldest_ts,
               last_status, candles, calls, attempts, updated_at)
           VALUES(?,?,?,?,?,?,?,?,
                  COALESCE((SELECT attempts FROM historical_bars_state
                            WHERE token_address=? AND network_id=? AND resolution=?),0)+1,?)
           ON CONFLICT(token_address, network_id, resolution) DO UPDATE SET
               cursor_to=excluded.cursor_to, oldest_ts=excluded.oldest_ts,
               last_status=excluded.last_status, candles=excluded.candles,
               calls=excluded.calls, attempts=excluded.attempts,
               updated_at=excluded.updated_at""",
        (token, net, RES, cursor_to, oldest_ts, status, candles, calls,
         token, net, RES, time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())),
    )
    db._commit()


def _extract_bars(raw: dict) -> list[dict]:
    """getBarsNew envelope → bar rows (same shape as the recorder's extractor)."""
    ro = raw.get("responseObject") if isinstance(raw, dict) else None
    if not isinstance(ro, dict):
        return []
    ts = ro.get("t") or []
    out = []
    for i, t in enumerate(ts):
        try:
            out.append({
                "token_address": None,  # filled by the caller
                "network_id": None,
                "resolution": RES,
                "ts": int(t),
                "o": ro["o"][i], "h": ro["h"][i],
                "l": ro["l"][i], "c": ro["c"][i],
                "v": ro["v"][i] if ro.get("v") else 0,
                "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
                "fetched_at": None,
            })
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


async def archive_token(client, db: RecorderDB, t: dict, stats: dict) -> None:
    """Archive a 48h window (decision start − context) for one token — resumes from cursor_to."""
    token, net = t["token_address"], str(t["network_id"])
    entry = int(t["entry_ts"])
    start = entry - 3600                      # one hour of context before the decision
    end = entry + WINDOW_H * 3600
    st = _state(db, token, net)
    if st and st["last_status"] == "ok":
        stats["already_done"] += 1
        return
    cursor = st["cursor_to"] if st and st["cursor_to"] else end
    candles = st["candles"] if st else 0
    calls = st["calls"] if st else 0
    from recorder import _fetch_bars_raw

    while cursor > start:
        try:
            raw = await _fetch_bars_raw(client, token, net, start, cursor,
                                        resolution=RES)
        except Exception:  # noqa: BLE001 — transient failure: back off then retry
            # Measurement 2026-08-28: a transient 404 wave stalled 456 tokens
            # in a minute (they work on retry) — so the right move is two
            # short backoffs before giving up, and the partial state keeps a
            # later resume safe.
            retried = False
            for wait_s in (5, 15):
                await asyncio.sleep(wait_s)
                try:
                    raw = await _fetch_bars_raw(client, token, net, start,
                                                cursor, resolution=RES)
                    retried = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not retried:
                _save_state(db, token, net, cursor_to=cursor,
                            oldest_ts=None, status="partial",
                            candles=candles, calls=calls)
                stats["errors"] += 1
                return
        calls += 1
        bars = _extract_bars(raw)
        if not bars:
            # no data in this segment: either the start of the series or a gap.
            _save_state(db, token, net, cursor_to=start, oldest_ts=cursor,
                        status="ok", candles=candles, calls=calls)
            stats["done"] += 1
            return
        for b in bars:
            b["token_address"] = token
            b["network_id"] = net
            b["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                            time.gmtime())
        wrote = db.insert_bars(bars)
        candles += len(bars)
        oldest = min(int(b["ts"]) for b in bars)
        cursor = min(cursor, oldest) - 60     # drop one minute below the oldest bar
        stats["rows"] += wrote
        _save_state(db, token, net, cursor_to=cursor, oldest_ts=oldest,
                    status="partial", candles=candles, calls=calls)
        await asyncio.sleep(PACING)
    _save_state(db, token, net, cursor_to=start, oldest_ts=start,
                status="ok", candles=candles, calls=calls)
    stats["done"] += 1


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    started = utcnow_iso()
    _stamp(db, "bars_1m_last_run_at", started)
    try:
        targets = _targets(db)
        if args.limit:
            targets = targets[: args.limit]
        done_prior = sum(
            1 for t in targets
            if (s := _state(db, t["token_address"], str(t["network_id"])))
            and s["last_status"] == "ok"
        )
        print(f"main-group targets: {len(targets):,} "
              f"(already done: {done_prior:,})")
        if not args.apply:
            print("\ndiagnosis only — pass --apply to execute.")
            return 0

        stats = {"done": 0, "rows": 0, "errors": 0, "already_done": 0}
        client = await _load_client()
        try:
            for i, t in enumerate(targets):
                await archive_token(client, db, t, stats)
                if (i + 1) % 10 == 0:
                    print(f"  progress: {i+1}/{len(targets)} · "
                          f"rows={stats['rows']:,} · done={stats['done']} · "
                          f"err={stats['errors']}", flush=True)
        finally:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
        print(f"round complete: done={stats['done']} · rows={stats['rows']:,} · "
              f"err={stats['errors']} · skip={stats['already_done']}")
        _stamp(db, "bars_1m_last_ok_at", utcnow_iso())
        _stamp(db, "bars_1m_last_stats", str(stats))
        return 0
    except Exception as exc:
        _stamp(db, "last_error_bars_1m", f"{utcnow_iso()}: {type(exc).__name__}: {exc}"[:400])
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
