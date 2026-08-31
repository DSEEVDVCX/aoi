"""A read-only data-access layer for recorder.db.

It opens the database via a URI with mode=ro so it cannot write at all — it
never blocks the recorder's writes (WAL). Every query is a pure function
taking a connection, testable against a temporary database.

Note: we open a new connection per request (cheap for local SQLite) and close
it — simpler than sharing a connection across FastAPI threads.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any


def connect_ro(db_path: str) -> sqlite3.Connection:
    """A read-only connection. mode=ro blocks any write at the SQLite level itself."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """The dashboard reads a database the recorder writes; the dashboard's version may
    run ahead of the recorder's migration (or behind). The check makes the new
    column optional instead of crashing the dashboard."""
    if not _table_exists(conn, table):
        return False
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


# --- meta ---
def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    if not _table_exists(conn, "meta"):
        return None
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def all_meta(conn: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(conn, "meta"):
        return {}
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}


# --- counts ---
_TABLES = ("signal_events", "market_ticks", "token_static", "watchlist", "snapshots",
           "outcomes", "token_bars", "activity_events")


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in _TABLES:
        if _table_exists(conn, t):
            out[t] = conn.execute("SELECT COUNT(*) AS n FROM " + t).fetchone()["n"]
        else:
            out[t] = 0
    return out


def active_watch_count(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "watchlist"):
        return 0
    return conn.execute("SELECT COUNT(*) AS n FROM watchlist WHERE active=1").fetchone()["n"]


def control_maturity(
    conn: sqlite3.Connection,
    preliminary_target: int,
    decision_target: int,
    design_version: int = 3,
) -> dict[str, Any]:
    """Qualified-control progress; `ok` alone is a complete, comparable 48h window."""
    row = None
    if _table_exists(conn, "outcomes"):
        required = ("design_version", "analysis_eligible", "is_control", "entry_ts")
        if all(_has_column(conn, "outcomes", column) for column in required):
            row = conn.execute(
                """SELECT COUNT(*) AS completed, MIN(entry_ts) AS first_entry_ts,
                          MAX(entry_ts) AS last_entry_ts
                     FROM outcomes
                    WHERE kind='watch' AND is_control=1 AND status='ok'
                      AND design_version>=? AND analysis_eligible=1""",
                (design_version,),
            ).fetchone()
    completed = int(row["completed"]) if row else 0
    preliminary_target = max(1, int(preliminary_target))
    decision_target = max(preliminary_target, int(decision_target))
    return {
        "completed": completed,
        "preliminary_target": preliminary_target,
        "decision_target": decision_target,
        "preliminary_remaining": max(0, preliminary_target - completed),
        "decision_remaining": max(0, decision_target - completed),
        "preliminary_pct": round(min(100, completed / preliminary_target * 100), 1),
        "decision_pct": round(min(100, completed / decision_target * 100), 1),
        "preliminary_ready": completed >= preliminary_target,
        "decision_ready": completed >= decision_target,
        "design_version": design_version,
        "first_entry_ts": row["first_entry_ts"] if row else None,
        "last_entry_ts": row["last_entry_ts"] if row else None,
    }


# --- Labeling and outcomes (outcomes/training_rows) ---
def _last_labeled(conn: sqlite3.Connection) -> str | None:
    if not _table_exists(conn, "outcomes"):
        return None
    row = conn.execute("SELECT MAX(labeled_at) FROM outcomes").fetchone()
    return row[0] if row else None


def last_labeled_at(conn: sqlite3.Connection) -> str | None:
    """The last labeling stamp — the labeler's pulse from its output, with no table scans.

    A single-moment query, read live on every request on top of the cached
    summary (see `/api/labeling` in app.py).
    """
    return _last_labeled(conn)


def labeling_outcomes(
    conn: sqlite3.Connection,
    live_start_ts: int,
    *,
    design_version: int = 3,
    gate_targets: tuple[int, int] = (100, 500),
    eta_days: int = 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A summary of what the pipeline produced once its windows closed — labeling and training rows.

    This is the output the project was built for, and it was missing from the
    whole dashboard: the "performance" display derives from the **running**
    window (open positions not yet complete), while this is about **final**
    outcomes after 48 hours — the ones `is_explosive` is labeled from and
    training rows are built from.

    - The tally is restricted to the live epoch (`live_start_ts`): what came
      before it is retro collection without the live families, excluded from
      training with its rows deleted, so mixing it with the live data corrupts
      every ratio.
    - The ratios (final_return_48h) are fractions, not percentages, and the
      median is computed in Python — `median()` is not built into SQLite, so
      it can't be relied on in SQL.
    - `eta` for the gate is estimated from the average daily production, not
      from a single last day: one zero day would have said "the gate is never
      reached".
    """
    if not _table_exists(conn, "outcomes"):
        return {"live": False, "has_outcomes_table": False}
    # The dashboard may run ahead of the recorder's migration (a new column
    # that hasn't arrived yet), so missing columns hide the dashboard rather
    # than crash it — the same guard as `control_maturity` above.
    required = (
        "kind", "status", "entry_ts", "is_control", "is_explosive",
        "final_return_48h", "max_gain_48h", "design_version",
        "analysis_eligible", "labeled_at",
    )
    missing = [c for c in required if not _has_column(conn, "outcomes", c)]
    if missing:
        return {"live": False, "has_outcomes_table": True, "missing_columns": missing}

    moment = now or datetime.now(UTC)

    def _median_pct(values: list[float]) -> float | None:
        """The median of a ratio from fractional values — in Python, not in SQL.

        `median()` is not a built-in SQLite function (it's an extension loaded
        conditionally), and a version check would have passed it confidently
        and then blown up at runtime. The list here is small (tens of
        thousands), so sorting it in memory is cheaper than a silent error.
        """
        if not values:
            return None
        ordered = sorted(values)
        n = len(ordered)
        mid = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
        return round(mid * 100, 1)

    # The labeling tally for the live epoch — per kind×status, and the gates
    # (ok) break down their exclusions, tallying where the samples went and why.
    status_rows = conn.execute(
        "SELECT kind, status, COUNT(*) AS n FROM outcomes"
        " WHERE entry_ts >= ? GROUP BY kind, status",
        (live_start_ts,),
    ).fetchall()
    status_counts = [
        {"kind": r["kind"], "status": r["status"], "count": int(r["n"])}
        for r in status_rows
    ]
    exclusion_rows = conn.execute(
        "SELECT status, exclusion_reason, COUNT(*) AS n FROM outcomes"
        " WHERE entry_ts >= ? AND status != 'ok'"
        " GROUP BY status, exclusion_reason ORDER BY n DESC",
        (live_start_ts,),
    ).fetchall()
    exclusions = [
        {
            "status": r["status"],
            "reason": r["exclusion_reason"] or "—",
            "count": int(r["n"]),
        }
        for r in exclusion_rows
    ]

    # The analysis-eligible set: a labeled window, healthy, in the current design.
    # (kind='watch' is the compared decision — signal and control together —
    # while 'signal' is bookkeeping labeling of every event and never enters
    # the comparison.)
    # The live-epoch filter applies here too: the side tally used to include
    # the retro data.
    analysis_where = (
        "kind='watch' AND status='ok' AND analysis_eligible=1 AND design_version>=?"
        " AND entry_ts >= ?"
    )
    analysis_args = (design_version, live_start_ts)

    def _group_stats(where_extra: str, args_extra: tuple = ()) -> dict[str, Any]:
        where = analysis_where + where_extra
        args = analysis_args + args_extra
        row = conn.execute(
            f"SELECT COUNT(*) AS n, SUM(is_explosive) AS explosive,"
            f"       AVG(final_return_48h) AS avg_ret,"
            f"       AVG(max_gain_48h) AS avg_gain,"
            f"       MAX(entry_ts) AS last_entry_ts"
            f"  FROM outcomes WHERE {where}",
            args,
        ).fetchone()
        # The median from the values themselves, in Python — see `_median_pct`.
        median_values = [
            float(r[0]) for r in conn.execute(
                f"SELECT final_return_48h FROM outcomes WHERE {where}"
                f" AND final_return_48h IS NOT NULL",
                args,
            )
        ]
        return {
            "count": int(row["n"] or 0),
            "explosive": int(row["explosive"] or 0),
            "explosive_pct": round(row["explosive"] / row["n"] * 100, 1)
            if row["n"] else None,
            "avg_return_pct": round(row["avg_ret"] * 100, 1)
            if row["avg_ret"] is not None else None,
            "avg_gain_pct": round(row["avg_gain"] * 100, 1)
            if row["avg_gain"] is not None else None,
            "median_return_pct": _median_pct(median_values),
            "last_entry_ts": row["last_entry_ts"],
        }

    signal_stats = _group_stats(" AND is_control=0")
    control_stats = _group_stats(" AND is_control=1")

    # The decision gate from the control group's actual production: the rate of
    # mature days + extrapolating arrival at both targets. The window is 48h,
    # so any day within the last two days may not have its windows complete
    # yet (the labeler labels only what's available) — including it in the
    # average would drag it down falsely, so we exclude it and state when the
    # last day included in the computation was.
    prelim_target, decision_target = gate_targets
    now_ts = int(moment.timestamp())
    per_day = conn.execute(
        "SELECT CAST(entry_ts / 86400 AS INTEGER) AS day, COUNT(*) AS n"
        "  FROM outcomes"
        " WHERE kind='watch' AND is_control=1 AND status='ok'"
        "   AND analysis_eligible=1 AND design_version>=?"
        "   AND entry_ts >= ? AND entry_ts >= ?"
        " GROUP BY day",
        (design_version, live_start_ts, now_ts - eta_days * 86400),
    ).fetchall()
    today_day = now_ts // 86400
    mature = [(int(r["day"]), int(r["n"])) for r in per_day if int(r["day"]) <= today_day - 2]
    avg_per_day = (sum(n for _, n in mature) / len(mature)) if mature else None
    remaining = max(0, decision_target - control_stats["count"])
    eta = (
        {"days": round(remaining / avg_per_day, 1)}
        if avg_per_day and remaining else None
    )

    # Labeled daily production (all kinds) for the last eta_days — shows the pace of collection.
    recent_days = conn.execute(
        "SELECT CAST(entry_ts / 86400 AS INTEGER) AS day, COUNT(*) AS n"
        "  FROM outcomes"
        " WHERE entry_ts >= ?"
        " GROUP BY day ORDER BY day DESC LIMIT ?",
        (int(moment.timestamp()) - eta_days * 86400, eta_days),
    ).fetchall()
    labeled_per_day = [
        {
            "day": datetime.fromtimestamp(d * 86400, UTC).date().isoformat(),
            "count": n,
        }
        for d, n in (
            (int(r["day"]), int(r["n"])) for r in reversed(recent_days)
        )
    ]

    # Training rows — modeling readiness without an approved training (the 500 gate is binding).
    training: dict[str, Any] = {"available": False}
    if _table_exists(conn, "training_rows"):
        version_rows = conn.execute(
            "SELECT feature_version, split, COUNT(*) AS n,"
            "       SUM(is_explosive) AS explosive, MAX(built_at) AS last_built"
            "  FROM training_rows GROUP BY feature_version, split"
            " ORDER BY feature_version DESC, split",
        ).fetchall()
        versions: dict[int, dict[str, Any]] = {}
        for r in version_rows:
            v = versions.setdefault(int(r["feature_version"]), {
                "feature_version": int(r["feature_version"]),
                "splits": {},
                "total": 0,
                "explosive": 0,
                "last_built_at": r["last_built"],
            })
            v["splits"][r["split"]] = {
                "count": int(r["n"]),
                "explosive": int(r["explosive"] or 0),
            }
            v["total"] += int(r["n"])
            v["explosive"] += int(r["explosive"] or 0)
        # "Current" = the highest feature version, the one that will be trained
        # when the gate opens. The highest version may be **half-built** (fv16
        # started 08-28 and isn't complete yet), so it gets classified as
        # "building" without a misleading explosion rate.
        current = None
        if versions:
            top = max(versions.values(), key=lambda v: v["feature_version"])
            # Fully built = the last row's date is recent (within a day of now)
            # — the builder runs hourly, so a version not built for more than a
            # day is stopped, not running.
            last_built = _parse_iso(top["last_built_at"]) if top["last_built_at"] else None
            building = bool(
                last_built and (moment - last_built).total_seconds() <= 86400
            )
            current = {**top, "building": building}
        training = {
            "available": True,
            "versions": sorted(
                versions.values(), key=lambda v: -v["feature_version"]
            ),
            "current": current,
        }

    # Last labeling activity — the labeler's pulse from its output, not from its cycle stamp.
    last_labeled_at = _last_labeled(conn)

    return {
        "live": True,
        "has_outcomes_table": True,
        "live_start_ts": live_start_ts,
        "design_version": design_version,
        "status_counts": status_counts,
        "exclusions": exclusions,
        "signal": signal_stats,
        "control": control_stats,
        "gate": {
            "completed": control_stats["count"],
            "preliminary_target": prelim_target,
            "decision_target": decision_target,
            "preliminary_ready": control_stats["count"] >= prelim_target,
            "decision_ready": control_stats["count"] >= decision_target,
            "avg_per_day": round(avg_per_day, 1) if avg_per_day else None,
            "remaining": remaining,
            "eta": eta,
        },
        "labeled_per_day": labeled_per_day,
        "training": training,
        "last_labeled_at": last_labeled_at,
    }


# --- recorder status ---
def recorder_status(
    conn: sqlite3.Connection,
    alive_window_seconds: int,
    labeler_window_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The recorder's status: alive? cycle count, last cycle, its stats, errors.

    `labeler_window_seconds` is mandatory with no default: the value lives in
    config alone, and a duplicate default here would silently drift from it on
    any later adjustment.
    """
    now = now or datetime.now(UTC)
    meta = all_meta(conn)
    last_cycle_at = meta.get("last_cycle_at")
    last_dt = _parse_iso(last_cycle_at)
    seconds_since = None
    alive = False
    if last_dt is not None:
        seconds_since = (now - last_dt).total_seconds()
        alive = 0 <= seconds_since <= alive_window_seconds

    def _int(v: str | None) -> int:
        return int(v) if v and v.lstrip("-").isdigit() else 0

    # Freshness of the source feed — fully separate from the recorder's
    # aliveness: the recorder may run error-free while fomo's feed has been
    # frozen for hours, making the archive look like a "quiet market" when
    # it's really an upstream outage. Seen frozen for 3 hours.
    feed_dt = _parse_iso(meta.get("last_feed_event_at"))
    feed_age = (now - feed_dt).total_seconds() if feed_dt else None

    # Labeler (FomoLabeler) aliveness: it writes labeler_last_run_at every
    # cycle (15 minutes) even when it labels nothing. Its death is completely
    # silent — no errors, no cycle crashes — while the results stop
    # accumulating, and that only surfaces at the maturity gate.
    labeler_dt = _parse_iso(meta.get("labeler_last_run_at"))
    labeler_age = (now - labeler_dt).total_seconds() if labeler_dt else None

    admission_networks: dict[str, Any] = {}
    raw_admission = meta.get("evm_admission_network_state")
    if raw_admission:
        try:
            decoded = json.loads(raw_admission)
            if isinstance(decoded, dict):
                admission_networks = decoded
        except (TypeError, ValueError):
            admission_networks = {}
    paused_networks: list[str] = []
    raw_paused = meta.get("evm_admission_paused_networks")
    if raw_paused:
        try:
            decoded = json.loads(raw_paused)
            if isinstance(decoded, list):
                paused_networks = [str(network) for network in decoded]
        except (TypeError, ValueError):
            paused_networks = []

    return {
        "alive": alive,
        "seconds_since_last_cycle": seconds_since,
        "last_feed_event_at": meta.get("last_feed_event_at"),
        "feed_age_seconds": feed_age,
        # Stale = the last event much older than double the aliveness window (15 minutes)
        "feed_stale": bool(feed_age is not None and feed_age > 900),
        "last_cycle_at": last_cycle_at,
        "cycles_total": _int(meta.get("cycles_total")),
        "errors_total": _int(meta.get("errors_total")),
        "cycle_crashes": _int(meta.get("cycle_crashes")),
        "last_cycle_stats": meta.get("last_cycle_stats"),
        "started_at": meta.get("started_at"),
        "schema_version": meta.get("schema_version"),
        "active_watch_count": active_watch_count(conn),
        # Labeler: None = never ran — treated as stale in the display.
        "labeler_last_run_at": meta.get("labeler_last_run_at"),
        "labeler_age_seconds": labeler_age,
        "labeler_stale": bool(labeler_age is None or labeler_age > labeler_window_seconds),
        "labeler_last_stats": meta.get("labeler_last_stats"),
        "evm_admission_networks": admission_networks,
        "evm_admission_paused_networks": paused_networks,
    }


def recorder_errors(
    conn: sqlite3.Connection,
    sources: tuple[str, ...],
    ok_stamps: dict[str, tuple[str, ...]] | None = None,
    recorder_stamps: tuple[str, ...] = ("started_at", "last_ok_cycle_at"),
) -> list[dict[str, Any]]:
    """The last error per source from meta (last_error_<src>). Absence = no error for that source.

    Note: `last_error_<src>` is a sticky meta value — written on every failure
    and never cleared on success, so it keeps showing the last error even if
    the source recovered. So we flag the error as **stale** when it predates a
    later success stamp.

    **And each source has its own boundary.** It used to be one shared boundary
    built from `started_at` and `last_ok_cycle_at`, which only `recorder.py`
    writes; so when the recorder died for 3h14m on 2026-08-17 the boundary
    froze and the chain/chain_auth/evm badges stayed red with errors that had
    already healed — and `FomoChain` completing its clean cycles meant
    nothing. So each queue got its own success stamp from its own writer
    (`chain_last_ok_at`…). A source that is declared here has an independent
    stamp and never falls back to the recorder's stamp in its absence: a
    missing stamp means it hasn't proven success yet, and healing it with
    another process's stamp would hide a first-run failure. The recorder's
    boundary remains only for sources that declare no independent stamps in
    `ok_stamps`.
    """
    meta = all_meta(conn)
    stamps = ok_stamps or {}

    def _boundary(keys: tuple[str, ...]) -> datetime | None:
        moments = [_parse_iso(meta.get(key)) for key in keys]
        return max((m for m in moments if m is not None), default=None)

    recorder_boundary = _boundary(recorder_stamps)
    out = []
    for src in sources:
        val = meta.get(f"last_error_{src}")
        # The stamp is inside the value as "<iso>: <msg>" — split on the first ": ".
        err_dt = _parse_iso(val.split(": ", 1)[0]) if val else None
        own_keys = stamps.get(src)
        own = _boundary(own_keys or ())
        # A source's presence in the map is an independent contract, even if
        # its stamp hasn't been written yet. Falling back to the recorder's
        # boundary is allowed only for a source that has no stamp contract at
        # all; otherwise a first-run failure gets declared "recovered" after
        # another process's healthy cycle.
        boundary = own if own_keys is not None else recorder_boundary
        stale = bool(val) and boundary is not None and err_dt is not None and err_dt < boundary
        out.append({
            "source": src,
            "last_error": val,
            "stale": stale,
            # Where the boundary came from: diagnosing "why is it still red?"
            # without reading meta by hand.
            "ok_at": own.isoformat() if own else None,
        })
    return out


def provider_keys(
    conn: sqlite3.Connection,
    prefix: str = "provider_keys_",
    min_keys: int = 2,
    stale_seconds: float = 2400.0,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The state of provider key pools as each process stamped it into `meta`.

    **With no key value at all** (FR-013): the writer writes only counts and
    gauges, and this function reads what was written — so there is no way to
    display a key or any fragment of one. (Account names and the last four
    characters come from a completely different route: `keystore` reads the
    file. The two routes must not be conflated — this row stays safe for
    logs, and the other one doesn't.)

    One row per (owner, provider), not per provider: a single provider can
    exist in both `FomoChain` and `FomoEVMReplay` with two independent states
    (two processes, two memories), and merging them would hide one of them
    running out of credits under the other's health. (The GoldRush pool was
    exactly this case before the provider was deleted, and today's replay
    route is keyless anyway.)

    And `stale` here is about the **report**, not the keys: a frozen report
    means the owning process didn't complete a cycle, and its counts are
    numbers from the past, not a description of the present.
    """
    moment = now or datetime.now(UTC)

    def _count(value: object) -> int:
        """A count from JSON written by another process: an older version may omit a key."""
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    def _indices(value: object, limit: int) -> list[int]:
        """Only integer indices within range.

        Another process's report may come from an older or newer version; an
        out-of-range index would have colored a row with no counterpart, or
        inflated the display. We drop it silently.
        """
        if not isinstance(value, list):
            return []
        out: list[int] = []
        for item in value:
            try:
                index = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= index < limit and index not in out:
                out.append(index)
        return sorted(out)

    out: list[dict[str, Any]] = []
    for key, raw in all_meta(conn).items():
        if not key.startswith(prefix) or not raw:
            continue
        try:
            report = json.loads(raw)
            pools = report["pools"]
        except (ValueError, TypeError, KeyError):
            continue
        owner = str(report.get("owner") or key[len(prefix):])
        at = report.get("at")
        at_dt = _parse_iso(at)
        age = (moment - at_dt).total_seconds() if at_dt else None
        stale = age is None or age > stale_seconds
        for provider, pool in sorted((pools or {}).items()):
            if not isinstance(pool, dict):
                continue
            keys = _count(pool.get("keys"))
            blocked = _count(pool.get("blocked"))
            available = _count(pool.get("available"))
            out.append({
                "owner": owner,
                "provider": str(provider),
                "keys": keys,
                "blocked": blocked,
                "available": available,
                "index": _count(pool.get("index")),
                # The cooled slots, not their count: the count says "one of
                # three rejected" without saying which, so all three get
                # colored red and the healthy one gets blamed. An index in a
                # list, no value and no length (FR-013). An older version of
                # the writer doesn't send it ⇒ an empty list, and `blocked`
                # remains the available meaning.
                "blocked_index": _indices(pool.get("blocked_index"), keys),
                "rotations": _count(pool.get("rotations")),
                "cooldown_seconds": pool.get("cooldown_seconds"),
                # A provider silenced for the rest of the process's life
                # (credits exhausted ⇒ 402, as happened to GoldRush): the
                # pools look healthy while the provider is off, so it's said
                # explicitly.
                "disabled": bool(pool.get("disabled")),
                "at": at,
                "age_seconds": age,
                "stale": stale,
                # Three levels: disabled/none available ⇒ bad, a single key or
                # one cooling right now ⇒ warn, otherwise good.
                "level": (
                    "bad" if (pool.get("disabled") or (keys and not available) or not keys)
                    else "warn" if (keys < min_keys or blocked)
                    else "good"
                ),
            })
    out.sort(key=lambda row: (row["provider"], row["owner"]))
    return out


# --- signals ---
def recent_signals(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    if not _table_exists(conn, "signal_events"):
        return []
    limit = max(1, min(limit, 500))
    rows = conn.execute(
        """SELECT id, token_address, network_id, ts, recorded_at, signal_type, ticker,
                  price_usd, fdv, market_cap, num_trades, are_top_traders,
                  top_trader_match_count, buyers_best_rank, buyer_handle,
                  num_swaps, is_first_buy, buyer_pnl_pct
           FROM signal_events
           ORDER BY recorded_at DESC, rowid DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- watchlist ---
def active_watchlist(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "watchlist"):
        return []
    # `watchlist.first_seen_at` is a lying name: `upsert_watch` overwrites it
    # on every re-acceptance after the window ends (db.py:781), so its real
    # meaning is "start of the current cycle", not "first capture". Displaying
    # it alone made a token captured 7 days ago look 22 hours old. The
    # write-once log is `watch_windows`, so we take the first capture from it
    # and display both together: true age and cycle age.
    first_ever = (
        "(SELECT MIN(ww.first_seen_at) FROM watch_windows ww "
        "  WHERE ww.token_address = w.token_address"
        "    AND ww.network_id = w.network_id) AS first_ever_at"
        if _table_exists(conn, "watch_windows")
        else "w.first_seen_at AS first_ever_at"
    )
    # The control pill on the watchlist page: control coins are watched and
    # recorded exactly like signal coins (that is the point of the comparison),
    # but the reader must be able to tell them apart at a glance.
    control_sel = (
        "w.is_control AS is_control,"
        if _has_column(conn, "watchlist", "is_control")
        else "0 AS is_control,"
    )
    rows = conn.execute(
        f"""SELECT w.token_address, w.network_id, w.source, w.first_seen_at, w.watch_until,
                   {first_ever},
                   {control_sel}
                   (SELECT ts.symbol FROM token_static ts
                      WHERE ts.token_address = w.token_address LIMIT 1) AS symbol,
                   (SELECT COUNT(*) FROM market_ticks m
                      WHERE m.token_address = w.token_address) AS tick_count
              FROM watchlist w
             WHERE w.active = 1
             ORDER BY w.first_seen_at DESC""",
    ).fetchall()
    return [dict(r) for r in rows]


# --- OHLCV bars ---
def bars_coverage(conn: sqlite3.Connection, live_start_ts: int) -> dict[str, Any]:
    """Progress of candle capture: how many watched coins actually have a price series.

    This is the decisive measure for labeling: a coin without candles can't
    have its outcome computed, so its sample is wasted. Before the token_bars
    table, a quarter of watches had no price at all.

    `live_start_ts` is mandatory with no default: the boundary lives in config
    alone, and a duplicate default here would silently drift from it. The
    candle count is restricted to the live epoch (`ts >= live`): token_bars
    carries retro price history predating the signal (466 thousand retro
    candles), which is data the live bot didn't collect, so it isn't displayed
    to keep it from mixing with bot data.
    """
    if not _table_exists(conn, "token_bars") or not _table_exists(conn, "watchlist"):
        return {"active": 0, "with_bars": 0, "pending": 0, "no_data": 0,
                "candles": 0, "coverage_pct": None}

    active = active_watch_count(conn)
    with_bars = conn.execute(
        """SELECT COUNT(*) AS n FROM watchlist w WHERE w.active = 1
             AND EXISTS (SELECT 1 FROM token_bars b
                          WHERE b.token_address = w.token_address
                            AND b.network_id = w.network_id)"""
    ).fetchone()["n"]
    # The live epoch only — retro candles (ts < live) are price history the bot didn't collect.
    candles = conn.execute(
        "SELECT COUNT(*) AS n FROM token_bars WHERE ts >= ?", (live_start_ts,)
    ).fetchone()["n"]

    no_data = 0
    if _table_exists(conn, "bars_fetch_state"):
        no_data = conn.execute(
            """SELECT COUNT(*) AS n FROM watchlist w
                 JOIN bars_fetch_state s
                   ON s.token_address = w.token_address AND s.network_id = w.network_id
                WHERE w.active = 1 AND s.last_status = 'no_data'"""
        ).fetchone()["n"]

    return {
        "active": active,
        "with_bars": with_bars,
        # The sweep hasn't reached it yet (the scan rotates over several cycles) — not a failure.
        "pending": max(0, active - with_bars - no_data),
        "no_data": no_data,
        "candles": candles,
        "coverage_pct": round(100 * with_bars / active, 1) if active else None,
    }


# --- Signal performance since entry ---
def _performance_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """For every watched coin: how its price moved **since the moment of the signal**.

    This is the question the project was built for. Entry price = the close of
    the first candle at or after `first_seen_at` (not before it — otherwise
    the future leaks in backwards). The peak and trough come from `h`/`l`
    within the same window.

    An honesty note: this is a **running window, not a final outcome** — most
    watches haven't completed 48 hours yet, and `peak_pct` only ever rises,
    never falls, over time. These are not labels.
    """
    if not _table_exists(conn, "token_bars") or not _table_exists(conn, "watchlist"):
        return []
    has_control = _has_column(conn, "watchlist", "is_control")
    control_sel = "w.is_control AS is_control," if has_control else "0 AS is_control,"
    has_windows = _table_exists(conn, "watch_windows")
    design_join = (
        "LEFT JOIN watch_windows ww ON ww.token_address=w.token_address "
        "AND ww.network_id=w.network_id AND ww.first_seen_at=w.first_seen_at"
        if has_windows else ""
    )
    design_sel = "COALESCE(ww.design_version, 1) AS design_version," if has_windows else (
        "1 AS design_version,"
    )
    # The impossible tails from upstream are excluded from the peak/trough: an
    # h of 2,626,092 was seen for a candle closing at 0.0219, and the dashboard
    # displayed +62,570,743,609%. The flag is computed at capture time
    # (h_suspect/l_suspect); here we only respect it. The raw value stays in
    # the database.
    has_flags = _has_column(conn, "token_bars", "h_suspect")
    peak_expr = ("MAX(CASE WHEN b.h_suspect = 1 THEN NULL ELSE b.h END)"
                 if has_flags else "MAX(b.h)")
    trough_expr = ("MIN(CASE WHEN b.l_suspect = 1 THEN NULL ELSE b.l END)"
                   if has_flags else "MIN(b.l)")
    # Entry/latest price from a **clean** close: the close itself is sometimes
    # corrupted (12,052.5), which would make the ratio's denominator rotten.
    clean_c = "AND c_suspect = 0" if _has_column(conn, "token_bars", "c_suspect") else ""
    t_expr = ("MIN(CASE WHEN b.c_suspect = 1 THEN NULL ELSE b.ts END)"
              if has_flags else "MIN(b.ts)")
    t1_expr = ("MAX(CASE WHEN b.c_suspect = 1 THEN NULL ELSE b.ts END)"
               if has_flags else "MAX(b.ts)")
    rows = conn.execute(
        f"""WITH w AS (
               SELECT token_address, network_id, source, first_seen_at, watch_until,
                      {"is_control," if has_control else ""}
                      CAST(strftime('%s', first_seen_at) AS INTEGER) AS entry_ts
                 FROM watchlist WHERE active = 1
           ),
           agg AS (
               SELECT w.token_address AS a, w.network_id AS n, w.source AS src,
                      w.first_seen_at AS fs, w.watch_until AS wu, w.entry_ts AS ets,
                       {control_sel}
                       {design_sel}
                      COUNT(*) AS candles, {t_expr} AS t0, {t1_expr} AS t1,
                      {peak_expr} AS peak, {trough_expr} AS trough
                 FROM w {design_join} JOIN token_bars b
                   ON b.token_address = w.token_address
                  AND b.network_id = w.network_id
                  AND b.ts >= w.entry_ts
                GROUP BY w.token_address, w.network_id
           )
           SELECT agg.*,
                  (SELECT c FROM token_bars
                    WHERE token_address = agg.a AND network_id = agg.n AND ts = agg.t0
                    {clean_c} LIMIT 1) AS entry_px,
                  (SELECT c FROM token_bars
                    WHERE token_address = agg.a AND network_id = agg.n AND ts = agg.t1
                    {clean_c} LIMIT 1) AS last_px,
                  (SELECT symbol FROM token_static
                    WHERE token_address = agg.a LIMIT 1) AS symbol
             FROM agg"""
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        entry, last, peak = r["entry_px"], r["last_px"], r["peak"]
        if not entry:  # zero or None → the ratios are undefined; we don't fake them
            continue
        out.append({
            "token_address": r["a"],
            "network_id": r["n"],
            "symbol": r["symbol"],
            "source": r["src"],
            "is_control": bool(r["is_control"]),
            "design_version": r["design_version"],
            "first_seen_at": r["fs"],
            "watch_until": r["wu"],
            "candles": r["candles"],
            "entry_px": entry,
            "last_px": last,
            "peak_px": peak,
            "trough_px": r["trough"],
            "change_pct": (last / entry - 1) * 100 if last else None,
            "peak_pct": (peak / entry - 1) * 100 if peak else None,
            # How far it has fallen from its peak now — a "you missed the sell" gauge
            "from_peak_pct": (last / peak - 1) * 100 if last and peak else None,
        })
    return out


# Allowed sort keys — a whitelist keeping any arbitrary expression out of the interface.
_SORT_KEYS = (
    "peak_pct", "change_pct", "from_peak_pct",
    "first_seen_at", "candles", "entry_px", "last_px", "symbol",
)


def watch_performance(
    conn: sqlite3.Connection,
    limit: int = 12,
    sort_key: str = "peak_pct",
    descending: bool = True,
) -> list[dict[str, Any]]:
    """The best/worst coins by the chosen key.

    Sorting happens here over the **full set** before truncation. If it were
    sorted in the browser after truncating to the first 12, descending order
    would have given "the top 12 inverted" instead of the actual worst — a
    silent mistake that looks correct.
    """
    # The table is titled "signal performance" — the control group is a
    # reference for comparison, not rows in it.
    rows = [r for r in _performance_rows(conn) if not r.get("is_control")]
    key = sort_key if sort_key in _SORT_KEYS else "peak_pct"

    def _sort_value(r: dict[str, Any]) -> Any:
        v = r.get(key)
        if isinstance(v, str):
            return v.lower()
        return v

    # The missing ones stay at the tail in both directions — they don't
    # meaninglessly top an ascending sort.
    present = [r for r in rows if _sort_value(r) is not None]
    missing = [r for r in rows if _sort_value(r) is None]
    present.sort(key=_sort_value, reverse=descending)
    return (present + missing)[: max(1, limit)]


def group_comparison(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compares **signal** coins with **control-group** coins on the same measures.

    This is the question the rest of the dashboard cannot answer: not "which
    signaled coin rises the most" but **"does the signal mean anything at
    all"**. Without a control group you might find 41% of your signals are
    winners and then discover 41% of the market was winning in that period.

    `delta` = the signal's edge over the control. Positive = the signal wins.
    `sufficient` = whether the two sample sizes are big enough to take the
    difference seriously (no statistical test here; a raw threshold keeps
    noise from being read as a result).
    """
    rows = [
        r for r in _performance_rows(conn)
        if r.get("change_pct") is not None and r.get("design_version", 1) >= 2
    ]
    signal = [r for r in rows if not r.get("is_control")]
    control = [r for r in rows if r.get("is_control")]

    def _stats(group: list[dict[str, Any]]) -> dict[str, Any]:
        if not group:
            return {"count": 0, "win_rate_pct": None, "avg_pct": None,
                    "median_pct": None, "avg_peak_pct": None}
        ch = sorted(r["change_pct"] for r in group)
        peaks = [r["peak_pct"] for r in group if r.get("peak_pct") is not None]
        n = len(ch)
        return {
            "count": n,
            "win_rate_pct": sum(1 for c in ch if c > 0) / n * 100,
            "avg_pct": sum(ch) / n,
            "median_pct": ch[n // 2] if n % 2 else (ch[n // 2 - 1] + ch[n // 2]) / 2,
            "avg_peak_pct": sum(peaks) / len(peaks) if peaks else None,
        }

    sig, ctl = _stats(signal), _stats(control)
    delta = {
        k: (sig[k] - ctl[k])
        if sig.get(k) is not None and ctl.get(k) is not None else None
        for k in ("win_rate_pct", "avg_pct", "median_pct", "avg_peak_pct")
    }
    # A raw threshold: below 20 per side the difference is most likely noise.
    return {"signal": sig, "control": ctl, "delta": delta,
            "sufficient": sig["count"] >= 20 and ctl["count"] >= 20}


def performance_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """The profit-and-loss tally across **all** watched coins, not the displayed slice.

    `change_pct` (price now versus entry) is the basis — not `peak_pct`,
    because a peak is only realized by selling at that exact moment. The
    average assumes equal weight per signal.

    A deliberate honesty warning in `is_open`: these are **open positions in
    a running window**, not realized outcomes — no signal has completed 48
    hours yet. The median is shown next to the mean because a single +789%
    winner drags the mean by itself.
    """
    # The tally is about **signal** coins; the control group is a reference
    # for comparison, not part of the performance.
    rows = [
        r for r in _performance_rows(conn)
        if r.get("change_pct") is not None and not r.get("is_control")
    ]
    if not rows:
        return {"count": 0, "winners": 0, "losers": 0, "flat": 0, "is_open": True,
                "avg_pct": None, "median_pct": None, "gross_gain_pct": None,
                "gross_loss_pct": None, "net_pct": None, "best": None, "worst": None,
                "win_rate_pct": None}

    changes = sorted(r["change_pct"] for r in rows)
    n = len(changes)
    gains = [c for c in changes if c > 0]
    losses = [c for c in changes if c < 0]
    median = (
        changes[n // 2] if n % 2 else (changes[n // 2 - 1] + changes[n // 2]) / 2
    )
    best = max(rows, key=lambda r: r["change_pct"])
    worst = min(rows, key=lambda r: r["change_pct"])

    def _brief(r: dict[str, Any]) -> dict[str, Any]:
        # network_id is included so the client can build the coin's page link on fomo
        return {
            "symbol": r.get("symbol"),
            "token_address": r["token_address"],
            "network_id": r.get("network_id"),
            "change_pct": r["change_pct"],
        }

    return {
        "count": n,
        "winners": len(gains),
        "losers": len(losses),
        "flat": n - len(gains) - len(losses),
        "win_rate_pct": len(gains) / n * 100,
        "avg_pct": sum(changes) / n,
        "median_pct": median,
        "gross_gain_pct": sum(gains),
        "gross_loss_pct": sum(losses),        # negative
        "net_pct": sum(changes),
        "best": _brief(best),
        "worst": _brief(worst),
        # A running window, not realized outcomes — the UI says this explicitly
        "is_open": True,
    }


def token_series(
    conn: sqlite3.Connection, token_address: str, network_id: str,
    since_ts: int, points: int = 40,
) -> list[float]:
    """A downsampled close series for drawing a sparkline. Fewer than two points → []."""
    if not _table_exists(conn, "token_bars"):
        return []
    rows = conn.execute(
        """SELECT c FROM token_bars
            WHERE token_address = ? AND network_id = ? AND ts >= ? AND c IS NOT NULL
            ORDER BY ts""",
        (token_address, network_id, since_ts),
    ).fetchall()
    closes = [float(r["c"]) for r in rows]
    if len(closes) < 2:
        return []
    if len(closes) <= points:
        return closes
    # Downsample by a fixed step, guaranteeing the last point stays (the current price).
    step = len(closes) / points
    sampled = [closes[int(i * step)] for i in range(points)]
    sampled[-1] = closes[-1]
    return sampled


# Sparkline buckets per watch: 40 points over the 48-hour window — the same
# resolution `token_series` gives the performance page, without its per-token
# query.
_SPARK_POINTS = 40
_WINDOW_SECONDS = 48 * 3600


def watchlist_market(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Market data for the active watches, keyed `"{address}|{network}"`.

    One dict feeds the whole watchlist row: the movement numbers come from
    `_performance_rows` (same math and same suspect-close guards as the
    performance page — one definition of "since entry", not two), and the
    sparkline comes from a **single** bucketed pass over the live bars.
    Calling `token_series` per coin instead would mean one full candle scan
    per coin per refresh; the GROUP BY walks the same rows once for all coins.

    Coins without a clean entry price yet (the candle sweep hasn't reached
    them) are absent from the map, exactly as they are absent from
    /api/performance — the watchlist row still renders, with dashes.

    Control coins are included: the watchlist page has always shown every
    active watch, and the row carries the flag so the UI can badge it.
    The caller caches this (60 s) — the entry/peak numbers are glance
    material, and the 48-hour countdown itself stays live via /api/watchlist.
    """
    market: dict[str, dict[str, Any]] = {
        f"{r['token_address']}|{r['network_id']}": r for r in _performance_rows(conn)
    }

    if not _table_exists(conn, "token_bars") or not _table_exists(conn, "watchlist"):
        return {k: {**v, "spark": []} for k, v in market.items()}

    # Same guard as `_performance_rows`: a suspect close must not bend the
    # sparkline either. When the column is absent the CASE collapses to `b.c`.
    clean_c = "CASE WHEN b.c_suspect = 1 THEN NULL ELSE b.c END" \
        if _has_column(conn, "token_bars", "c_suspect") else "b.c"
    rows = conn.execute(
        f"""WITH w AS (
               SELECT token_address, network_id,
                      CAST(strftime('%s', first_seen_at) AS INTEGER) AS entry_ts
                 FROM watchlist WHERE active = 1
           )
           SELECT w.token_address AS a, w.network_id AS n,
                  MIN({_SPARK_POINTS - 1},
                      (b.ts - w.entry_ts) * {_SPARK_POINTS} / {_WINDOW_SECONDS}) AS bucket,
                  AVG({clean_c}) AS px
             FROM w JOIN token_bars b
               ON b.token_address = w.token_address AND b.network_id = w.network_id
              AND b.ts >= w.entry_ts AND b.c IS NOT NULL
            GROUP BY a, n, bucket
            ORDER BY a, n, bucket"""
    ).fetchall()

    sparks: dict[str, list[float]] = {}
    for r in rows:
        if r["px"] is None:
            continue
        sparks.setdefault(f"{r['a']}|{r['n']}", []).append(float(r["px"]))
    for key, row in market.items():
        row["spark"] = sparks.get(key, [])
    return market


# --- Signal flow over time ---
def signal_timeline(conn: sqlite3.Connection, hours: int = 24) -> dict[str, Any]:
    """The number of signals per hour split by type — for a stacked column.

    We return every hour in the range including empty ones, otherwise the
    chart would look continuous across a gap where the recorder was down.
    """
    if not _table_exists(conn, "signal_events"):
        return {"hours": [], "types": [], "series": {}}
    hours = max(1, min(hours, 168))
    rows = conn.execute(
        """SELECT strftime('%Y-%m-%dT%H:00:00', recorded_at) AS hour,
                  signal_type, COUNT(*) AS n
             FROM signal_events
            WHERE recorded_at >= datetime('now', ?)
            GROUP BY hour, signal_type""",
        (f"-{hours} hours",),
    ).fetchall()
    if not rows:
        return {"hours": [], "types": [], "series": {}}

    counts: dict[str, dict[str, int]] = {}
    types: set[str] = set()
    for r in rows:
        counts.setdefault(r["hour"], {})[r["signal_type"]] = r["n"]
        types.add(r["signal_type"])

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    buckets = [
        (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00")
        for h in range(hours - 1, -1, -1)
    ]
    # A fixed type order: the color follows the type, not its rank (otherwise
    # the colors would shift with filtering)
    ordered = [t for t in _SIGNAL_TYPE_ORDER if t in types]
    ordered += sorted(t for t in types if t not in _SIGNAL_TYPE_ORDER)
    return {
        "hours": buckets,
        "types": ordered,
        "series": {t: [counts.get(b, {}).get(t, 0) for b in buckets] for t in ordered},
    }


# A fixed order guaranteeing every signal type keeps its color no matter how the data changes.
_SIGNAL_TYPE_ORDER = ("multi_user_buy", "large_buy", "multi_user_sell", "large_sell")


# --- storage ---
def storage_stats(
    db_path: str,
    conn: sqlite3.Connection,
    *,
    backup_dir: str | None = None,
    backup_max_age_hours: float = 36.0,
    disk_free_warn_bytes: int = 25 * 1024**3,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The database's size on disk + the estimated daily growth rate.

    Added because unlimited growth stayed hidden for 20 hours until the
    database reached 720 MB: the dashboard used to show row counts, not
    sizes. The rate is estimated from (size / archive age) because all rows
    arrive at a fixed rate (one cycle per minute).
    """
    total_bytes = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total_bytes += os.path.getsize(db_path + suffix)
        except OSError:
            pass

    # The archive's age from the oldest snapshot — not from started_at (which
    # is reset on every run).
    span_days = None
    if _table_exists(conn, "snapshots"):
        row = conn.execute(
            "SELECT MIN(recorded_at) AS lo, MAX(recorded_at) AS hi FROM snapshots"
        ).fetchone()
        lo, hi = _parse_iso(row["lo"]), _parse_iso(row["hi"])
        if lo and hi and hi > lo:
            span_days = (hi - lo).total_seconds() / 86400

    meta = all_meta(conn)
    free_bytes = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free

    latest_backup = None
    backup_age_hours = None
    if backup_dir:
        backup_path = os.path.abspath(os.path.expanduser(backup_dir))
        try:
            candidates = [
                path for path in (
                    os.path.join(backup_path, name)
                    for name in os.listdir(backup_path)
                    if name.startswith("recorder-") and name.endswith(".db")
                )
                if os.path.isfile(path)
            ]
        except OSError:
            candidates = []
        if candidates:
            latest_backup = max(candidates, key=os.path.getmtime)
            backup_dt = datetime.fromtimestamp(os.path.getmtime(latest_backup), UTC)
            backup_age_hours = max(
                0.0, ((now or datetime.now(UTC)) - backup_dt).total_seconds() / 3600
            )

    return {
        "bytes": total_bytes,
        "mb": round(total_bytes / 1e6, 1),
        "span_days": round(span_days, 2) if span_days else None,
        "mb_per_day": round(total_bytes / 1e6 / span_days, 1) if span_days else None,
        "raw_encoding": meta.get("raw_encoding", "plain"),
        "disk_free_bytes": free_bytes,
        "disk_free_gb": round(free_bytes / 1024**3, 1),
        "disk_warning": free_bytes < disk_free_warn_bytes,
        "backup_configured": bool(backup_dir),
        "backup_dir": os.path.abspath(os.path.expanduser(backup_dir)) if backup_dir else None,
        "latest_backup": os.path.basename(latest_backup) if latest_backup else None,
        "backup_age_hours": round(backup_age_hours, 1) if backup_age_hours is not None else None,
        "backup_warning": bool(
            backup_dir
            and (backup_age_hours is None or backup_age_hours > backup_max_age_hours)
        ),
    }


# --- market ticks summary ---
def ticks_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """The total count + the latest market snapshot per active watched coin."""
    if not _table_exists(conn, "market_ticks"):
        return {"total": 0, "per_token": []}
    total = conn.execute("SELECT COUNT(*) AS n FROM market_ticks").fetchone()["n"]
    # The last tick per active coin (newest recorded_at).
    rows = conn.execute(
        """SELECT m.token_address, m.recorded_at, m.price_usd, m.holders,
                  m.change_24h, m.volume_24h, m.buy_count_24h, m.sell_count_24h
           FROM market_ticks m
           JOIN watchlist w
             ON w.token_address = m.token_address AND w.active = 1
           JOIN (SELECT token_address, MAX(recorded_at) AS mx
                   FROM market_ticks GROUP BY token_address) last
             ON last.token_address = m.token_address AND last.mx = m.recorded_at
           GROUP BY m.token_address
           ORDER BY m.volume_24h DESC NULLS LAST""",
    ).fetchall()
    return {"total": total, "per_token": [dict(r) for r in rows]}


# --- network coverage ---
def latest_tick_per_active_network(conn: sqlite3.Connection) -> dict[str, str]:
    """The latest market snapshot per network with an active watch — by seeks, not a scan.

    `network_summary` computes this stamp inside an aggregation that scans the
    whole snapshots table (measured at 1780 ms over three million rows)
    because it groups by `network_id` and no index starts with it. Here we
    reverse the direction: we walk the active coins (184) and ask for the
    latest stamp of each, so `(token_address, network_id)` matches the start
    of the primary key ⇒ a single seek to the edge of its range. Measured:
    **0.4 ms**, with an index that already exists and nothing new built.

    And the only difference is that it's blind to a network with no active
    watch — which is why it isn't used as a replacement for the summary but
    as a layer on top of it (`with_live_latest_tick`): the cached value
    carries all the networks and the live one refreshes the freshness of the
    active ones.
    """
    if not (_table_exists(conn, "watchlist") and _table_exists(conn, "market_ticks")):
        return {}
    rows = conn.execute(
        """SELECT w.network_id AS network_id,
                  MAX((SELECT MAX(m.recorded_at) FROM market_ticks m
                        WHERE m.token_address = w.token_address
                          AND m.network_id = w.network_id)) AS latest_tick
             FROM (SELECT DISTINCT token_address, network_id
                     FROM watchlist
                    WHERE active = 1
                      AND network_id IS NOT NULL AND network_id != '') w
            GROUP BY w.network_id""",
    ).fetchall()
    return {
        str(row["network_id"]): row["latest_tick"]
        for row in rows
        if row["latest_tick"]
    }


def with_live_latest_tick(
    rows: list[dict[str, Any]], live: dict[str, str],
) -> list[dict[str, Any]]:
    """Copies the summary rows and raises `latest_tick` to the newer of the cached and live values.

    **Copies, doesn't mutate**: the incoming rows may be in a cache shared by
    concurrent requests, so editing them in place would corrupt them for
    whoever reads them at the same moment.

    And `max`, not replacement: the live view is blind to a coin whose watch
    ended after its last snapshot, so if that coin held the newest stamp in
    its network, replacing would be a **regression** in freshness — and a
    number that goes backwards in the dashboard reads as the data moving back
    in time. The stamps are ISO with the same offset (+00:00) from a single
    writer, so their lexicographic order is their chronological order.
    """
    merged: list[dict[str, Any]] = []
    for row in rows:
        copy = dict(row)
        fresh = live.get(str(copy.get("network_id")))
        if fresh:
            stored = copy.get("latest_tick")
            copy["latest_tick"] = max(stored, fresh) if stored else fresh
        merged.append(copy)
    return merged


def network_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """A coverage summary per network for live data and on-chain measurements.

    A structurally heavy query: the `ticks` node groups by `network_id` and no
    index starts with it, so it scans the whole table (1724 of 1880 measured
    ms). It can't be fixed without a new index — which would mean touching
    the database — so its output is cached in `cache.MEMO` with a fixed TTL,
    and its freshness is lifted on top from `latest_tick_per_active_network`.

    And the `ticks` node isn't replaced with the cheap alternative: it also
    contributes to the **network list** itself
    (`networks = active UNION ticks`), so a network with snapshots but no
    active watch appears thanks to it alone — replacing it would have hidden
    it from the dashboard without anyone saying a thing.
    """
    if not _table_exists(conn, "watchlist"):
        return []

    has_concentration = _table_exists(conn, "chain_concentration")
    concentration_columns = {
        column: _has_column(conn, "chain_concentration", column)
        for column in ("top1_pct", "top5_pct", "top10_pct", "top20_pct", "holder_count")
    }
    has_ticks = _table_exists(conn, "market_ticks")
    empty_ticks = "SELECT CAST(NULL AS TEXT) AS network_id, 0 AS tick_rows, NULL AS latest_tick WHERE 0"
    ticks_cte = (
        "SELECT network_id, COUNT(*) AS tick_rows, MAX(recorded_at) AS latest_tick "
        "FROM market_ticks WHERE network_id IS NOT NULL AND network_id != '' GROUP BY network_id"
        if has_ticks else empty_ticks
    )
    has_details = _table_exists(conn, "token_holders") and all(
        _has_column(conn, "token_holders", column)
        for column in (
            "token_address", "network_id", "recorded_at", "source",
            "top10_pct", "holder_count",
        )
    )
    if has_details:
        details_cte = """SELECT h.token_address, h.network_id, h.recorded_at,
                                h.top10_pct, h.holder_count
                           FROM token_holders h
                           JOIN (SELECT token_address, network_id,
                                        MAX(recorded_at) AS recorded_at
                                   FROM token_holders
                                  WHERE source='token_details'
                                  GROUP BY token_address, network_id) latest
                             ON latest.token_address = h.token_address
                            AND latest.network_id = h.network_id
                            AND latest.recorded_at = h.recorded_at
                          WHERE h.source='token_details'"""
    else:
        details_cte = """SELECT CAST(NULL AS TEXT) AS token_address,
                                 CAST(NULL AS TEXT) AS network_id,
                                 NULL AS recorded_at, NULL AS top10_pct,
                                 NULL AS holder_count
                            WHERE 0"""

    # Use only the newest snapshot for each currently active token. Counting all
    # historical rows made a single token with a long replay history look like
    # thousands of covered tokens.
    if has_concentration:
        fields = ", ".join(
            f"c.{column} AS {column}" if available else f"NULL AS {column}"
            for column, available in concentration_columns.items()
        )
        concentration_cte = f"""SELECT c.token_address, c.network_id, c.recorded_at, {fields}
                                  FROM chain_concentration c
                                  JOIN (SELECT token_address, network_id, MAX(recorded_at) AS recorded_at
                                          FROM chain_concentration
                                         GROUP BY token_address, network_id) latest
                                    ON latest.token_address = c.token_address
                                   AND latest.network_id = c.network_id
                                   AND latest.recorded_at = c.recorded_at"""
    else:
        concentration_cte = """SELECT CAST(NULL AS TEXT) AS token_address,
                                      CAST(NULL AS TEXT) AS network_id,
                                      NULL AS recorded_at,
                                      NULL AS top1_pct, NULL AS top5_pct,
                                      NULL AS top10_pct, NULL AS top20_pct,
                                      NULL AS holder_count
                                 WHERE 0"""

    rows = conn.execute(
        f"""WITH active_tokens AS (
                 SELECT DISTINCT token_address, network_id
                   FROM watchlist
                  WHERE active=1 AND network_id IS NOT NULL AND network_id != ''
              ), active AS (
                  SELECT network_id, COUNT(*) AS active_watches
                    FROM active_tokens GROUP BY network_id
              ), historical_tokens AS (
                  SELECT DISTINCT token_address, network_id
                    FROM watchlist
                   WHERE active=0 AND network_id IS NOT NULL AND network_id != ''
              ), historical AS (
                  SELECT network_id, COUNT(*) AS historical_watches
                    FROM historical_tokens GROUP BY network_id
              ), latest_concentration AS (
                  {concentration_cte}
              ), conc AS (
                 SELECT a.network_id,
                        COUNT(lc.token_address) AS concentration_rows,
                        COUNT(lc.top1_pct) AS top1_rows,
                        COUNT(lc.top5_pct) AS top5_rows,
                        COUNT(lc.top10_pct) AS top10_rows,
                        COUNT(lc.top20_pct) AS top20_rows,
                        COUNT(lc.holder_count) AS holder_count_rows,
                        MAX(lc.recorded_at) AS latest_concentration
                   FROM active_tokens a
                   LEFT JOIN latest_concentration lc
                     ON lc.token_address = a.token_address
                    AND lc.network_id = a.network_id
                   GROUP BY a.network_id
              ), historical_conc AS (
                  SELECT h.network_id,
                         COUNT(lc.token_address) AS historical_concentration_rows,
                         COUNT(lc.top1_pct) AS historical_top1_rows,
                         COUNT(lc.top5_pct) AS historical_top5_rows,
                         COUNT(lc.top10_pct) AS historical_top10_rows,
                         COUNT(lc.top20_pct) AS historical_top20_rows
                    FROM historical_tokens h
                    LEFT JOIN latest_concentration lc
                      ON lc.token_address = h.token_address
                     AND lc.network_id = h.network_id
                   GROUP BY h.network_id
              ), latest_details AS (
                 {details_cte}
             ), details AS (
                 SELECT a.network_id,
                        COUNT(ld.holder_count) AS details_holder_rows,
                        COUNT(ld.top10_pct) AS details_top10_rows,
                        MAX(ld.recorded_at) AS latest_details
                   FROM active_tokens a
                   LEFT JOIN latest_details ld
                     ON ld.token_address = a.token_address
                    AND ld.network_id = a.network_id
                  GROUP BY a.network_id
             ), ticks AS (
                 {ticks_cte}
              ), networks AS (
                  SELECT network_id FROM active
                  UNION SELECT network_id FROM historical
                  UNION SELECT network_id FROM ticks
              )
              SELECT networks.network_id,
                     COALESCE(active.active_watches, 0) AS active_watches,
                     COALESCE(historical.historical_watches, 0) AS historical_watches,
                     COALESCE(conc.concentration_rows, 0) AS concentration_rows,
                    COALESCE(conc.top1_rows, 0) AS top1_rows,
                    COALESCE(conc.top5_rows, 0) AS top5_rows,
                    COALESCE(conc.top10_rows, 0) AS top10_rows,
                     COALESCE(conc.top20_rows, 0) AS top20_rows,
                     COALESCE(historical_conc.historical_concentration_rows, 0)
                         AS historical_concentration_rows,
                     COALESCE(historical_conc.historical_top1_rows, 0) AS historical_top1_rows,
                     COALESCE(historical_conc.historical_top5_rows, 0) AS historical_top5_rows,
                     COALESCE(historical_conc.historical_top10_rows, 0) AS historical_top10_rows,
                     COALESCE(historical_conc.historical_top20_rows, 0) AS historical_top20_rows,
                    COALESCE(conc.holder_count_rows, 0) AS holder_count_rows,
                    COALESCE(details.details_holder_rows, 0) AS details_holder_rows,
                    COALESCE(details.details_top10_rows, 0) AS details_top10_rows,
                    COALESCE(ticks.tick_rows, 0) AS tick_rows,
                    conc.latest_concentration, details.latest_details,
                    ticks.latest_tick
               FROM networks
                LEFT JOIN active ON active.network_id = networks.network_id
                LEFT JOIN historical ON historical.network_id = networks.network_id
                LEFT JOIN conc ON conc.network_id = networks.network_id
                LEFT JOIN historical_conc ON historical_conc.network_id = networks.network_id
               LEFT JOIN details ON details.network_id = networks.network_id
               LEFT JOIN ticks ON ticks.network_id = networks.network_id
              ORDER BY active_watches DESC, networks.network_id"""
    ).fetchall()
    return [dict(row) for row in rows]
