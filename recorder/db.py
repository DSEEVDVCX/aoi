"""Database layer for the historical data recorder.

A thin wrapper around sqlite3: schema creation, idempotent inserts (INSERT OR IGNORE /
UPSERT depending on the table), and watchlist reads. No network logic here — fully
testable with a temporary database.

Principle: every row always carries the full raw_json. Extracted fields serve fast
queries; the raw serves the complete truth and re-derivation.

**Raw compression**: raw is stored zlib-compressed (BLOB), not as text. Without
compression the database grew ~1.3 GB daily (`snapshots` alone 577 MB over 20 hours)
because a full trending/verified/feed snapshot is written every minute. Compression is
lossless (~4.7x ratio), so not a byte of the archive is lost — see `decode_raw` for
reading. Old rows written as text remain readable (decode_raw accepts both types).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

# zlib compression level: 6 is the best trade-off measured on live data
# (4.7x at 5.6ms/snapshot vs 5.9x at 49ms for lzma).
_ZLIB_LEVEL = 6
# The standard zlib prefix (0x78) — how we tell compressed raw apart from legacy text.
_ZLIB_MAGIC = 0x78
_EVM_NETWORK_IDS = frozenset({"56", "143", "4663", "8453"})
_EVM_NETWORK_IDS_SQL = "'56', '143', '4663', '8453'"


class StaleEVMState(RuntimeError):
    """EVM ledger state changed during a network call; its stale answer must not be committed."""


def utcnow_iso() -> str:
    """Current ISO-8601 UTC time — the recorder's unified timestamp."""
    return datetime.now(UTC).isoformat()


def encode_raw(raw: Any) -> bytes:
    """Any object (or ready JSON text) → a compressed BLOB for storage in the raw_json column."""
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    # Some upstream comments carry a lone UTF-16 surrogate such as `\ud83d`. This is not
    # valid UTF-8, but representing it as a JSON escape preserves the raw instead of dropping the token snapshot.
    return zlib.compress(text.encode("utf-8", errors="backslashreplace"), _ZLIB_LEVEL)


def decode_raw(value: Any) -> Any:
    """The raw_json column → the original object. Accepts new rows (compressed BLOB)
    and old ones (plain JSON text) alike, so the existing archive is never broken."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        if data[:1] and data[0] == _ZLIB_MAGIC:
            data = zlib.decompress(data)
        return json.loads(data.decode("utf-8"))
    return json.loads(value)


_NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]+")


def english_note(text: str | None) -> str | None:
    """An error note with every non-ASCII run masked: `boom [non-english 17]`.

    Upstream answers sometimes arrive in Arabic, and an exception carrying one
    used to store it verbatim. Measured 2026-09-06: ten backfill retry rows,
    seven replay rows and one dashboard `last_error_*` line were unreadable —
    and unshowable, since every tool around this database is ASCII English.
    The mask keeps the readable part and records the length of what was
    dropped, so a masked note still says what happened and how much was lost.
    """
    if text is None:
        return None
    return _NON_ASCII_RUN.sub(
        lambda run: f"[non-english {len(run.group(0))}]", text,
    )


class RecorderDB:
    """A single SQLite connection for the recorder (single-threaded loop, so no connection pool)."""

    def __init__(self, db_path: str, schema_path: str) -> None:
        # timeout=30: two writers are active now (recorder + the FomoLabeler labeler);
        # WAL serializes writes, but Python's default timeout (5s) can be too tight
        # under contention and throw "database is locked" needlessly.
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, timeout=30)
        # WAL size cap after checkpoint (256MB): without this cap the file grew
        # to ~5GB under parallel writers (recorder+labeler+EVM) and re-raised
        # "database is locked" errors on long readers. Measured 2026-08-24.
        self._conn.execute("PRAGMA journal_size_limit=268435456")
        self._conn.row_factory = sqlite3.Row
        self._batching = False
        self._savepoint_counter = 0
        self._apply_schema(schema_path)

    def _apply_schema(self, schema_path: str) -> None:
        # Migration **before** the schema: schema.sql creates an index on is_control,
        # which fails on an old table that does not have the column yet. On a fresh
        # database the migration is a no-op (no tables yet), so the order is safe either way.
        # Several tasks start together after login; one of them may hold the write lock
        # during schema setup. We retry only on a transient lock, and never hide schema errors.
        delay = 0.25
        for attempt in range(8):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 2.0)
        try:
            self._migrate()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        with open(schema_path, encoding="utf-8") as fh:
            script = fh.read()
        # Seven processes open the database now, and their startups can coincide (a task
        # restart, or a Windows login). The schema contains `DROP VIEW IF EXISTS v; CREATE VIEW v`
        # because a view must be rebuilt to see new columns — and this pair is not
        # atomic across two processes: A drops, then B drops, then A creates, so B's
        # create fails with "view ... already exists". It actually happened: FomoBuildRows
        # died at startup and stayed dead 13 hours (2026-08-13, 02:09 → 15:12) with no
        # line in its periodic log — the failure was in the boot log alone. The window is
        # a fraction of a second, so a retry suffices: the neighbour will have finished.
        for attempt in range(3):
            try:
                self._conn.executescript(script)
                break
            except sqlite3.OperationalError as exc:
                if "already exists" not in str(exc) or attempt == 2:
                    raise
                time.sleep(0.4 * (attempt + 1))
        self._backfill_watch_windows()
        self._backfill_age_observed_at()
        self._quarantine_legacy_outcomes()
        self._set_current_feature_version()
        self._conn.commit()

    def _set_current_feature_version(self) -> None:
        """Keep the model view aligned with the feature builder's current release."""
        from features import FEATURE_VERSION

        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('current_feature_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(FEATURE_VERSION),),
        )

    def _backfill_age_observed_at(self) -> None:
        """Recover when the stored creation timestamp was actually observable."""
        from features import epoch_of

        rows = self._conn.execute(
            """SELECT token_address, network_id, recorded_at,
                      token_created_at, raw_json
                 FROM token_static
                WHERE token_created_at IS NOT NULL
                  AND token_created_at <> ''
                  AND token_created_at_observed_at IS NULL"""
        ).fetchall()
        for row in rows:
            observed_at = None
            try:
                raw = decode_raw(row["raw_json"])
            except (OSError, TypeError, UnicodeError, ValueError, zlib.error):
                raw = None
            if isinstance(raw, Mapping):
                token = raw.get("token")
                token = token if isinstance(token, Mapping) else {}
                raw_created = token.get("createdAt") or raw.get("createdAt")
                if (
                    epoch_of(raw_created) is not None
                    and epoch_of(raw_created) == epoch_of(row["token_created_at"])
                ):
                    observed_at = row["recorded_at"]
            if observed_at is None:
                state = self._conn.execute(
                    """SELECT last_lookup_at FROM token_age_lookup_state
                        WHERE token_address=? AND network_id=?
                          AND last_status='ok'""",
                    (row["token_address"], row["network_id"]),
                ).fetchone()
                if state is not None:
                    observed_at = state["last_lookup_at"]
            if observed_at is not None:
                self._conn.execute(
                    """UPDATE token_static SET token_created_at_observed_at=?
                        WHERE token_address=? AND network_id=?""",
                    (observed_at, row["token_address"], row["network_id"]),
                )

    def _quarantine_legacy_outcomes(self) -> None:
        """Quarantines every watch outcome older than the current comparison design."""
        from config import CONTROL_DESIGN_VERSION

        self._conn.execute(
            """UPDATE outcomes
                  SET analysis_eligible=0,
                      exclusion_reason='superseded_comparison_design'
                WHERE kind='watch'
                  AND (design_version IS NULL OR design_version < ?)""",
            (CONTROL_DESIGN_VERSION,),
        )

    def _backfill_watch_windows(self) -> None:
        """Preserves only the legacy signal windows before the log becomes immutable.

        The v1 control cohort was deleted by project decision and must never be
        re-created from an old watchlist; only non-control signal windows are migrated, for historical operation.
        """
        self._conn.execute(
            """INSERT OR IGNORE INTO watch_windows(
                   token_address, network_id, first_seen_at, source,
                   watch_until, entry_signal_id, is_control, admission_price_usd,
                   design_version
               )
               SELECT token_address, network_id, first_seen_at, source,
                      watch_until, entry_signal_id, is_control, NULL, 1
                 FROM watchlist
                WHERE is_control=0"""
        )

    def _migrate(self) -> None:
        """Adds missing columns to an existing database.

        `CREATE TABLE IF NOT EXISTS` never touches an existing table, so a column added
        to schema.sql never appears in a database created before it — queries break in
        production while passing on a fresh test database. The check here is idempotent,
        and a missing table is skipped (`if cols`) so the schema handles it shortly.
        """
        for table, column, ddl in _COLUMN_MIGRATIONS:
            cols = {
                r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if cols and column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

        # outcomes v2: the old key (token, entry_ts) inevitably collides (201 signals
        # for a single token). The table is empty by design, so dropping it is safe; if
        # it unexpectedly holds data we keep it under the legacy name instead of destroying it.
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(outcomes)")}
        if cols and "kind" not in cols:
            n = self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
            if n == 0:
                self._conn.execute("DROP TABLE outcomes")
            else:  # pragma: no cover - not expected to happen
                self._conn.execute("ALTER TABLE outcomes RENAME TO outcomes_legacy")

    def _commit(self) -> None:
        """Commits immediately, except inside batch() where the commit is deferred to the end of the batch."""
        if not self._batching:
            self._conn.commit()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Bundles many inserts into a single commit (one fsync instead of dozens).

        The loop writes ~65 ticks per cycle; committing each row separately meant ~70
        fsyncs per minute. An error on exit rolls the whole batch back (rollback) — hence
        we use it only around insert loops, never around error-bookkeeping meta writes.
        """
        if self._batching:
            self._savepoint_counter += 1
            savepoint = f"batch_{self._savepoint_counter}"
            self._conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            return
        owns_transaction = not self._conn.in_transaction
        savepoint: str | None = None
        self._batching = True
        try:
            # Claim the writer before any state check inside the batch. Starting the
            # transaction at the first INSERT leaves a window between the SELECT and
            # the write where reset or another worker can move the cursor, applying the same records twice.
            if owns_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            else:
                self._savepoint_counter += 1
                savepoint = f"batch_{self._savepoint_counter}"
                self._conn.execute(f"SAVEPOINT {savepoint}")
            yield
            if owns_transaction:
                self._conn.commit()
            else:
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except BaseException:
            if owns_transaction:
                self._conn.rollback()
            else:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        finally:
            self._batching = False

    def close(self) -> None:
        self._conn.close()

    def recover_connection(self) -> str:
        """Rescues a connection that got stuck and now refuses every write with `database is locked`.

        The connection is single and lives as long as the process (`__init__` opens
        one), so a fault in its state lasts until restart. And the most dangerous
        thing that sticks is a WAL read snapshot: once writers pass our connection's
        snapshot, SQLite answers every write from it with `SQLITE_BUSY_SNAPSHOT`
        — and its Python message is the same `database is locked`, **and the timeout
        does not help**: no busy handler is invoked for this state, so the thirty
        seconds expire as they began and the connection stays helpless forever, with no visible cause.

        It actually happened: 2026-08-19, collection was down 22 minutes 40 seconds
        (14:10:11 → 14:32:51 UTC), every cycle crashing at `set_meta`, while a fresh
        connection took the write lock in 0.09 seconds — the lock was free and the
        stuck one was ours. It began 42 seconds before the `VACUUM INTO` that
        `FomoBackup` runs over 16.7 GB. And the cycle shield kept logging and completing, so only a manual restart released it.

        Three tiers, stopping at the first that suffices: `rollback` if a transaction
        is open, then a `BEGIN IMMEDIATE`/`ROLLBACK` probe that proves writes work
        again, and otherwise a new connection. And no `_apply_schema`: migration and
        schema are heavy boot work and this is not their place — the database already stands with its schema.

        Returns a short description for the log and never raises anything: it is
        called from a crash path, and the rescue hand must not take down the loop it came to save.
        """
        steps: list[str] = []
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
                steps.append("rollback")
        except Exception as exc:  # noqa: BLE001 — a rescue hand must not take down the loop
            steps.append(f"rollback failed: {type(exc).__name__}")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("ROLLBACK")
            return "+".join(steps) or "ok"
        except Exception as exc:  # noqa: BLE001
            steps.append(f"probe failed: {type(exc).__name__}")
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 — it may already be dead
            pass
        try:
            self._conn = sqlite3.connect(self._db_path, timeout=30)
            self._conn.execute("PRAGMA journal_size_limit=268435456")
            self._conn.row_factory = sqlite3.Row
            self._batching = False
            steps.append("reconnected")
        except Exception as exc:  # noqa: BLE001
            steps.append(f"reconnect failed: {type(exc).__name__}")
        return "+".join(steps)

    # --- meta ---
    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def note_error(self, key: str, value: str) -> bool:
        """A bookkeeping stamp that must never take down its writer. Returns True if written.

        Plain `set_meta` is used for state that is relied upon (`schema_version`, cycle
        stamps, ledger generation), so its failure must be heard. But `last_error_*`
        lines are **a description of a failure that already happened**, and are written
        from inside an exception handler — if the database is the failing resource it
        would raise too, taking down the handler and with it the rest of the cycle, and
        then the loop shield itself. Measured 2026-08-17: `database is locked` by this
        path exited the recorder with code 1, leaving the task `Ready` for three silent hours. Losing a descriptive line is cheaper than losing the cycle, and measurements are not written this way.
        """
        try:
            self.set_meta(key, english_note(value))
            return True
        except Exception:  # noqa: BLE001 — bookkeeping, not a measurement
            return False

    def evm_ledger_generation(self) -> int:
        value = self.get_meta("evm_ledger_generation")
        return int(value) if value and value.isdigit() else 0

    def assert_evm_ledger_generation(self, expected: int) -> None:
        current = self.evm_ledger_generation()
        if current != int(expected):
            raise StaleEVMState(
                f"EVM ledger generation changed mid-cycle: {expected} -> {current}"
            )

    def assert_evm_cursor(self, network_id: str, expected_block: int | None) -> None:
        row = self.evm_cursor(network_id)
        current = int(row["last_block"]) if row is not None else None
        if current != expected_block:
            raise StaleEVMState(
                f"EVM cursor [{network_id}] changed while fetching: "
                f"{expected_block} -> {current}"
            )

    def assert_evm_backfill_state(
        self, network_id: str, token_address: str,
        expected_status: str | None, expected_from: int | None,
        expected_to: int | None,
    ) -> None:
        row = self.evm_backfill_state(network_id, token_address)
        current = (
            None if row is None else
            (row.get("status"), row.get("from_block"), row.get("to_block"))
        )
        expected = (
            None if expected_status is None and expected_from is None and expected_to is None
            else (expected_status, expected_from, expected_to)
        )
        if current != expected:
            raise StaleEVMState(
                f"EVM backfill state for token {token_address} changed while fetching"
            )

    def bump_evm_ledger_generation(self) -> int:
        current = self.evm_ledger_generation() + 1
        self.set_meta("evm_ledger_generation", str(current))
        return current

    def bump_counter(self, key: str, by: int = 1) -> None:
        cur = self.get_meta(key)
        n = (int(cur) if cur and cur.isdigit() else 0) + by
        self.set_meta(key, str(n))

    # --- signal_events ---
    def insert_signal(self, row: Mapping[str, Any]) -> bool:
        """Idempotent insert keyed by id (feed event id). Returns True if a new row was inserted.

        We build the parameters from an explicit column list (like the other inserts),
        not from the caller's keys: passing the dict as-is meant any new column would
        crash every caller that did not yet know it — a `ProgrammingError` instead of a missing field.
        """
        cols = _SIGNAL_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO signal_events({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        duplicate = self._conn.execute(
            """SELECT 1 FROM signal_events
                WHERE token_address=? AND COALESCE(network_id, '')=COALESCE(?, '')
                  AND ts=? AND signal_type=? LIMIT 1""",
            (row.get("token_address"), row.get("network_id"),
             row.get("ts"), row.get("signal_type")),
        ).fetchone()
        if duplicate:
            return False
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    # --- market_ticks ---
    def insert_tick(self, row: Mapping[str, Any]) -> bool:
        """Composite key (token, network, recorded_at, source) prevents duplicates within a cycle."""
        cols = _TICK_COLUMNS
        placeholders = ", ".join(f":{c}" for c in cols)
        sql = (
            f"INSERT OR IGNORE INTO market_ticks({', '.join(cols)}) "
            f"VALUES({placeholders})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    # --- token_static ---
    def upsert_static(self, row: Mapping[str, Any]) -> None:
        """Token constants — written once; left as-is if already present (INSERT OR IGNORE)."""
        cols = _STATIC_COLUMNS
        placeholders = ", ".join(f":{c}" for c in cols)
        sql = (
            f"INSERT OR IGNORE INTO token_static({', '.join(cols)}) "
            f"VALUES({placeholders})"
        )
        values = {c: row.get(c) for c in cols}
        if str(values.get("network_id") or "") in _EVM_NETWORK_IDS:
            address = values.get("token_address")
            if isinstance(address, str) and address.lower().startswith("0x"):
                values["token_address"] = address.lower()
        if (
            values.get("token_created_at") not in (None, "")
            and values.get("token_created_at_observed_at") in (None, "")
        ):
            values["token_created_at_observed_at"] = values.get("recorded_at")
        self._conn.execute(sql, _with_compressed_raw(values))
        self._commit()

    def static_exists(self, token_address: str, network_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM token_static WHERE token_address=? AND network_id=?",
            (token_address, network_id),
        ).fetchone()
        return row is not None

    def set_age_lookup_state(
        self, token_address: str, network_id: str, status: str, now_iso: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO token_age_lookup_state(
                   token_address, network_id, last_lookup_at, last_status, attempts)
               VALUES(?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_lookup_at=excluded.last_lookup_at,
                   last_status=excluded.last_status,
                   attempts=token_age_lookup_state.attempts + 1""",
            (token_address, network_id, now_iso, status),
        )
        self._commit()

    def age_lookup_due(
        self, token_address: str, network_id: str, now_iso: str,
        missing_retry_seconds: int, error_retry_seconds: int,
    ) -> bool:
        row = self._conn.execute(
            """SELECT last_lookup_at, last_status FROM token_age_lookup_state
                WHERE token_address=? AND network_id=?""",
            (token_address, network_id),
        ).fetchone()
        if row is None or str(row["last_status"] or "") == "ok":
            return row is None
        try:
            elapsed = (
                datetime.fromisoformat(now_iso)
                - datetime.fromisoformat(str(row["last_lookup_at"]))
            ).total_seconds()
        except (TypeError, ValueError):
            return True
        delay = (
            error_retry_seconds
            if row["last_status"] == "error"
            else missing_retry_seconds
        )
        return elapsed >= max(0, int(delay))

    # --- snapshots ---
    def insert_snapshot(self, source: str, raw: Any, recorded_at: str | None = None) -> None:
        """A full raw archive for a source. Stored compressed — this table alone was 80%
        of the database size (~310 KB per snapshot × 3 sources × 1440 cycles daily)."""
        self._conn.execute(
            "INSERT INTO snapshots(recorded_at, source, raw_json) VALUES(?, ?, ?)",
            (recorded_at or utcnow_iso(), source, encode_raw(raw)),
        )
        self._commit()

    def read_snapshot(self, snapshot_id: int) -> Any:
        """Reads a snapshot and decompresses it (accepts old text rows too)."""
        row = self._conn.execute(
            "SELECT raw_json FROM snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        return decode_raw(row["raw_json"]) if row else None

    def prune_snapshots(self, older_than_iso: str) -> int:
        """Deletes snapshots older than a given timestamp. Returns the number deleted.

        Optional and disabled by default (config.SNAPSHOT_RETENTION_DAYS = 0): snapshots
        are the re-derivation archive, so deleting them is the owner's decision, not implicit behavior.
        """
        cur = self._conn.execute(
            "DELETE FROM snapshots WHERE recorded_at < ?", (older_than_iso,)
        )
        self._commit()
        return cur.rowcount

    # --- token_bars ---
    def insert_bars(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Inserts OHLCV candles. Returns the number of rows written.

        OR REPLACE, not OR IGNORE: the newest candle is still forming at fetch time,
        so its value gets revised by the next fetch — the newest is the correct one.
        """
        if not rows:
            return 0
        cols = _BAR_COLUMNS
        sql = (
            f"INSERT OR REPLACE INTO token_bars({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        with self.batch():
            self._conn.executemany(sql, [{c: r.get(c) for c in cols} for r in rows])
        return len(rows)

    def recompute_bar_flags(
        self, token_address: str, network_id: str, resolution: str = "5"
    ) -> int:
        """Recomputes the distortion flags for a token's whole series. Returns the number of rows updated.

        Needed after every insert: the neighbour rule (`bar_context_flags`) needs the
        next candle, and the last candle in any batch has none yet — so it is judged
        when it arrives. Raw values are untouched; only the flags change.
        """
        from extract import bar_context_flags  # local import: db does not depend on extract

        rows = self._conn.execute(
            "SELECT ts, o, h, l, c, h_suspect, l_suspect, c_suspect FROM token_bars "
            "WHERE token_address=? AND network_id=? AND resolution=? ORDER BY ts",
            (token_address, network_id, resolution),
        ).fetchall()
        if not rows:
            return 0
        series = [dict(r) for r in rows]
        import config
        ratio = (
            config.DAILY_BAR_WICK_MAX_RATIO
            if resolution == "1D" else config.BAR_WICK_MAX_RATIO
        )
        changes = [
            (h, low, c, token_address, network_id, resolution, b["ts"])
            for b, (h, low, c) in zip(
                series, bar_context_flags(series, max_ratio=ratio), strict=True
            )
            if (h, low, c) != (b["h_suspect"], b["l_suspect"], b["c_suspect"])
        ]
        if changes:
            with self.batch():
                self._conn.executemany(
                    "UPDATE token_bars SET h_suspect=?, l_suspect=?, c_suspect=? "
                    "WHERE token_address=? AND network_id=? AND resolution=? AND ts=?",
                    changes,
                )
        return len(changes)

    def bars_count(self, token_address: str, network_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM token_bars WHERE token_address=? AND network_id=?",
            (token_address, network_id),
        ).fetchone()
        return int(row["n"])

    def set_bars_state(
        self, token_address: str, network_id: str, status: str, candles: int, now_iso: str
    ) -> None:
        """Records the outcome of the last fetch attempt. `attempts` accumulates to surface dead tokens."""
        self._conn.execute(
            """INSERT INTO bars_fetch_state(
                   token_address, network_id, last_fetch_at, last_status, candles, attempts)
               VALUES(?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   candles       = excluded.candles,
                   attempts      = bars_fetch_state.attempts + 1""",
            (token_address, network_id, now_iso, status, candles),
        )
        self._commit()

    def bars_fetch_due(
        self, limit: int, stale_before_iso: str, max_no_data_attempts: int
    ) -> list[dict[str, Any]]:
        """Active watched tokens due for a fetch — least recently fetched first.

        Round-robin scheduling: each cycle takes a small slice, so the sweep completes
        over several cycles without a burst that loads the loop or floods fomo. A
        token fomo answered `no_data` repeatedly is excluded — no point wasting
        attempts on a token with no price series. `NULLS FIRST` guarantees the never-fetched come before everyone (entry backfill).
        """
        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, w.first_seen_at,
                      s.last_fetch_at, s.last_status, s.attempts
               FROM watchlist w
               LEFT JOIN bars_fetch_state s
                 ON s.token_address = w.token_address AND s.network_id = w.network_id
               WHERE w.active = 1
                 AND (s.last_fetch_at IS NULL OR s.last_fetch_at < ?)
                 -- COALESCE is mandatory: without a state row the condition
                 -- becomes NOT (NULL AND NULL) = NULL, silently excluding every
                 -- never-fetched token — exactly the case the backfill exists for.
                 AND NOT (COALESCE(s.last_status, '') = 'no_data'
                          AND COALESCE(s.attempts, 0) >= ?)
               ORDER BY s.last_fetch_at IS NOT NULL, s.last_fetch_at
               LIMIT ?""",
            (stale_before_iso, max_no_data_attempts, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- token_social ---
    def insert_social(self, row: Mapping[str, Any]) -> bool:
        """A social snapshot. The key (token, network, recorded_at) prevents duplicates."""
        cols = _SOCIAL_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO token_social({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    def insert_thesis_items(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Inserts individual theses. Idempotent by id. Returns the number actually inserted."""
        if not rows:
            return 0
        cols = _THESIS_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO token_thesis({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        added = 0
        with self.batch():
            for r in rows:
                cur = self._conn.execute(
                    sql, _with_compressed_raw({c: r.get(c) for c in cols})
                )
                added += cur.rowcount
        return added

    def thesis_count_before(self, token_address: str, at_iso: str) -> int:
        """How many theses existed before a given moment — rebuilding the historical count."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM token_thesis "
            "WHERE token_address=? AND created_at <= ?",
            (token_address, at_iso),
        ).fetchone()
        return int(row["n"])

    # --- activity_events (written only by backfill_activity.py, retroactively) ---
    def insert_activity_events(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Inserts tradingActivity events. Idempotent by id — resuming the walk from the
        checkpoint passes over inserted rows harmlessly. Returns the number actually inserted."""
        if not rows:
            return 0
        cols = _ACTIVITY_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO activity_events({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        added = 0
        with self.batch():
            for r in rows:
                cur = self._conn.execute(
                    sql, _with_compressed_raw({c: r.get(c) for c in cols})
                )
                added += cur.rowcount
        return added

    def activity_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM activity_events").fetchone()
        return int(row["n"])

    def activity_event_tokens(self) -> list[dict[str, Any]]:
        """Tokens with labelable events plus their time range — for fetching candles."""
        rows = self._conn.execute(
            """SELECT token_address, network_id, MIN(ts) AS min_ts, MAX(ts) AS max_ts,
                      COUNT(*) AS n
                 FROM activity_events
                WHERE token_address IS NOT NULL AND ts IS NOT NULL
                GROUP BY token_address, network_id
                ORDER BY n DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def activity_events_for_token(self, token_address: str, network_id: str) -> list[dict[str, Any]]:
        """A token's events in chronological order (for candle-fetch clusters)."""
        rows = self._conn.execute(
            """SELECT id, ts FROM activity_events
                WHERE token_address=? AND network_id=? AND ts IS NOT NULL
                ORDER BY ts""",
            (token_address, network_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_activity_bars_state(
        self, token_address: str, network_id: str, status: str, candles: int, now_iso: str
    ) -> None:
        self._conn.execute(
            """INSERT INTO activity_bars_state(
                   token_address, network_id, last_fetch_at, last_status, candles, attempts)
               VALUES(?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   candles       = excluded.candles,
                   attempts      = activity_bars_state.attempts + 1""",
            (token_address, network_id, now_iso, status, candles),
        )
        self._commit()

    def activity_bars_state(self, token_address: str, network_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM activity_bars_state WHERE token_address=? AND network_id=?",
            (token_address, network_id),
        ).fetchone()
        return dict(row) if row else None

    def activities_pending_label(self, mature_before_epoch: int, limit: int) -> list[dict[str, Any]]:
        """Activity events that matured, are unlabeled, and whose token's candles are fetched (status='ok').

        The last gate is decisive: labeling is idempotent and never revised, so labeling
        an event before its token's candles arrive writes an eternal no_entry. `prev_ts`
        = timestamp of the previous activity event on the same token (for the independence flag, same rule as signals).
        """
        rows = self._conn.execute(
            """SELECT a.id, a.event_type, a.token_address, a.network_id, a.ts,
                      CAST(strftime('%s', a.ts) AS INTEGER) AS entry_epoch,
                      (SELECT MAX(p.ts) FROM activity_events p
                        WHERE p.token_address = a.token_address AND p.ts < a.ts)
                        AS prev_ts
                 FROM activity_events a
                WHERE a.ts IS NOT NULL
                  AND a.token_address IS NOT NULL
                  AND CAST(strftime('%s', a.ts) AS INTEGER) <= ?
                  AND NOT EXISTS (SELECT 1 FROM outcomes o
                                   WHERE o.kind = 'activity' AND o.key = a.id)
                  AND EXISTS (SELECT 1 FROM activity_bars_state s
                               WHERE s.token_address = a.token_address
                                 AND s.network_id = a.network_id
                                 AND s.last_status = 'ok')
                ORDER BY a.ts LIMIT ?""",
            (mature_before_epoch, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_social_state(
        self, token_address: str, network_id: str, status: str, items: int, now_iso: str
    ) -> None:
        self._conn.execute(
            """INSERT INTO social_fetch_state(
                   token_address, network_id, last_fetch_at, last_status, items, attempts)
               VALUES(?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   items         = excluded.items,
                   attempts      = social_fetch_state.attempts + 1""",
            (token_address, network_id, now_iso, status, items),
        )
        self._commit()

    def social_fetch_due(
        self, limit: int, stale_before_iso: str,
        error_stale_before_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Tokens due for a social snapshot — least recently fetched first.

        Unlike candles, we do not exclude the empty token: the absence of discussion
        is **a signal in itself**, and its change over time is what we want — so there is no point dropping a token that is silent today.
        """
        error_stale = error_stale_before_iso or stale_before_iso
        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, s.last_fetch_at
               FROM watchlist w
               LEFT JOIN social_fetch_state s
                  ON s.token_address = w.token_address AND s.network_id = w.network_id
               WHERE w.active = 1
                 AND (s.last_fetch_at IS NULL
                      OR (s.last_status='error' AND s.last_fetch_at < ?)
                      OR (COALESCE(s.last_status, '') <> 'error'
                          AND s.last_fetch_at < ?))
               ORDER BY s.last_fetch_at IS NOT NULL, s.last_fetch_at
               LIMIT ?""",
            (error_stale, stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def social_count(self, token_address: str, network_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM token_social WHERE token_address=? AND network_id=?",
            (token_address, network_id),
        ).fetchone()
        return int(row["n"])

    # --- token_holders ---
    def insert_holders(self, row: Mapping[str, Any]) -> bool:
        """A holding-concentration snapshot; the key includes source so one source never masks another."""
        cols = _HOLDERS_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO token_holders({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    def set_holders_state(
        self, token_address: str, network_id: str, status: str,
        top10_pct: float | None, now_iso: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO holders_fetch_state(
                   token_address, network_id, last_fetch_at, last_status,
                   top10_pct, attempts)
               VALUES(?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   top10_pct     = excluded.top10_pct,
                   attempts      = holders_fetch_state.attempts + 1""",
            (token_address, network_id, now_iso, status, top10_pct),
        )
        self._commit()

    # --- token_flow ---
    def insert_flow(self, row: Mapping[str, Any]) -> bool:
        """A trade flow row (buy/sell). The same tokenDetails reply that feeds holdings."""
        cols = _FLOW_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO token_flow({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    # --- traders ---
    def upsert_trader(self, row: Mapping[str, Any]) -> None:
        """A trader profile. The profile **changes** (followers, trade count) so we
        overwrite it — unlike token_static, which is constant by nature."""
        cols = _TRADER_COLUMNS
        sql = (
            f"INSERT OR REPLACE INTO traders({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()

    def set_trader_state(self, trader_id: str, status: str, now_iso: str) -> None:
        self._conn.execute(
            """INSERT INTO traders_fetch_state(
                   trader_id, last_fetch_at, last_status, attempts)
               VALUES(?, ?, ?, 1)
               ON CONFLICT(trader_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   attempts      = traders_fetch_state.attempts + 1""",
            (trader_id, now_iso, status),
        )
        self._commit()

    def traders_fetch_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
        min_events: int = 3,
    ) -> list[dict[str, Any]]:
        """Traders due for fetching: **repeat traders only** (≥`min_events` events).

        Measured: 5,572 distinct buyers but only 3,202 with ≥3 events — someone who
        appeared once has no behavior for us to learn, so fetching them spends cycle
        budget for nothing. Ordering: never-fetched first, then the most active (their events carry the most information).

        `COALESCE(s.last_status,'') <> 'error'` is mandatory: without it the condition
        becomes NULL for anyone without a state row, excluding them forever.
        """
        rows = self._conn.execute(
            """SELECT e.buyer_id AS trader_id, e.n AS event_count,
                      s.last_fetch_at, s.last_status
                 FROM (SELECT buyer_id, COUNT(*) n FROM signal_events
                        WHERE buyer_id IS NOT NULL AND buyer_id <> ''
                        GROUP BY buyer_id HAVING COUNT(*) >= ?) e
                 LEFT JOIN traders_fetch_state s ON s.trader_id = e.buyer_id
                WHERE (s.last_fetch_at IS NULL
                       OR (s.last_status='error' AND s.last_fetch_at < ?)
                         OR (COALESCE(s.last_status, '') NOT IN ('error', 'unsupported')
                             AND s.last_fetch_at < ?))
                ORDER BY s.last_fetch_at IS NOT NULL,
                         s.last_fetch_at,
                         e.n DESC
                LIMIT ?""",
            (min_events, error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_static_protocol(
        self, token_address: str, network_id: str, protocol: str
    ) -> bool:
        """Fills `dex_protocol` **only when it is empty**.

        `upsert_static` is INSERT OR IGNORE so it never updates an existing row, and
        the protocol comes only from filterTokens (absent from raw trending: zero of
        3,000) — so a narrow update path is needed. The `IS NULL` condition prevents overwriting an existing measurement.
        """
        cur = self._conn.execute(
            """UPDATE token_static SET dex_protocol=?
                WHERE token_address=? AND network_id=? AND dex_protocol IS NULL""",
            (protocol, token_address, network_id),
        )
        self._commit()
        return cur.rowcount > 0

    def set_static_created_at(
        self, token_address: str, network_id: str, created_at: str,
        observed_at: str,
    ) -> bool:
        """Fills `token_created_at` **only when it is empty**.

        A rare but real case: the source returned the token in a public list without a
        creation date (3 of 406 measured), so the static row exists while the age is
        missing — and `upsert_static` is INSERT OR IGNORE so it cannot fix it. filterTokens
        is asked about this token every cycle, so the first answer carrying the date closes the gap.

        The emptiness condition prevents overwriting a recorded date: the source's own
        value **changes** (53 of 216 contradict the stored one by more than an hour, one
        by a year), so we pin the first thing we saw instead of following its drift —
        otherwise the age-gate verdict flips under a token that was already accepted.
        """
        cur = self._conn.execute(
            """UPDATE token_static
                  SET token_created_at=?, token_created_at_observed_at=?
                WHERE token_address=? AND network_id=?
                  AND (token_created_at IS NULL OR token_created_at='')""",
            (created_at, observed_at, token_address, network_id),
        )
        self._commit()
        return cur.rowcount > 0

    def observe_static_created_at(
        self, token_address: str, network_id: str, expected_created_at: str,
        observed_at: str,
    ) -> bool:
        """Record when an existing creation timestamp was seen in a live item."""
        cur = self._conn.execute(
            """UPDATE token_static SET token_created_at_observed_at=?
                WHERE token_address=? AND network_id=?
                  AND token_created_at=?
                  AND token_created_at_observed_at IS NULL""",
            (observed_at, token_address, network_id, expected_created_at),
        )
        self._commit()
        return cur.rowcount > 0

    def replace_unobserved_static_created_at(
        self, token_address: str, network_id: str, old_created_at: str,
        created_at: str, observed_at: str,
    ) -> bool:
        """Atomically replace only an unproven value the caller just read."""
        cur = self._conn.execute(
            """UPDATE token_static
                  SET token_created_at=?, token_created_at_observed_at=?
                WHERE token_address=? AND network_id=?
                  AND token_created_at=?
                  AND token_created_at_observed_at IS NULL""",
            (created_at, observed_at, token_address, network_id, old_created_at),
        )
        self._commit()
        return cur.rowcount > 0

    def replace_static_created_at(
        self, token_address: str, network_id: str, created_at: str,
        observed_at: str,
    ) -> bool:
        """Replace a timestamp only after the caller validated the new value."""
        cur = self._conn.execute(
            """UPDATE token_static
                  SET token_created_at=?, token_created_at_observed_at=?
                WHERE token_address=? AND network_id=?""",
            (created_at, observed_at, token_address, network_id),
        )
        self._commit()
        return cur.rowcount > 0

    def holders_fetch_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
    ) -> list[dict[str, Any]]:
        """Watches due for a concentration measurement; newest first and signals before controls."""
        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, w.first_seen_at,
                      w.entry_signal_id, w.is_control, s.last_fetch_at, s.last_status
                 FROM watchlist w
                 LEFT JOIN holders_fetch_state s
                   ON s.token_address=w.token_address AND s.network_id=w.network_id
                WHERE w.active=1
                  AND (s.last_fetch_at IS NULL
                       OR (s.last_status='error' AND s.last_fetch_at < ?)
                       OR (COALESCE(s.last_status, '') <> 'error'
                           AND s.last_fetch_at < ?))
                ORDER BY s.last_fetch_at IS NOT NULL,
                         w.is_control,
                         s.last_fetch_at,
                         w.first_seen_at DESC
                LIMIT ?""",
            (error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- chain_concentration (direct on-chain measurement) ---
    def insert_chain_concentration(self, row: Mapping[str, Any]) -> bool:
        """A concentration snapshot from the chain. The key is (address, network, time), so duplicates are harmless."""
        cols = _CHAIN_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO chain_concentration({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        payload = {c: row.get(c) for c in cols}
        # Zero, not NULL: the column is `NOT NULL DEFAULT 0` and there are three row
        # writers (Solana, the live EVM ledger, retroactive replay) — normalizing
        # here spares them from remembering it, and an explicit NULL from any of them would raise `NOT NULL constraint failed`.
        payload["is_replay"] = 1 if payload.get("is_replay") else 0
        cur = self._conn.execute(sql, _with_compressed_raw(payload))
        self._commit()
        return cur.rowcount > 0

    def set_chain_state(
        self, token_address: str, network_id: str, status: str,
        top1_pct: float | None, now_iso: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO chain_fetch_state(
                   token_address, network_id, last_fetch_at, last_status,
                   top1_pct, attempts)
               VALUES(?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   top1_pct      = excluded.top1_pct,
                   attempts      = chain_fetch_state.attempts + 1""",
            (token_address, network_id, now_iso, status, top1_pct),
        )
        self._commit()

    def chain_fetch_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
        networks: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Watches due for an on-chain measurement — **supported networks only**.

        Networks are passed in, not read from `config` here: this module knows nothing
        about sources (see the file header) so it stays testable with a temporary
        database. And an empty list returns nothing rather than meaning "all networks"
        — silence is more honest than sweeping a network the source does not serve.
        """
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.entry_signal_id, w.is_control, s.last_fetch_at, s.last_status
                  FROM watchlist w
                  LEFT JOIN chain_fetch_state s
                    ON s.token_address=w.token_address AND s.network_id=w.network_id
                 WHERE w.active=1
                   AND w.network_id IN ({marks})
                   AND (s.last_fetch_at IS NULL
                        OR (s.last_status='error' AND s.last_fetch_at < ?)
                        OR (COALESCE(s.last_status, '') <> 'error'
                            AND s.last_fetch_at < ?))
                 ORDER BY s.last_fetch_at IS NOT NULL,
                          w.is_control,
                          s.last_fetch_at,
                          w.first_seen_at DESC
                 LIMIT ?""",
            (*nets, error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def evm_snapshot_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
        networks: Sequence[str],
    ) -> list[dict[str, Any]]:
        """EVM tokens with a complete ledger that are due for a snapshot, with filtering before `LIMIT`."""
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.entry_signal_id, w.is_control, s.last_fetch_at, s.last_status
                  FROM watchlist w
                  JOIN evm_backfill_state b
                    ON b.token_address=w.token_address AND b.network_id=w.network_id
                   AND b.status='done'
                  LEFT JOIN chain_fetch_state s
                    ON s.token_address=w.token_address AND s.network_id=w.network_id
                 WHERE w.active=1
                   AND w.network_id IN ({marks})
                   AND (s.last_fetch_at IS NULL
                        OR (s.last_status='error' AND s.last_fetch_at < ?)
                        OR (COALESCE(s.last_status, '') <> 'error'
                            AND s.last_fetch_at < ?))
                 ORDER BY s.last_fetch_at IS NOT NULL,
                          w.is_control,
                          s.last_fetch_at,
                          w.first_seen_at DESC
                 LIMIT ?""",
            (*nets, error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- chain_authority (the slow layer: authorities and mutability) ---
    def insert_chain_authority(self, row: Mapping[str, Any]) -> bool:
        """An authority snapshot. One row per measurement, not one per token: revoking
        the mint authority **is an event** that happens mid-window, and a single row updated in place erases its history."""
        cols = _CHAIN_AUTH_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO chain_authority({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    def set_chain_auth_state(
        self, token_address: str, network_id: str, status: str, now_iso: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO chain_auth_state(
                   token_address, network_id, last_fetch_at, last_status, attempts)
               VALUES(?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   attempts      = chain_auth_state.attempts + 1""",
            (token_address, network_id, now_iso, status),
        )
        self._commit()

    def chain_auth_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
        networks: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Same logic as `chain_fetch_due` on the lazy state table.

        A separate query, not a `table` parameter interpolated into the text: a table
        name is never built from input, and repeating ten lines is cheaper than opening an injection door.
        """
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.entry_signal_id, w.is_control, s.last_fetch_at, s.last_status
                  FROM watchlist w
                  LEFT JOIN chain_auth_state s
                    ON s.token_address=w.token_address AND s.network_id=w.network_id
                 WHERE w.active=1
                   AND w.network_id IN ({marks})
                   AND (s.last_fetch_at IS NULL
                        OR (s.last_status='error' AND s.last_fetch_at < ?)
                        OR (COALESCE(s.last_status, '') <> 'error'
                            AND s.last_fetch_at < ?))
                 ORDER BY s.last_fetch_at IS NOT NULL,
                          w.is_control,
                          s.last_fetch_at,
                          w.first_seen_at DESC
                 LIMIT ?""",
            (*nets, error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- EVM layer: the balance ledger and the block cursor ---
    def evm_cursor(self, network_id: str) -> dict[str, Any] | None:
        """The network's block cursor, or None if it has not been built yet."""
        row = self._conn.execute(
            "SELECT * FROM evm_block_cursor WHERE network_id=?", (str(network_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_evm_cursor(
        self, network_id: str, last_block: int, now_iso: str,
        status: str, logs_applied: int = 0, last_error: str | None = None,
    ) -> None:
        """Advances the cursor. `logs_applied` accumulates and is never replaced — a
        single cycle's number carries no diagnostic meaning; the total is what says whether the layer works at all."""
        self._conn.execute(
            """INSERT INTO evm_block_cursor(
                   network_id, last_block, last_run_at, last_status,
                   logs_applied, last_error)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(network_id) DO UPDATE SET
                   last_block   = excluded.last_block,
                   last_run_at  = excluded.last_run_at,
                   last_status  = excluded.last_status,
                   logs_applied = evm_block_cursor.logs_applied + excluded.logs_applied,
                   last_error   = excluded.last_error""",
            (str(network_id), int(last_block), now_iso, status,
             int(logs_applied), english_note(last_error)),
        )
        self._commit()

    def evm_apply_transfers(
        self, network_id: str, token_address: str,
        deltas: Mapping[str, tuple[int, ...]], now_iso: str,
        allow_negative: Sequence[str] = (),
    ) -> int:
        """Applies balance changes for a single token. `deltas` = address ⇒ (change, block number).

        The change **is signed** and is added to the stored balance with Python
        arithmetic, not SQL: uint256 values exceed 64 bits, so `balance_hex + ?` in
        SQLite overflows silently. Read and write in one transaction via `batch()` from the caller.

        A negative balance is impossible for an ordinary address in a correct ERC-20;
        seeing one means a record was lost or duplicated, so we raise and roll the
        transaction back instead of storing a ledger that looks sound. Only the
        mint/burn addresses passed in `allow_negative` may go below zero: their balance never enters the snapshot anyway, so we pin it at zero without hiding a holder's corruption.
        """
        if not deltas:
            return 0
        token = token_address.lower()
        net = str(network_id)
        allowed = {str(a).lower() for a in allow_negative}
        holders = list(deltas)
        variable_limit = self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        chunk_size = max(1, int(variable_limit) - 2)
        current: dict[str, tuple[str, int | None]] = {}
        for offset in range(0, len(holders), chunk_size):
            chunk = holders[offset:offset + chunk_size]
            marks = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""SELECT holder_address, balance_hex, first_seen_block
                      FROM evm_balances
                     WHERE network_id=? AND token_address=?
                       AND holder_address IN ({marks})""",
                (net, token, *chunk),
            ).fetchall()
            current.update({
                row["holder_address"]: (row["balance_hex"], row["first_seen_block"])
                for row in rows
            })
        rows = []
        for holder, change in deltas.items():
            delta, block = change[:2]
            received_block = change[2] if len(change) > 2 else None
            prev_hex, first_block = current.get(holder, (None, None))
            prev = int(prev_hex, 16) if prev_hex else 0
            new = prev + int(delta)
            if new < 0:
                if holder.lower() not in allowed:
                    raise ValueError(
                        f"negative EVM balance for address {holder} in {token} [{net}]"
                    )
                new = 0
            # First receipt: the block is recorded once and never updated after — "new
            # holder" means first entry, not the latest movement.
            if first_block is None:
                if received_block is not None:
                    first_block = int(received_block)
                elif delta > 0:
                    first_block = int(block)
            rows.append((net, token, holder, f"{new:064x}",
                         first_block, int(block), now_iso))
        self._conn.executemany(
            """INSERT INTO evm_balances(
                   network_id, token_address, holder_address, balance_hex,
                   first_seen_block, updated_block, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(network_id, token_address, holder_address) DO UPDATE SET
                   balance_hex      = excluded.balance_hex,
                   first_seen_block = COALESCE(evm_balances.first_seen_block,
                                               excluded.first_seen_block),
                   updated_block    = excluded.updated_block,
                   updated_at       = excluded.updated_at""",
            rows,
        )
        self._commit()
        return len(rows)

    def evm_top_balances(
        self, network_id: str, token_address: str, limit: int,
        exclude: Sequence[str] = (),
    ) -> list[tuple[str, int]]:
        """The top `limit` balances in descending order.

        The ordering is lexicographic over a width-64 padded string ⇒ identical to
        numeric ordering, so the `idx_evm_bal_rank` index serves it unsorted. A zero
        balance is excluded: an address that sold everything keeps its ledger row (its history is information) but is not a holder.
        """
        net, token = str(network_id), token_address.lower()
        params: list[Any] = [net, token]
        clause = ""
        skip = [a.lower() for a in exclude]
        if skip:
            clause = f" AND holder_address NOT IN ({', '.join('?' for _ in skip)})"
            params.extend(skip)
        params.append(int(limit))
        rows = self._conn.execute(
            f"""SELECT holder_address, balance_hex FROM evm_balances
                 WHERE network_id=? AND token_address=?
                   AND balance_hex <> '{'0' * 64}'{clause}
                 ORDER BY balance_hex DESC
                 LIMIT ?""",
            params,
        ).fetchall()
        return [(r["holder_address"], int(r["balance_hex"], 16)) for r in rows]

    def evm_ledger_stats(
        self, network_id: str, token_address: str, exclude: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Holder count and circulating supply from the ledger — in a single SQL call.

        `supply` here is the sum of live balances, not the contract's `totalSupply()`:
        burn addresses are excluded, so ratios are computed over what can actually be
        sold. A token whose half was burned shows its true concentration, not one diluted by the dead half.
        """
        net, token = str(network_id), token_address.lower()
        params: list[Any] = [net, token]
        clause = ""
        skip = [a.lower() for a in exclude]
        if skip:
            clause = f" AND holder_address NOT IN ({', '.join('?' for _ in skip)})"
            params.extend(skip)
        rows = self._conn.execute(
            f"""SELECT balance_hex FROM evm_balances
                 WHERE network_id=? AND token_address=?
                   AND balance_hex <> '{'0' * 64}'{clause}""",
            params,
        ).fetchall()
        total = 0
        for r in rows:
            total += int(r["balance_hex"], 16)
        return {"holder_count": len(rows), "supply": total}

    def evm_ledger_frontier(
        self, network_id: str, token_address: str,
    ) -> int | None:
        """The highest block the ledger has applied for this token.

        A backfill resume point older than this describes a range that was
        already consumed — by live apply, after the row went stale — and
        re-walking it would apply the same transfers twice. `None` for a
        token with no ledger rows yet: a fresh walk is not clamped.
        """
        row = self._conn.execute(
            "SELECT MAX(updated_block) AS frontier FROM evm_balances"
            " WHERE network_id=? AND token_address=?",
            (str(network_id), token_address.lower()),
        ).fetchone()
        frontier = row["frontier"] if row else None
        return int(frontier) if frontier is not None else None

    def evm_new_holders_since(
        self, network_id: str, token_address: str, since_block: int,
    ) -> int:
        """How many addresses first received the token after a given block.

        This is what no provider gives: all of them a snapshot with no entry history.
        It is computed from `first_seen_block` alone, so it costs no call.
        """
        row = self._conn.execute(
            """SELECT COUNT(*) c FROM evm_balances
                WHERE network_id=? AND token_address=? AND first_seen_block > ?""",
            (str(network_id), token_address.lower(), int(since_block)),
        ).fetchone()
        return int(row["c"] or 0)

    def evm_backfill_state(
        self, network_id: str, token_address: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM evm_backfill_state
                WHERE network_id=? AND token_address=?""",
            (str(network_id), token_address.lower()),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_evm_backfill_state(
        self, network_id: str, token_address: str, status: str, now_iso: str,
        from_block: int | None = None, to_block: int | None = None,
        transfers: int | None = None, calls: int | None = None,
        last_error: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO evm_backfill_state(
                   network_id, token_address, status, from_block, to_block,
                   transfers, calls, last_try_at, last_error)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(network_id, token_address) DO UPDATE SET
                   status      = excluded.status,
                   from_block  = COALESCE(excluded.from_block,
                                          evm_backfill_state.from_block),
                   to_block    = COALESCE(excluded.to_block,
                                          evm_backfill_state.to_block),
                   transfers   = COALESCE(excluded.transfers,
                                          evm_backfill_state.transfers),
                   calls       = COALESCE(excluded.calls, evm_backfill_state.calls),
                   last_try_at = excluded.last_try_at,
                   last_error  = excluded.last_error""",
            (str(network_id), token_address.lower(), status, from_block, to_block,
             transfers, calls, now_iso, english_note(last_error)),
        )
        self._commit()

    def evm_watched(
        self, networks: Sequence[str],
        extra_tokens: Sequence[tuple[str, str]] = (),
    ) -> list[dict[str, Any]]:
        """All active watches on the enabled EVM networks, with their backfill state.

        One row joins the watch and the backfill: the layer needs both every cycle (who
        the log applies to, and who awaits backfill), and two calls would drift apart.
        An empty network list returns nothing — not "all networks".
        """
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        extra = [(str(network), str(token).lower()) for network, token in extra_tokens]
        raw_cohort = self.get_meta("evm_repair_cohort")
        if raw_cohort:
            try:
                cohort = json.loads(raw_cohort)
                extra.extend(
                    (str(item["network_id"]), str(item["token_address"]).lower())
                    for item in cohort.get("active_backfills", [])
                )
            except (KeyError, TypeError, ValueError):
                pass
        extra = list(dict.fromkeys(extra))
        extra_sql = " OR ".join(
            "(w.network_id=? AND lower(w.token_address)=?)" for _ in extra
        )
        active_clause = "w.active=1"
        params: list[Any] = list(nets)
        if extra_sql:
            active_clause += f" OR {extra_sql}"
            params.extend(value for pair in extra for value in pair)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.entry_signal_id, w.is_control,
                       s.token_created_at, s.token_created_at_observed_at,
                       b.status AS backfill_status, b.from_block, b.to_block,
                       b.transfers AS backfill_transfers,
                       b.calls AS backfill_calls,
                       b.last_try_at AS backfill_last_try_at
                  FROM watchlist w
                  LEFT JOIN token_static s
                    ON CASE WHEN s.network_id IN ('56','143','4663','8453')
                            THEN lower(s.token_address)=lower(w.token_address)
                            ELSE s.token_address=w.token_address END
                   AND s.network_id=w.network_id
                   AND s.recorded_at <= w.first_seen_at
                  LEFT JOIN evm_backfill_state b
                    ON lower(b.token_address)=lower(w.token_address)
                   AND b.network_id=w.network_id
                  WHERE w.network_id IN ({marks}) AND ({active_clause})
                  ORDER BY w.is_control, w.first_seen_at DESC""",
            params,
        ).fetchall()
        return [
            dict(row) for row in rows
            if self._age_gate_allows_window(
                row["first_seen_at"], row["token_created_at"],
                row["token_created_at_observed_at"],
            )
        ]

    def assert_evm_done_tokens(
        self, network_id: str, expected_tokens: Sequence[str],
    ) -> None:
        current = sorted(
            str(row["token_address"]).lower()
            for row in self.evm_watched([network_id])
            if (row.get("backfill_status") or "") == "done"
        )
        expected = sorted(str(token).lower() for token in expected_tokens)
        if current != expected:
            raise StaleEVMState(
                f"completed EVM ledger set [{network_id}] changed while fetching"
            )

    def evm_replay_windows(
        self, token_address: str, network_id: str,
    ) -> list[dict[str, str]]:
        rows = self._conn.execute(
            """SELECT w.first_seen_at, w.watch_until, s.token_created_at,
                      s.token_created_at_observed_at
                 FROM watch_windows w
                 LEFT JOIN token_static s
                   ON CASE WHEN s.network_id IN ('56','143','4663','8453')
                           THEN lower(s.token_address)=lower(w.token_address)
                           ELSE s.token_address=w.token_address END
                  AND s.network_id=w.network_id
                  AND s.recorded_at <= w.first_seen_at
                WHERE w.token_address=? AND w.network_id=?
                ORDER BY w.first_seen_at""",
            (token_address.lower(), str(network_id)),
        ).fetchall()
        return [
            {"first_seen_at": str(row["first_seen_at"]),
             "watch_until": str(row["watch_until"])}
            for row in rows
            if self._age_gate_allows_window(
                row["first_seen_at"], row["token_created_at"],
                row["token_created_at_observed_at"],
            )
        ]

    def assert_evm_replay_windows(
        self, token_address: str, network_id: str,
        expected_windows: Sequence[Mapping[str, Any]],
    ) -> None:
        expected = sorted(
            (str(window["first_seen_at"]), str(window["watch_until"]))
            for window in expected_windows
        )
        current = sorted(
            (window["first_seen_at"], window["watch_until"])
            for window in self.evm_replay_windows(token_address, network_id)
        )
        if current != expected:
            raise StaleEVMState(
                f"replay windows for token {token_address} changed while fetching"
            )

    def evm_training_rebuild_state(self) -> tuple[int, str, str]:
        return (
            self.evm_ledger_generation(),
            self.get_meta("evm_ledger_rebuild_required") or "0",
            self.get_meta("evm_training_rebuild_started") or "0",
        )

    def assert_evm_training_rebuild_state(
        self, expected: tuple[int, str, str],
    ) -> None:
        if self.evm_training_rebuild_state() != expected:
            raise StaleEVMState("training rebuild state changed while computing the batch")

    # --- Retroactive replay (evm_replay) ---
    def evm_replay_targets(self, networks: Sequence[str]) -> list[dict[str, Any]]:
        """Every watch window on the replay networks — **ended and active together**.

        `active=1` is unconditional here, unlike `evm_watched`: an ended token is
        exactly one we missed measuring (the EVM layer was born after it), and its
        training rows exist waiting for their columns. An empty network list returns nothing.

        Oldest first: those are the furthest from live coverage, so they never compete with it.
        """
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.watch_until, w.entry_signal_id, w.is_control,
                       COALESCE(l.active, 0) AS active,
                       s.token_created_at, s.token_created_at_observed_at,
                       r.status AS replay_status, r.last_try_at AS replay_last_try_at,
                       r.snapshots AS replay_snapshots,
                       r.from_block AS replay_from_block,
                       r.to_block AS replay_to_block,
                       r.transfers AS replay_transfers,
                       r.calls AS replay_calls,
                       r.checkpoint_json AS replay_checkpoint_json,
                       r.revision AS replay_revision
                  FROM watch_windows w
                  LEFT JOIN watchlist l
                    ON l.token_address=w.token_address AND l.network_id=w.network_id
                  LEFT JOIN token_static s
                    ON CASE WHEN s.network_id IN ('56','143','4663','8453')
                            THEN lower(s.token_address)=lower(w.token_address)
                            ELSE s.token_address=w.token_address END
                   AND s.network_id=w.network_id
                   AND s.recorded_at <= w.first_seen_at
                  LEFT JOIN evm_replay_state r
                    ON r.token_address=w.token_address AND r.network_id=w.network_id
                 WHERE w.network_id IN ({marks})
                 ORDER BY w.first_seen_at""",
            nets,
        ).fetchall()
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in rows:
            if not self._age_gate_allows_window(
                raw["first_seen_at"], raw["token_created_at"],
                raw["token_created_at_observed_at"],
            ):
                continue
            row = dict(raw)
            key = (str(row["token_address"]).lower(), str(row["network_id"]))
            current = grouped.get(key)
            window = {
                "first_seen_at": row["first_seen_at"],
                "watch_until": row["watch_until"],
            }
            if current is None:
                row["replay_windows"] = [window]
                grouped[key] = row
                continue
            current["replay_windows"].append(window)
            current["first_seen_at"] = min(
                current["first_seen_at"], row["first_seen_at"]
            )
            current["watch_until"] = max(current["watch_until"], row["watch_until"])
            current["is_control"] = min(
                int(current.get("is_control") or 0), int(row.get("is_control") or 0)
            )
        return list(grouped.values())

    def evm_replay_state(
        self, token_address: str, network_id: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM evm_replay_state
                WHERE token_address=? AND network_id=?""",
            (token_address.lower(), str(network_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_evm_replay_state(
        self, token_address: str, network_id: str, status: str, now_iso: str,
        from_block: int | None = None, to_block: int | None = None,
        transfers: int | None = None, snapshots: int | None = None,
        calls: int | None = None, balance_check: str | None = None,
        last_error: str | None = None, checkpoint: Any = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO evm_replay_state(
                   token_address, network_id, status, from_block, to_block,
                   transfers, snapshots, calls, balance_check, last_try_at,
                   last_error, checkpoint_json, revision)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   status        = excluded.status,
                   from_block    = COALESCE(excluded.from_block,
                                            evm_replay_state.from_block),
                   to_block      = COALESCE(excluded.to_block,
                                            evm_replay_state.to_block),
                   transfers     = COALESCE(excluded.transfers,
                                            evm_replay_state.transfers),
                   snapshots     = COALESCE(excluded.snapshots,
                                            evm_replay_state.snapshots),
                   calls         = COALESCE(excluded.calls, evm_replay_state.calls),
                   balance_check = COALESCE(excluded.balance_check,
                                            evm_replay_state.balance_check),
                   last_try_at   = excluded.last_try_at,
                   last_error    = excluded.last_error,
                   checkpoint_json = excluded.checkpoint_json,
                   revision = evm_replay_state.revision + 1""",
            (token_address.lower(), str(network_id), status, from_block, to_block,
             transfers, snapshots, calls, balance_check, now_iso,
             english_note(last_error),
             encode_raw(checkpoint) if checkpoint is not None else None),
        )
        self._commit()

    def mark_evm_replay_error(
        self, token_address: str, network_id: str, now_iso: str, error: str,
    ) -> None:
        """Records a transient error while keeping the checkpoint and previous resume point."""
        self._conn.execute(
            """INSERT INTO evm_replay_state(
                   token_address, network_id, status, last_try_at, last_error)
               VALUES(?, ?, 'error', ?, ?)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   status='error', last_try_at=excluded.last_try_at,
                   last_error=excluded.last_error,
                   revision=evm_replay_state.revision + 1""",
            (token_address.lower(), str(network_id), now_iso, english_note(error)),
        )
        self._commit()

    def stale_evm_replay_verdict(self, token_address: str, network_id: str) -> None:
        """A new window voids the verdict alone — the walk stays.

        This used to be a deletion of the whole row, and the difference between deleting
        and this is the difference between two truths conflated into one column:
        `status` answers "did I cover all the token's windows?" and is indeed voided by
        a new window, whereas `from_block`/`to_block`/`checkpoint_json` answer "how far
        did my walk get in the chain?" — about blocks, not windows, so an added window does not void them. Deletion threw the second away with the first.

        And its price was measured, not guessed: a hot token is signalled every few
        minutes, so its state row was deleted faster than it could be written — one on
        Base has 522 windows, another 1440 — so the replay restarted from genesis every
        time and never reached the first snapshot. That is why 51 tokens on Base were
        `partial` with revision = 1 and span = -1: one attempt after each deletion, no progress, forever.

        Worse, the deletion was defeating the very guard written for exactly this: the
        `EVM_REPLAY_TOKEN_CALL_CAP` cap is accumulated from `calls` in the row, so
        erasing the row zeroes the counter — a token that never completes never reaches
        its cap. Keeping the row lets the counter accumulate, so it is moved to `budget` and out of the way.

        And `window` is not in `FINAL_STATUSES`, so scheduling does not change: it is
        picked as it was picked while it had no row. It is in the checkpoint-resume
        set in `evm_replay._replay_token` — otherwise the row is read and its walk discarded.

        And `budget` alone is exempted. It is not a verdict on coverage but a decision
        to stop spending: the token reached `EVM_REPLAY_TOKEN_CALL_CAP` and stopped
        with a saved resume point. Voiding its verdict returns it to the queue, it is
        picked, walks a stretch, and the cap sends it back to `budget` — at the cost
        of a full selection pass per window. And a token that reaches the cap is by
        nature the hot token with hundreds of windows (522 for one Base token), so
        that slowly re-defeats the cap — exactly what this change restored. So
        `budget` stays final until the cap is raised or `--redo` is requested, which is what the cap's comment in `config` says anyway.

        And the revision is incremented: a running job holding an older snapshot falls
        into `StaleEVMState` and is left for the next cycle, instead of writing over a
        window it never saw. And no `_commit` here: the call comes from inside the insert transaction, which owns the commit.
        """
        self._conn.execute(
            """UPDATE evm_replay_state
                  SET status = 'window', revision = revision + 1
                WHERE token_address = ? AND network_id = ?
                  AND status <> 'budget'""",
            (token_address.lower(), str(network_id)),
        )

    def reset_evm_replay_token(self, token_address: str, network_id: str) -> None:
        """Erases a single token's replay rows and state so that `--redo` is a true redo."""
        token, net = token_address.lower(), str(network_id)
        with self.batch():
            self._conn.execute(
                """DELETE FROM chain_concentration
                    WHERE token_address=? AND network_id=? AND is_replay=1""",
                (token, net),
            )
            self._conn.execute(
                "DELETE FROM evm_replay_state WHERE token_address=? AND network_id=?",
                (token, net),
            )

    def restart_evm_backfill_from(
        self, token_address: str, network_id: str, from_block: int, now_iso: str,
    ) -> None:
        """Reopens a token's EVM ledger backfill from a new starting block.

        For tokens stuck in `partial` with an impossible range (tens of millions of
        blocks): erases the old partial ledger (its balances were built over a range
        that will be re-read) and resets the state to `retry` from `from_block`.
        Existing training rows are untouched — the ledger is cumulative above the new point and new rows are built on top of the present.
        """
        token, net = token_address.lower(), str(network_id)
        with self.batch():
            self._conn.execute(
                "DELETE FROM evm_balances WHERE token_address=? AND network_id=?",
                (token, net),
            )
            self._conn.execute(
                """INSERT INTO evm_backfill_state(
                       network_id, token_address, status, from_block, to_block,
                       transfers, calls, last_try_at, last_error)
                   VALUES(?,?, 'retry', ?, NULL, 0, NULL, ?, NULL)
                   ON CONFLICT(network_id, token_address) DO UPDATE SET
                       status='retry', from_block=excluded.from_block,
                       to_block=NULL, transfers=0, calls=NULL,
                       last_try_at=excluded.last_try_at, last_error=NULL""",
                (net, token, int(from_block), now_iso),
            )

    def assert_evm_replay_state(
        self, token_address: str, network_id: str,
        expected_status: str | None, expected_from: int | None,
        expected_checkpoint: Any, expected_revision: int | None,
    ) -> None:
        row = self.evm_replay_state(token_address, network_id)
        current = None if row is None else (
            row.get("status"), row.get("from_block"), row.get("checkpoint_json"),
            row.get("revision"),
        )
        expected = (
            None if expected_status is None and expected_from is None
            and expected_checkpoint is None
            else (
                expected_status, expected_from, expected_checkpoint,
                expected_revision,
            )
        )
        if current != expected:
            raise StaleEVMState(
                f"replay state for token {token_address} changed while fetching"
            )

    def delete_evm_replay_rows(self, token_address: str, network_id: str) -> int:
        """Deletes the rows derived by this replay only, on late corruption detection."""
        cur = self._conn.execute(
            """DELETE FROM chain_concentration
                WHERE token_address=? AND network_id=? AND is_replay=1""",
            (token_address.lower(), str(network_id)),
        )
        self._commit()
        return cur.rowcount

    def add_block_anchor(
        self, network_id: str, block_number: int, block_ts: int, now_iso: str,
    ) -> None:
        """A time↔block anchor. `OR IGNORE`: a block's timestamp never changes."""
        self._conn.execute(
            """INSERT OR IGNORE INTO evm_block_time(
                   network_id, block_number, block_ts, fetched_at)
               VALUES(?, ?, ?, ?)""",
            (str(network_id), int(block_number), int(block_ts), now_iso),
        )
        self._commit()

    def block_anchors(
        self, network_id: str, from_block: int | None = None,
        to_block: int | None = None,
    ) -> list[tuple[int, int]]:
        """Anchors ordered by block, with one anchor **outside** each end when one exists.

        Both ends are deliberate: interpolation needs an anchor on each side of the
        requested block, and clipping the list to exactly the range leaves its ends
        without a side, turning interpolation into extrapolation — the wider error.
        """
        net = str(network_id)
        if from_block is None or to_block is None:
            rows = self._conn.execute(
                """SELECT block_number, block_ts FROM evm_block_time
                    WHERE network_id=? ORDER BY block_number""", (net,),
            ).fetchall()
            return [(int(r["block_number"]), int(r["block_ts"])) for r in rows]
        lo, hi = int(from_block), int(to_block)
        out: set[tuple[int, int]] = set()
        for sql, params in (
            ("""SELECT block_number, block_ts FROM evm_block_time
                 WHERE network_id=? AND block_number BETWEEN ? AND ?""",
             (net, lo, hi)),
            ("""SELECT block_number, block_ts FROM evm_block_time
                 WHERE network_id=? AND block_number < ?
                 ORDER BY block_number DESC LIMIT 1""", (net, lo)),
            ("""SELECT block_number, block_ts FROM evm_block_time
                 WHERE network_id=? AND block_number > ?
                 ORDER BY block_number LIMIT 1""", (net, hi)),
        ):
            for r in self._conn.execute(sql, params).fetchall():
                out.add((int(r["block_number"]), int(r["block_ts"])))
        return sorted(out)

    def chain_first_recorded_at(
        self, token_address: str, network_id: str, live_only: bool = True,
    ) -> str | None:
        """The earliest concentration snapshot that exists for this token — the replay's upper bound.

        The replay stops where live coverage begins: two measurements of the same
        moment by different routes (live with confirmation lag, replayed at the exact
        block) differ slightly, and interleaving them in one series creates phantom five-minute gaps.
        """
        sql = """SELECT MIN(recorded_at) m FROM chain_concentration
                  WHERE token_address=? AND network_id=?"""
        if live_only:
            sql += " AND COALESCE(is_replay, 0)=0"
        row = self._conn.execute(
            sql, (token_address.lower(), str(network_id)),
        ).fetchone()
        return row["m"] if row is not None and row["m"] else None

    def chain_live_coverage(
        self, token_address: str, network_id: str,
    ) -> dict[str, str]:
        rows = self._conn.execute(
            """SELECT watch_first_seen_at, MIN(recorded_at) AS first_recorded_at
                  FROM chain_concentration
                 WHERE token_address=? AND network_id=?
                   AND COALESCE(is_replay, 0)=0
                 GROUP BY watch_first_seen_at""",
            (token_address.lower(), str(network_id)),
        ).fetchall()
        return {
            str(row["watch_first_seen_at"]): str(row["first_recorded_at"])
            for row in rows if row["first_recorded_at"]
        }

    def assert_chain_live_coverage(
        self, token_address: str, network_id: str,
        expected: Mapping[str, str],
    ) -> None:
        if self.chain_live_coverage(token_address, network_id) != dict(expected):
            raise StaleEVMState(
                f"live coverage for token {token_address} changed while fetching"
            )

    # --- evm_contract (contract safety — Base only) ---
    def insert_evm_contract(self, row: Mapping[str, Any]) -> bool:
        cols = _EVM_CONTRACT_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO evm_contract({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    def set_evm_contract_state(
        self, token_address: str, network_id: str, status: str, now_iso: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO evm_contract_state(
                   token_address, network_id, last_fetch_at, last_status, attempts)
               VALUES(?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   attempts      = evm_contract_state.attempts + 1""",
            (token_address, network_id, now_iso, status),
        )
        self._commit()

    def evm_contract_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
        networks: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Same logic as `chain_auth_due` on the contract state table."""
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.entry_signal_id, w.is_control, s.last_fetch_at, s.last_status
                  FROM watchlist w
                  LEFT JOIN evm_contract_state s
                    ON s.token_address=w.token_address AND s.network_id=w.network_id
                 WHERE w.active=1
                   AND w.network_id IN ({marks})
                   AND (s.last_fetch_at IS NULL
                        OR (s.last_status='error' AND s.last_fetch_at < ?)
                        OR (COALESCE(s.last_status, '') <> 'error'
                            AND s.last_fetch_at < ?))
                 ORDER BY s.last_fetch_at IS NOT NULL,
                          w.is_control,
                          s.last_fetch_at,
                          w.first_seen_at DESC
                 LIMIT ?""",
            (*nets, error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- watchlist ---
    def upsert_watch(
        self,
        token_address: str,
        network_id: str,
        source: str,
        entry_signal_id: str | None,
        watch_hours: int,
        now_iso: str | None = None,
        admission_price_usd: float | None = None,
    ) -> bool:
        """Admits a token to watching, or re-activates a token whose window ended.

        Returns True if the token entered watching now (new or re-activated).

        - A token already **active**: nothing — we do not extend its window (the
          first appearance stays the reference).
        - A token **deactivated** (active=0 after 48h): re-admitted with a new window
          and a new entry signal. Without this branch the old row stays and INSERT OR IGNORE swallows every later signal forever, so the list bleeds to zero and the ticks stop.
        """
        now = now_iso or utcnow_iso()
        until = (datetime.fromisoformat(now) + timedelta(hours=watch_hours)).isoformat()
        cur = self._conn.execute(
            """INSERT INTO watchlist(
                token_address, network_id, first_seen_at, source,
                watch_until, entry_signal_id, active, is_control
            ) VALUES(?, ?, ?, ?, ?, ?, 1, 0)
            ON CONFLICT(token_address, network_id) DO UPDATE SET
                first_seen_at   = excluded.first_seen_at,
                source          = excluded.source,
                watch_until     = excluded.watch_until,
                entry_signal_id = excluded.entry_signal_id,
                active          = 1,
                is_control      = 0
            WHERE watchlist.active = 0 OR watchlist.is_control = 1""",
            (token_address, network_id, now, source, until, entry_signal_id),
        )
        if cur.rowcount > 0:
            # A new window needs a fresh fetch cycle; the previous window's
            # no_data/freshness state must not keep it out of scheduling.
            self._conn.execute(
                "DELETE FROM bars_fetch_state WHERE token_address=? AND network_id=?",
                (token_address, network_id),
            )
            # Re-activation can arrive after a gap during which the EVM ledger went
            # unwatched. A short gap (≤ EVM_REACTIVATION_KEEP_LEDGER_SECONDS, 2026-08-29)
            # keeps the ledger and the resume point and only widens to_block: erasing
            # it zeroed the whole backfill progress, so queues were rebuilt from
            # scratch forever (measured 08-29: 43 tokens on Robinhood went back to
            # zero by this path). A longer gap rebuilds from genesis as before — the
            # old final state proves no coverage of a long gap, and accepting an incomplete balance is worse than rebuilding.
            if self._reactivation_gap_exceeds_keep(
                token_address, network_id, now,
            ):
                self._conn.execute(
                    "DELETE FROM evm_balances WHERE token_address=? AND network_id=?",
                    (token_address.lower(), str(network_id)),
                )
                self._conn.execute(
                    "DELETE FROM evm_backfill_state WHERE token_address=? AND network_id=?",
                    (token_address.lower(), str(network_id)),
                )
                self._conn.execute(
                    "DELETE FROM evm_replay_state WHERE token_address=? AND network_id=?",
                    (token_address.lower(), str(network_id)),
                )
            else:
                # The ledger stays: backfill resumes from its point and the gap is
                # caught up through the widened `to_block` (step 2 in evm_layer reads
                # up to the cycle's new cursor for partial tokens before declaring them done).
                self._conn.execute(
                    """UPDATE evm_backfill_state
                          SET to_block = COALESCE(to_block, 0),
                              status = CASE WHEN status = 'done'
                                            THEN 'partial' ELSE status END
                        WHERE token_address=? AND network_id=?""",
                    (token_address.lower(), str(network_id)),
                )
            self._insert_watch_window(
                token_address, network_id, now, source, until, entry_signal_id, 0,
                admission_price_usd,
            )
        self._commit()
        return cur.rowcount > 0

    def _reactivation_gap_exceeds_keep(
        self, token_address: str, network_id: str, now_iso: str,
    ) -> bool:
        """Is the gap since the previous window ended longer than the ledger-keep cap?

        The last **ended** window (the oldest `watch_until` in watch_windows) is
        compared against the re-activation time. Missing previous windows or a
        failed time parse means "unknown gap" ⇒ rebuild (safety first). And it is
        read from `watch_windows`, not watchlist, because `watch_until` there is
        overwritten the moment re-activation happens, leaving no trace of the ended window.
        """
        try:
            import config
            keep = float(config.EVM_REACTIVATION_KEEP_LEDGER_SECONDS or 0)
        except (ImportError, TypeError, ValueError):
            keep = 0.0
        if keep <= 0:
            return True  # cap disabled: the old behavior (full delete)
        row = self._conn.execute(
            """SELECT MIN(watch_until) AS oldest_end
                 FROM watch_windows
                WHERE token_address=? AND network_id=?""",
            (token_address, str(network_id)),
        ).fetchone()
        if row is None or row["oldest_end"] is None:
            return True
        try:
            gap = (
                datetime.fromisoformat(now_iso)
                - datetime.fromisoformat(row["oldest_end"])
            ).total_seconds()
        except (TypeError, ValueError):
            return True
        return gap > keep

    def admit_control(
        self,
        token_address: str,
        network_id: str,
        watch_hours: int,
        now_iso: str | None = None,
        admission_price_usd: float | None = None,
        source: str = "control",
        design_version: int = 2,
        admission_source: str | None = None,
    ) -> bool:
        """Admits a **control** token (no signal). Returns True if added.

        It never touches an existing row: a token a signal pointed at must never be
        demoted to control, and an active control token does not get its window reset.
        Promotion to "signalled" happens only in the other direction, via upsert_watch.
        """
        now = now_iso or utcnow_iso()
        until = (datetime.fromisoformat(now) + timedelta(hours=watch_hours)).isoformat()
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO watchlist(
                token_address, network_id, first_seen_at, source,
                watch_until, entry_signal_id, active, is_control
            ) VALUES(?, ?, ?, ?, ?, NULL, 1, 1)""",
            (token_address, network_id, now, source, until),
        )
        if cur.rowcount > 0:
            self._insert_watch_window(
                token_address, network_id, now, source, until, None, 1,
                admission_price_usd, design_version, admission_source,
            )
        self._commit()
        return cur.rowcount > 0

    def add_signal_comparison_window(
        self,
        token_address: str,
        network_id: str,
        source: str,
        entry_signal_id: str,
        watch_hours: int,
        now_iso: str,
        admission_price_usd: float,
        design_version: int,
        admission_source: str | None = None,
    ) -> bool:
        """Adds an independent comparison window for a signal from the control's own cohort.

        It does not change `watchlist`: operational recording began when the signal
        arrived, while this window documents the moment the token appeared in trending/verified and its observable price.
        """
        until = (
            datetime.fromisoformat(now_iso) + timedelta(hours=watch_hours)
        ).isoformat()
        existing = self._conn.execute(
            """SELECT token_address, network_id, first_seen_at
                 FROM watch_windows
                WHERE token_address=? AND network_id=? AND first_seen_at=?""",
            (token_address, network_id, now_iso),
        ).fetchone()
        if existing is not None:
            self._conn.execute(
                """DELETE FROM watch_windows
                    WHERE token_address=? AND network_id=? AND first_seen_at=?""",
                (token_address, network_id, now_iso),
            )
        self._insert_watch_window(
            token_address, network_id, now_iso, source, until, entry_signal_id,
            0, admission_price_usd, design_version, admission_source,
        )
        self._commit()
        return True

    def _insert_watch_window(
        self,
        token_address: str,
        network_id: str,
        first_seen_at: str,
        source: str,
        watch_until: str,
        entry_signal_id: str | None,
        is_control: int,
        admission_price_usd: float | None,
        design_version: int = 2,
        admission_source: str | None = None,
    ) -> None:
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO watch_windows(
                   token_address, network_id, first_seen_at, source,
                   watch_until, entry_signal_id, is_control, admission_price_usd,
                   design_version, admission_source
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (token_address, network_id, first_seen_at, source,
             watch_until, entry_signal_id, is_control, admission_price_usd,
             design_version, admission_source),
        )
        if cur.rowcount > 0:
            # Replay state concerns the union of the token's windows. Adding a window
            # makes any earlier final verdict stale; the previous rows stay correct,
            # and only the verdict is voided so the next run adds the new window's points with no gap and no history loss.
            self.stale_evm_replay_verdict(token_address, network_id)

    def known_tokens(self) -> set[tuple[str, str]]:
        """Every token that has ever entered (signalled or control, active or ended).

        Used to exclude candidates: we never admit a control token we have seen before.
        """
        return {
            (r["token_address"], str(r["network_id"] or ""))
            for r in self._conn.execute("SELECT token_address, network_id FROM watchlist")
        }

    def signalled_tokens(self) -> set[str]:
        """Addresses of every token a signal event has mentioned — even if it never entered watching.

        A control candidate must never have been signalled at all, or it is no longer a control.
        """
        return {
            r["token_address"]
            for r in self._conn.execute("SELECT DISTINCT token_address FROM signal_events")
        }

    def active_watch_count(self, is_control: int | None = None) -> int:
        """Count of active tokens. `is_control=None` includes everyone; 0 signalled; 1 control.

        The cap (`WATCHLIST_CAP`) applies to the signalled alone, or the controls
        would compete with them for their seats.
        """
        if is_control is None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM watchlist WHERE active=1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM watchlist WHERE active=1 AND is_control=?",
                (is_control,),
            ).fetchone()
        return int(row["n"])

    def active_watches(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT token_address, network_id, source, watch_until, first_seen_at "
            "FROM watchlist WHERE active=1"
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _age_gate_allows_window(
        first_seen_at: Any, token_created_at: Any,
        token_created_at_observed_at: Any = None,
    ) -> bool:
        """Return whether a post-gate window may consume worker RPC budget."""
        from config import AGE_GATE_ENABLED_AT, MIN_TOKEN_AGE_DAYS

        if not MIN_TOKEN_AGE_DAYS:
            return True
        from features import epoch_of

        entry = epoch_of(first_seen_at)
        gate = epoch_of(AGE_GATE_ENABLED_AT)
        if entry is None or gate is None or entry < gate:
            return True
        observed = epoch_of(token_created_at_observed_at)
        if observed is None or observed > entry:
            return False
        created = epoch_of(token_created_at)
        return (
            created is not None
            and entry >= created
            and entry - created >= float(MIN_TOKEN_AGE_DAYS) * 86400.0
        )

    def quarantine_age_invalid_active(
        self, now_iso: str, min_age_days: float, *, since_iso: str,
    ) -> int:
        """Deactivate active rows that violated the gate; preserve history."""
        if min_age_days <= 0:
            return 0
        from features import epoch_of

        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, w.first_seen_at,
                      s.token_created_at, s.token_created_at_observed_at
                 FROM watchlist w
                 LEFT JOIN token_static s
                   ON CASE WHEN s.network_id IN ('56','143','4663','8453')
                           THEN lower(s.token_address)=lower(w.token_address)
                           ELSE s.token_address=w.token_address END
                  AND s.network_id=w.network_id
                  AND s.recorded_at <= w.first_seen_at
                WHERE w.active=1""",
        ).fetchall()
        gate = epoch_of(since_iso)
        minimum_seconds = float(min_age_days) * 86400.0
        invalid: list[tuple[str, str]] = []
        for row in rows:
            entry = epoch_of(row["first_seen_at"])
            observed = epoch_of(row["token_created_at_observed_at"])
            created = epoch_of(row["token_created_at"])
            if (
                entry is None
                or gate is None
                or entry < gate
            ):
                continue
            if (
                observed is None
                or observed > entry
                or created is None
                or entry < created
                or entry - created < minimum_seconds
            ):
                invalid.append((row["token_address"], row["network_id"]))
        if not invalid:
            return 0
        self._conn.executemany(
            """UPDATE watchlist SET active=0
                WHERE token_address=? AND network_id=? AND active=1""",
            invalid,
        )
        self._commit()
        return len(invalid)

    def deactivate_expired(self, now_iso: str | None = None) -> int:
        """Deactivates the window after its final candle fetch completes, not at hour 48 sharp."""
        now = now_iso or utcnow_iso()
        cur = self._conn.execute(
            """UPDATE watchlist SET active=0
                WHERE active=1 AND watch_until <= ?
                  AND EXISTS (
                      SELECT 1 FROM bars_fetch_state s
                       WHERE s.token_address = watchlist.token_address
                         AND s.network_id = watchlist.network_id
                         AND ((s.last_status='ok' AND s.last_fetch_at >= watchlist.watch_until)
                              OR (s.last_status='no_data' AND s.attempts >= 3))
                  )""",
            (now,),
        )
        self._commit()
        return cur.rowcount


    # --- outcomes (written by the labeler, a separate process) ---
    def insert_outcome(self, row: Mapping[str, Any]) -> bool:
        """Inserts a labeled outcome. Idempotent by (kind, key) — a label is written
        once and never revised (the window is complete and history does not change)."""
        cols = _OUTCOME_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO outcomes({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        values = {c: row.get(c) for c in cols}
        if values["status"] == "ok":
            required = ("final_return_48h", "max_gain_24h", "is_rug")
            if any(values[field] is None for field in required):
                values["status"] = "incomplete"
                values["analysis_eligible"] = 0
                values["exclusion_reason"] = "incomplete_metrics"
        if values["design_version"] is None:
            values["design_version"] = 1
        if values["analysis_eligible"] is None:
            values["analysis_eligible"] = 0
        if values["exclusion_reason"] is None and values["analysis_eligible"] == 0:
            values["exclusion_reason"] = "legacy_or_non_phase1_outcome"
        cur = self._conn.execute(sql, values)
        self._commit()
        return cur.rowcount > 0

    def signals_pending_label(self, mature_before_epoch: int, limit: int) -> list[dict[str, Any]]:
        """Signals whose window has matured and are still unlabeled — oldest first.

        The decision time is `recorded_at`: the first moment the signal became
        available to the system. Using the original `ts` makes an entry appear
        before a late-arriving event, by up to two hours. `prev_ts` = arrival time of the previous decision on the same token, for computing independence.
        """
        rows = self._conn.execute(
            """SELECT s.id, s.token_address, s.network_id, s.signal_type,
                      s.ts AS source_ts, s.recorded_at,
                      CAST(strftime('%s', s.recorded_at) AS INTEGER) AS entry_epoch,
                      (SELECT MAX(p.recorded_at) FROM signal_events p
                        WHERE p.token_address = s.token_address
                          AND p.network_id = s.network_id
                          AND p.recorded_at < s.recorded_at)
                        AS prev_ts
               FROM signal_events s
               WHERE s.recorded_at IS NOT NULL
                  AND CAST(strftime('%s', s.recorded_at) AS INTEGER) <= ?
                  AND NOT EXISTS (SELECT 1 FROM outcomes o
                                   WHERE o.kind = 'signal' AND o.key = s.id)
               ORDER BY s.recorded_at LIMIT ?""",
            (mature_before_epoch, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def watches_pending_label(self, mature_before_epoch: int, limit: int) -> list[dict[str, Any]]:
        """Watch entries (signal and control) whose window matured and are unlabeled —
        for comparison over completed windows rather than the running one."""
        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, w.source, w.is_control,
                      w.first_seen_at, w.admission_price_usd, w.design_version,
                      ts.token_created_at,
                      ts.token_created_at_observed_at,
                      CAST(strftime('%s', w.first_seen_at) AS INTEGER) AS entry_epoch,
                      w.token_address || ':' || w.network_id || ':' || w.first_seen_at
                        AS key
                 FROM watch_windows w
                 LEFT JOIN token_static ts
                   ON CASE WHEN ts.network_id IN ('56','143','4663','8453')
                           THEN lower(ts.token_address)=lower(w.token_address)
                           ELSE ts.token_address=w.token_address END
                  AND ts.network_id=w.network_id
                  AND ts.recorded_at <= w.first_seen_at
                WHERE CAST(strftime('%s', w.first_seen_at) AS INTEGER) <= ?
                  AND EXISTS (
                      SELECT 1 FROM bars_fetch_state s
                       WHERE s.token_address = w.token_address
                         AND s.network_id = w.network_id
                         AND ((s.last_status = 'ok')
                              OR (s.last_status = 'no_data' AND s.attempts >= 3))
                  )
                  AND NOT EXISTS (SELECT 1 FROM outcomes o
                                   WHERE o.kind = 'watch' AND o.key =
                                     w.token_address || ':' || w.network_id || ':' || w.first_seen_at)
                ORDER BY w.first_seen_at LIMIT ?""",
            (mature_before_epoch, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def bars_for(
        self, token_address: str, network_id: str, from_ts: int, to_ts: int,
        resolution: str = "5",
    ) -> list[dict[str, Any]]:
        """A token's candles within a time range, in order. A missing field drops its
        row — a candle without h/l/c is useless for labeling and we fabricate no values for it.

        `h_suspect`/`l_suspect` are passed through as-is: the labeler excludes the
        flagged tail from the peak/trough and keeps the candle body (o/c) valid — an
        impossible tail from the source (seen ×119 million) was poisoning max_gain with billions of percentage points.
        """
        rows = self._conn.execute(
            """SELECT ts, o, h, l, c, h_suspect, l_suspect, c_suspect FROM token_bars
               WHERE token_address = ? AND network_id = ? AND resolution = ?
                 AND ts >= ? AND ts <= ?
               ORDER BY ts""",
            (token_address, network_id, resolution, from_ts, to_ts),
        ).fetchall()
        return [
            dict(r) for r in rows
            if r["h"] is not None and r["l"] is not None and r["c"] is not None
        ]


def _with_compressed_raw(row: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the row with the raw_json field compressed (if present). Extraction
    yields pure, testable JSON text; compression stays the storage layer's job alone."""
    out = dict(row)
    raw = out.get("raw_json")
    if raw is not None and not isinstance(raw, (bytes, bytearray)):
        out["raw_json"] = encode_raw(raw)
    return out


# Column order for market_ticks and token_static — one source of truth matching schema.sql.
_TICK_COLUMNS = (
    "token_address", "network_id", "recorded_at", "source", "price_usd",
    "liquidity", "market_cap", "holders", "top10_holders_pct",
    "change_5m", "change_1h", "change_4h", "change_12h", "change_24h",
    "volume_5m", "volume_1h", "volume_4h", "volume_12h", "volume_24h",
    "txn_count_1h", "txn_count_4h", "txn_count_12h", "txn_count_24h",
    "buy_count_1h", "buy_count_4h", "buy_count_12h", "buy_count_24h",
    "sell_count_1h", "sell_count_4h", "sell_count_12h", "sell_count_24h",
    "unique_buys_1h", "unique_buys_4h", "unique_buys_12h", "unique_buys_24h",
    "unique_sells_1h", "unique_sells_4h", "unique_sells_12h", "unique_sells_24h",
    "circulating_supply", "total_supply", "raw_json",
)

# Column migrations: (table, column, its definition). Applied once at startup.
_COLUMN_MIGRATIONS = (
    ("watchlist", "is_control", "is_control INTEGER NOT NULL DEFAULT 0"),
    # Trade-size fields — backfilled from raw_json via backfill_sizes.py
    ("signal_events", "size_usd", "size_usd REAL"),
    ("signal_events", "in_amount", "in_amount REAL"),
    ("signal_events", "in_token_address", "in_token_address TEXT"),
    ("signal_events", "out_amount", "out_amount REAL"),
    ("signal_events", "out_token_address", "out_token_address TEXT"),
    ("signal_events", "token_amount", "token_amount REAL"),
    ("signal_events", "realized_pnl_usd", "realized_pnl_usd REAL"),
    # The true thesis count — added after discovering the count saturating at 100
    ("token_social", "thesis_total", "thesis_total INTEGER"),
    ("token_social", "thesis_sampled", "thesis_sampled INTEGER"),
    ("token_social", "has_next_page", "has_next_page INTEGER"),
    # Impossible-tail flags — recomputed retroactively from stored o/h/l/c via
    # backfill_bar_flags.py (no network: the raw suffices).
    ("token_bars", "h_suspect", "h_suspect INTEGER NOT NULL DEFAULT 0"),
    ("token_bars", "l_suspect", "l_suspect INTEGER NOT NULL DEFAULT 0"),
    ("token_bars", "c_suspect", "c_suspect INTEGER NOT NULL DEFAULT 0"),
    ("outcomes", "suspect_bars", "suspect_bars INTEGER"),
    # The live/retro flag — separates the data epoch cleanly for training (project
    # decision: live only), no epoch leakage from the absence pattern.
    ("training_rows", "is_live", "is_live INTEGER NOT NULL DEFAULT 0"),
    ("training_rows", "feature_version", "feature_version INTEGER NOT NULL DEFAULT 1"),
    ("training_rows", "ath_history_complete", "ath_history_complete INTEGER"),
    ("training_rows", "ath_history_days", "ath_history_days REAL"),
    ("training_rows", "pre_signal_runup", "pre_signal_runup REAL"),
    ("outcomes", "is_explosive", "is_explosive INTEGER"),
    ("outcomes", "time_to_plus20_min", "time_to_plus20_min REAL"),
    ("training_rows", "is_explosive", "is_explosive INTEGER"),
    ("training_rows", "time_to_plus20_min", "time_to_plus20_min REAL"),
    ("token_static", "social_channels_dex", "social_channels_dex INTEGER"),
    ("token_static", "social_match_fomo_dex", "social_match_fomo_dex INTEGER"),
    ("training_rows", "social_channels_dex", "social_channels_dex INTEGER"),
    ("training_rows", "social_match_fomo_dex", "social_match_fomo_dex INTEGER"),
    ("watch_windows", "admission_price_usd", "admission_price_usd REAL"),
    ("watch_windows", "admission_source", "admission_source TEXT"),
    ("watch_windows", "design_version", "design_version INTEGER NOT NULL DEFAULT 1"),
    ("outcomes", "design_version", "design_version INTEGER NOT NULL DEFAULT 1"),
    ("outcomes", "analysis_eligible", "analysis_eligible INTEGER NOT NULL DEFAULT 0"),
    ("outcomes", "exclusion_reason", "exclusion_reason TEXT"),
    # Engagement on the signal event — was in the raw (100% coverage) and is not extracted.
    # Backfilled from raw_json via backfill_engagement.py (no network).
    ("signal_events", "likes", "likes INTEGER"),
    ("signal_events", "views", "views INTEGER"),
    ("signal_events", "num_replies", "num_replies INTEGER"),
    ("signal_events", "pinned", "pinned INTEGER"),
    # External legitimacy signals — likewise from the saved raw, no network.
    ("token_static", "exchanges_count", "exchanges_count INTEGER"),
    ("token_static", "exchanges_json", "exchanges_json TEXT"),
    ("token_static", "cmc_id", "cmc_id TEXT"),
    ("token_static", "description", "description TEXT"),
    ("token_static", "description_len", "description_len INTEGER"),
    ("token_static", "has_banner", "has_banner INTEGER"),
    ("token_static", "has_image", "has_image INTEGER"),
    ("token_static", "token_created_at_observed_at",
     "token_created_at_observed_at TEXT"),
    # Platform crowd positioning from /hodlers/top. Added after a live measurement
    # proved the source gives no supply ratios at all (so no top1_pct from it), only
    # fomo users' positions. The table may have been created in the first shape, so the migration completes it without data loss.
    ("token_holders", "platform_holders", "platform_holders INTEGER"),
    ("token_holders", "platform_holders_listed", "platform_holders_listed INTEGER"),
    ("token_holders", "platform_value_usd", "platform_value_usd REAL"),
    ("token_holders", "platform_underwater", "platform_underwater INTEGER"),
    ("token_holders", "platform_median_hold_seconds",
     "platform_median_hold_seconds REAL"),
    ("token_holders", "platform_dev_holding", "platform_dev_holding INTEGER"),
    # Version-4 features on the existing training rows (20,303 rows). Old rows stay
    # NULL here — absent ≠ zero, and the splitter distinguishes feature_version.
    ("training_rows", "exchanges_count", "exchanges_count INTEGER"),
    ("training_rows", "listed_on_exchange", "listed_on_exchange INTEGER"),
    ("training_rows", "has_cmc_id", "has_cmc_id INTEGER"),
    ("training_rows", "description_len", "description_len INTEGER"),
    ("training_rows", "has_banner", "has_banner INTEGER"),
    ("training_rows", "chain_top10_pct", "chain_top10_pct REAL"),
    ("training_rows", "chain_holder_count", "chain_holder_count INTEGER"),
    ("training_rows", "chain_holders_delta_1h", "chain_holders_delta_1h INTEGER"),
    ("training_rows", "chain_holders_growth_1h", "chain_holders_growth_1h REAL"),
    ("training_rows", "chain_holders_span_min", "chain_holders_span_min REAL"),
    ("training_rows", "holders_age_min", "holders_age_min REAL"),
    ("training_rows", "platform_holders", "platform_holders INTEGER"),
    ("training_rows", "platform_penetration", "platform_penetration REAL"),
    ("training_rows", "platform_underwater_ratio", "platform_underwater_ratio REAL"),
    ("training_rows", "platform_value_usd", "platform_value_usd REAL"),
    ("training_rows", "platform_median_hold_h", "platform_median_hold_h REAL"),
    ("training_rows", "platform_dev_holding", "platform_dev_holding INTEGER"),
    # On-chain ownership (v10): it was Solana only, and became both networks in v12 —
    # a balance ledger from Transfer logs (evm_layer) writes into the same chain_concentration.
    ("training_rows", "onchain_top1_pct", "onchain_top1_pct REAL"),
    ("training_rows", "onchain_top5_pct", "onchain_top5_pct REAL"),
    ("training_rows", "onchain_top10_pct", "onchain_top10_pct REAL"),
    ("training_rows", "onchain_top20_pct", "onchain_top20_pct REAL"),
    ("training_rows", "onchain_top_accounts", "onchain_top_accounts INTEGER"),
    ("training_rows", "onchain_age_min", "onchain_age_min REAL"),
    ("training_rows", "onchain_top1_delta_5m", "onchain_top1_delta_5m REAL"),
    ("training_rows", "onchain_top10_delta_5m", "onchain_top10_delta_5m REAL"),
    ("training_rows", "onchain_delta_span_min", "onchain_delta_span_min REAL"),
    # (v12) holder count exact from the ledger — EVM only, and Solana stays NULL since
    # getTokenLargestAccounts returns at most 20 accounts and does not know the total.
    ("training_rows", "onchain_holder_count", "onchain_holder_count INTEGER"),
    ("training_rows", "onchain_holders_delta_5m", "onchain_holders_delta_5m INTEGER"),
    # H2-c) Structural risk from the chain (chain_authority, lazy).
    ("training_rows", "onchain_has_mint_authority", "onchain_has_mint_authority INTEGER"),
    ("training_rows", "onchain_has_freeze_authority", "onchain_has_freeze_authority INTEGER"),
    ("training_rows", "onchain_is_mutable", "onchain_is_mutable INTEGER"),
    ("training_rows", "onchain_is_token2022", "onchain_is_token2022 INTEGER"),
    ("training_rows", "onchain_dev_holding_pct", "onchain_dev_holding_pct REAL"),
    ("training_rows", "onchain_auth_age_min", "onchain_auth_age_min REAL"),
    # H2-d) its EVM counterpart (evm_contract, lazy, Base only): straight from the
    # bytecode — ERC-20 carries no declared authorities, and the marker's presence is the proof.
    ("training_rows", "onchain_code_size", "onchain_code_size INTEGER"),
    ("training_rows", "onchain_function_count", "onchain_function_count INTEGER"),
    ("training_rows", "onchain_is_proxy", "onchain_is_proxy INTEGER"),
    ("training_rows", "onchain_owner_renounced", "onchain_owner_renounced INTEGER"),
    ("training_rows", "onchain_has_mint_fn", "onchain_has_mint_fn INTEGER"),
    ("training_rows", "onchain_has_pause_fn", "onchain_has_pause_fn INTEGER"),
    ("training_rows", "onchain_has_blacklist_fn", "onchain_has_blacklist_fn INTEGER"),
    ("training_rows", "onchain_has_fee_setter", "onchain_has_fee_setter INTEGER"),
    ("training_rows", "onchain_has_limit_setter", "onchain_has_limit_setter INTEGER"),
    ("training_rows", "onchain_has_trading_switch", "onchain_has_trading_switch INTEGER"),
    ("training_rows", "onchain_contract_age_min", "onchain_contract_age_min REAL"),
    # Period leaderboards (v7): the source caps at 50 in the base leaderboard, and every
    # ranking notation is silently ignored, but /24h and /7d and /30d each return 100, so the union of the four is 214 traders
    # (match rate 3.68% → 15.26% over 7,200 events). There is no way to fill the past: the
    # archive saved totalPnL alone, so there is no history of period ranks — old rows rightly stay NULL.
    ("signal_events", "top_trader_match_count_24h", "top_trader_match_count_24h INTEGER"),
    ("signal_events", "buyers_best_rank_24h", "buyers_best_rank_24h INTEGER"),
    ("signal_events", "top_trader_match_count_7d", "top_trader_match_count_7d INTEGER"),
    ("signal_events", "buyers_best_rank_7d", "buyers_best_rank_7d INTEGER"),
    ("signal_events", "top_trader_match_count_30d", "top_trader_match_count_30d INTEGER"),
    ("signal_events", "buyers_best_rank_30d", "buyers_best_rank_30d INTEGER"),
    ("signal_events", "top_trader_periods_matched", "top_trader_periods_matched INTEGER"),
    ("training_rows", "top_trader_match_count_24h", "top_trader_match_count_24h INTEGER"),
    ("training_rows", "buyers_best_rank_24h", "buyers_best_rank_24h INTEGER"),
    ("training_rows", "top_trader_match_count_7d", "top_trader_match_count_7d INTEGER"),
    ("training_rows", "buyers_best_rank_7d", "buyers_best_rank_7d INTEGER"),
    ("training_rows", "top_trader_match_count_30d", "top_trader_match_count_30d INTEGER"),
    ("training_rows", "buyers_best_rank_30d", "buyers_best_rank_30d INTEGER"),
    ("training_rows", "top_trader_periods_matched", "top_trader_periods_matched INTEGER"),
    ("training_rows", "top_trader_any_period", "top_trader_any_period INTEGER"),
    ("training_rows", "best_rank_any_period", "best_rank_any_period INTEGER"),
    # v8 — closing the collection gap. Three things were available and not collected:
    # 1) Pool protocol: absent from raw trending (zero of 3,000) and coming from
    #    filterTokens alone. Existing rows are filled when we see it later (set_static_protocol).
    ("token_static", "dex_protocol", "dex_protocol TEXT"),
    # 2) Event tag: body.tag with the single value 'Top Trader' in 3.4% of 4,000 events ⇒
    #    presence is the information. Old rows stay NULL (the raw keeps them if filling is ever wanted).
    ("signal_events", "is_top_trader_tagged", "is_top_trader_tagged INTEGER"),
    # 3) Flow and aggregation features on training rows — from tokenDetails already fetched.
    #    All NULL before collection began (FR-007: "not measured" is not "zero").
    ("training_rows", "tick_rich_age_min", "tick_rich_age_min REAL"),
    ("training_rows", "flow_age_min", "flow_age_min REAL"),
    ("training_rows", "flow_buy_volume_5m", "flow_buy_volume_5m REAL"),
    ("training_rows", "flow_sell_volume_5m", "flow_sell_volume_5m REAL"),
    ("training_rows", "flow_net_volume_5m", "flow_net_volume_5m REAL"),
    ("training_rows", "flow_net_volume_1h", "flow_net_volume_1h REAL"),
    ("training_rows", "flow_net_volume_24h", "flow_net_volume_24h REAL"),
    ("training_rows", "flow_buy_sell_volume_ratio_5m",
     "flow_buy_sell_volume_ratio_5m REAL"),
    ("training_rows", "flow_buy_sell_volume_ratio_1h",
     "flow_buy_sell_volume_ratio_1h REAL"),
    ("training_rows", "flow_buy_sell_volume_ratio_24h",
     "flow_buy_sell_volume_ratio_24h REAL"),
    ("training_rows", "flow_buy_count_5m", "flow_buy_count_5m INTEGER"),
    ("training_rows", "flow_sell_count_5m", "flow_sell_count_5m INTEGER"),
    ("training_rows", "flow_unique_buys_5m", "flow_unique_buys_5m INTEGER"),
    ("training_rows", "flow_unique_sells_5m", "flow_unique_sells_5m INTEGER"),
    ("training_rows", "flow_buy_sell_count_ratio_5m",
     "flow_buy_sell_count_ratio_5m REAL"),
    ("training_rows", "flow_unique_ratio_5m", "flow_unique_ratio_5m REAL"),
    ("training_rows", "flow_trade_size_5m", "flow_trade_size_5m REAL"),
    ("training_rows", "flow_is_low_fees", "flow_is_low_fees INTEGER"),
    # The **exact** holder count from the EVM layer. The live database created
    # chain_concentration before this layer existed, so without this line the column
    # stays missing there and `insert_chain_concentration` raises "no such column".
    # It stays NULL on Solana: `getTokenLargestAccounts` returns at most 20 accounts
    # and does not know the total (measured absence, not zero — FR-007).
    ("chain_concentration", "holder_count", "holder_count INTEGER"),
    # The replayed-row marker (`evm_replay.py`). The live database created the table
    # before replay existed, and `NOT NULL DEFAULT 0` makes all its old rows "live" — which
    # they truly are. Without the column, the model cannot be trained on live-measured data only.
    ("chain_concentration", "is_replay",
     "is_replay INTEGER NOT NULL DEFAULT 0"),
    ("evm_replay_state", "checkpoint_json", "checkpoint_json BLOB"),
    ("evm_replay_state", "revision", "revision INTEGER NOT NULL DEFAULT 0"),
    # Note: `dex_protocol` and `is_top_trader_tagged` are **not** here. They are
    # collection columns on token_static and signal_events (their place is in the
    # migration above), and `build_features` does not produce them; so a column for
    # them in training_rows stays NULL forever — exactly the top10_holders_pct
    # defect (1.43 million empty rows). A training-rows column is added only with a key in ROW_COLUMNS that writes it.
)

_BAR_COLUMNS = (
    "token_address", "network_id", "resolution", "ts", "o", "h", "l", "c", "v",
    "h_suspect", "l_suspect", "c_suspect", "fetched_at",
)

_SIGNAL_COLUMNS = (
    "id", "token_address", "network_id", "ts", "recorded_at", "signal_type",
    "ticker", "price_usd", "fdv", "market_cap", "num_trades", "unique_traders",
    "minutes", "price_change_pct", "total_volume", "are_top_traders",
    "top_trader_ids_json", "top_trader_match_count", "buyers_best_rank",
    "top_trader_match_count_24h", "buyers_best_rank_24h",
    "top_trader_match_count_7d", "buyers_best_rank_7d",
    "top_trader_match_count_30d", "buyers_best_rank_30d",
    "top_trader_periods_matched",
    "buyer_id", "buyer_handle", "num_swaps", "is_first_buy", "buyer_pnl_pct",
    "avg_cost", "size_usd", "in_amount", "in_token_address", "out_amount",
    "out_token_address", "token_amount", "realized_pnl_usd", "likes", "views",
    "num_replies", "pinned", "is_top_trader_tagged", "raw_json",
)

# Column order for token_flow — matches schema.sql (no 12h tier: the source does not provide it).
_FLOW_COLUMNS = (
    "token_address", "network_id", "recorded_at", "watch_first_seen_at",
    "entry_signal_id", "is_control",
    "buy_count_5m", "buy_count_1h", "buy_count_4h", "buy_count_24h",
    "sell_count_5m", "sell_count_1h", "sell_count_4h", "sell_count_24h",
    "buy_volume_5m", "buy_volume_1h", "buy_volume_4h", "buy_volume_24h",
    "sell_volume_5m", "sell_volume_1h", "sell_volume_4h", "sell_volume_24h",
    "unique_buys_5m", "unique_buys_1h", "unique_buys_4h", "unique_buys_24h",
    "unique_sells_5m", "unique_sells_1h", "unique_sells_4h", "unique_sells_24h",
    "is_low_fees", "raw_json",
)

# Column order for traders — matches schema.sql, and matches what /v2/users/{id}
# returns **as measured live** (26 keys): no profit and no win-rate in that reply at all.
_TRADER_COLUMNS = (
    "trader_id", "recorded_at", "handle", "display_name", "followers_count",
    "following_count", "swap_count", "num_trades", "total_volume_usd",
    "avg_hold_seconds", "is_restricted", "is_private", "wallet_address",
    "evm_address", "twitter_url", "created_at", "raw_json",
)

_OUTCOME_COLUMNS = (
    "kind", "key", "token_address", "network_id", "signal_type", "is_control",
    "is_independent", "entry_ts", "entry_px", "entry_lag_s",
    "max_gain_1h", "max_gain_4h", "max_gain_24h", "max_gain_48h",
    "max_drawdown_48h", "final_return_48h", "time_to_peak_h",
    "candles_48h", "suspect_bars", "last_bar_lag_h", "bars_truncated", "is_rug",
    # (fv15) the explosion label and the early-entry hook — the labeler writes both with the rest.
    "is_explosive", "time_to_plus20_min",
    "split", "status", "labeled_at",
    "design_version", "analysis_eligible", "exclusion_reason",
)

_THESIS_COLUMNS = (
    "id", "token_address", "network_id", "created_at", "user_handle", "user_id",
    "num_likes", "num_replies", "equity", "trade_id", "comment", "fetched_at",
    "raw_json",
)

_ACTIVITY_COLUMNS = (
    "id", "event_type", "token_address", "network_id", "ts", "recorded_at",
    "user_id", "user_handle", "trade_id", "usd_amount", "price_usd",
    "market_cap", "fdv", "equity", "num_trades", "unique_traders", "minutes",
    "price_change_pct", "total_volume", "are_top_traders", "top_trader_ids_json",
    "ticker", "raw_json",
)

_SOCIAL_COLUMNS = (
    "token_address", "network_id", "recorded_at",
    "thesis_total", "thesis_sampled", "has_next_page", "thesis_count",
    "thesis_likes", "thesis_replies", "thesis_authors", "holder_authors",
    "newest_thesis_at", "raw_json",
)

_STATIC_COLUMNS = (
    "token_address", "network_id", "recorded_at", "name", "symbol", "decimals",
    "mintable", "freezable", "is_scam", "creator_address", "launchpad_name",
    "migrated", "graduation_percent", "twitter", "telegram", "website", "discord",
    "token_created_at", "token_created_at_observed_at",
    "exchanges_count", "exchanges_json", "cmc_id",
    "description", "description_len", "has_banner", "has_image",
    "dex_protocol", "social_channels_dex", "social_match_fomo_dex", "raw_json",
)

_HOLDERS_COLUMNS = (
    "token_address", "network_id", "recorded_at", "watch_first_seen_at",
    "entry_signal_id", "is_control", "source", "top10_pct", "holder_count",
    "platform_holders", "platform_holders_listed", "platform_value_usd",
    "platform_underwater", "platform_median_hold_seconds",
    "platform_dev_holding", "top_holders_json", "raw_json",
)

# Column order for chain_concentration — matches schema.sql. A separate table from
# token_holders because that one is sourced from FOMO at a 25-minute cadence and mixes
# two populations (the whole chain and platform users), while this is direct on-chain measurement at a 5-minute cadence.
_CHAIN_COLUMNS = (
    "token_address", "network_id", "recorded_at", "watch_first_seen_at",
    "entry_signal_id", "is_control", "supply", "decimals",
    "top1_pct", "top5_pct", "top10_pct", "top20_pct", "holder_count",
    "top_accounts", "is_replay", "raw_json",
)

# Column order for evm_contract — matches schema.sql. Base only is measured: it is
# the only network where contracts actually varied (19 full contracts at sizes 135B–14.8KB),
# while BSC is identical proxies and Robinhood six duplicated templates ⇒ a constant column, no information.
_EVM_CONTRACT_COLUMNS = (
    "token_address", "network_id", "recorded_at", "watch_first_seen_at",
    "entry_signal_id", "is_control", "code_size", "function_count",
    "is_proxy", "impl_address", "code_hash", "owner_address",
    "is_ownership_renounced", "has_mint", "has_pause", "has_blacklist",
    "has_fee_setter", "has_limit_setter", "has_trading_switch", "raw_json",
)

# Column order for chain_authority — matches schema.sql. The slow (lazy) layer:
# mint/freeze authority and `mutable` change once in a lifetime, so there is no point
# asking for them at the concentration cadence.
_CHAIN_AUTH_COLUMNS = (
    "token_address", "network_id", "recorded_at", "watch_first_seen_at",
    "entry_signal_id", "is_control", "token_program", "mint_authority",
    "freeze_authority", "update_authority", "is_mutable", "creator_address",
    "creator_count", "supply", "decimals", "dev_owner", "dev_holding_pct",
    "raw_json",
)
