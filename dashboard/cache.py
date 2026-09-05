"""A cache for heavy queries — stale values are served instantly and refreshed in the background.

The measured problem: `/api/networks` used to eat 2.2 seconds out of the 2.2
seconds of every dashboard refresh, because a single node in it scans
3,073,284 rows of `market_ticks` to produce **five rows**. And the page asks
every ten seconds, which is 22% of a core permanently reserved for a compressed
number whose meaning doesn't change in two minutes.

And why "serve the stale value" instead of settling for a fixed TTL? Because a
naive cache with a 120-second TTL moves the outage, it doesn't remove it:
eleven refreshes get answered from cache, and the twelfth pays the full two
seconds while the browser waits. Here instead, a request never waits after the
first computation: it takes the last known value and returns, and the refresh
runs in a background thread. The accepted cost is that the number may lag the
refresh by a second or two — a number that is two minutes old to begin with.

And three deliberate limits:

- **One computation, not thirteen.** The page fires all its requests in
  parallel, so without a per-key lock the first refresh after boot would start
  the same scan several times at once.
- **A failed refresh never loses the value.** A busy database drops a refresh,
  so we keep the old value, log the error, and back off before retrying —
  otherwise every request became a failed attempt.
- **No writes.** This is the dashboard process's own memory: no staging table,
  no stamp in `meta`, no index being built. The database stays `mode=ro` as it is.

And an entry is never mutated after publication (`frozen`): failure replaces it
with a modified copy, it doesn't edit it in place, so a reader holding a
reference to it never sees a half-applied update.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class _Entry:
    """A computed value and its time. Never mutated after publication — replaced with a modified copy."""

    value: Any
    computed_at: float          # monotonic clock — for age
    computed_wall: float        # wall clock — display only
    error: str | None = None
    failed_at: float | None = None


class _State:
    __slots__ = ("entry", "lock", "refreshing")

    def __init__(self) -> None:
        self.lock = threading.Lock()      # serializes the cold computation only
        self.entry: _Entry | None = None
        self.refreshing = False


def _spawn_thread(run: Callable[[], None]) -> None:
    threading.Thread(target=run, daemon=True).start()


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat()


class TTLMemo:
    """A keyed cache; each key has its own TTL and refresh logic.

    The clocks and the spawner are injectable so that expiry and refresh can be
    tested without real waiting and without thread races: a test that waits two
    seconds to watch the TTL expire is a brittle test, and one that depends on
    thread scheduling fails once in ten.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        spawn: Callable[[Callable[[], None]], None] = _spawn_thread,
        error_backoff: float = 15.0,
        max_entries: int = 16,
    ) -> None:
        self._clock = clock
        self._wall = wall_clock
        self._spawn = spawn
        self._error_backoff = float(error_backoff)
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._states: OrderedDict[str, _State] = OrderedDict()

    # --- Interface ---
    def get(
        self,
        key: str,
        ttl: float,
        compute: Callable[[], Any],
        *,
        error_backoff: float | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Returns (value, freshness description). Waits only if there is no value yet.

        `compute` takes no arguments and opens its own connection: the refresh
        runs in a background thread after the request that triggered it has
        closed its connection, so passing the request's connection would have
        been used after it was closed.
        """
        backoff = self._error_backoff if error_backoff is None else float(error_backoff)
        now = self._clock()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _State()
                self._states[key] = state
            self._states.move_to_end(key)
            entry = state.entry
            if len(self._states) > self._max_entries:
                for old_key, old_state in tuple(self._states.items()):
                    if old_key != key and not old_state.refreshing:
                        self._states.pop(old_key)
                        break
            spawn_refresh = False
            meta: dict[str, Any] | None = None
            if entry is not None:
                age = now - entry.computed_at
                if age >= ttl and not state.refreshing and (
                    entry.failed_at is None or now - entry.failed_at >= backoff
                ):
                    spawn_refresh = True
                    state.refreshing = True
                meta = self._describe(entry, age, ttl, refreshing=state.refreshing)

        if entry is not None and meta is not None:
            if spawn_refresh:
                self._start_refresh(key, compute)
            return entry.value, meta

        # Cold: nothing to serve yet, so waiting is unavoidable — and only one caller computes.
        with state.lock:
            with self._lock:
                existing = state.entry
                refreshing = state.refreshing
            if existing is not None:
                age = self._clock() - existing.computed_at
                return existing.value, self._describe(
                    existing, age, ttl, refreshing=refreshing
                )
            fresh = self._store(key, compute())
        return fresh.value, self._describe(fresh, 0.0, ttl, refreshing=False)

    def warm(self, key: str, ttl: float, compute: Callable[[], Any]) -> bool:
        """Warms a key without dropping the caller. For calling from a background thread at boot."""
        try:
            self.get(key, ttl, compute)
        except Exception:  # noqa: BLE001 — a failed warmup is not a dashboard outage
            return False
        return True

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._states.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    # --- Internals ---
    def _describe(
        self, entry: _Entry, age: float, ttl: float, *, refreshing: bool
    ) -> dict[str, Any]:
        return {
            "computed_at": _iso(entry.computed_wall),
            "age_seconds": round(max(0.0, age), 1),
            "ttl_seconds": round(float(ttl), 1),
            "stale": age >= ttl,
            "refreshing": refreshing,
            "error": entry.error,
        }

    def _store(self, key: str, value: Any) -> _Entry:
        entry = _Entry(value=value, computed_at=self._clock(), computed_wall=self._wall())
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _State()
                self._states[key] = state
            state.entry = entry
        return entry

    def _note_failure(self, key: str, exc: BaseException) -> None:
        """Keeps the old value and stamps the failure — so not every request becomes a failed attempt."""
        detail = f"{type(exc).__name__}: {exc}"[:200]
        with self._lock:
            state = self._states.get(key)
            if state is None or state.entry is None:
                return
            state.entry = replace(state.entry, error=detail, failed_at=self._clock())

    def _start_refresh(self, key: str, compute: Callable[[], Any]) -> None:
        def run() -> None:
            try:
                value = compute()
            except Exception as exc:  # noqa: BLE001 — a failed refresh is logged, not raised
                self._note_failure(key, exc)
            else:
                self._store(key, value)
            finally:
                with self._lock:
                    state = self._states.get(key)
                    if state is not None:
                        state.refreshing = False

        self._spawn(run)


# The dashboard process's cache. One instance because there is one process; cleared in tests.
MEMO = TTLMemo()
