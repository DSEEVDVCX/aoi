"""Read and rotate local provider keys without exposing their values.

Observability is part of this too: the pool lives in the owning process's memory,
while the dashboard is another process reading the database — without a written
stamp there is no way to know "is there even a second key?" or "is one of them
rejected right now?" except by reading a text log. Hence `KeyPool.stats()` and
`pool_report()`: **counts and indicators only, no key value and no fragment of
one** (FR-013) — a count cannot be rebuilt into a key, and a fingerprint can be
matched back, so we never emit it.

And this limit is a limit of *this route*: whatever is written to `meta` gets
backed up and read inside logs, so it never carries any part of the secret. The
dashboard has a separate second route (`keystore` reads the file directly) where
it shows the account name and the last four characters on request — computed at
request time, never written to `meta` or to any log. The two routes are therefore
never mixed: what may be shown on screen is not necessarily what may be logged.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from typing import Any

import config
import key_file


def read_keys(plural: str, singular: str, env_name: str | None = None) -> list[str]:
    """The values of one provider's keys for the pool — **only the enabled ones**.

    The file format itself is defined in `key_file` alone (the dashboard reads and
    writes it too, so there is one source of truth, not two). This spot adds two
    things: environment-variable precedence, and the `enabled` filter — a
    temporarily disabled key stays in the file so the dashboard can see it and
    re-enable it, but it never enters the pool, so it never gets called.
    """
    if env_name:
        env = os.environ.get(env_name, "").strip()
        if env:
            return list(dict.fromkeys(value.strip() for value in env.split(",") if value.strip()))
    return [
        row["key"]
        for row in key_file.load_entries(config.chain_keys_path(), plural, singular)
        if row["enabled"]
    ]


class KeyPool:
    """Round-robin pool with temporary cooldown for rejected keys."""

    def __init__(self, keys: list[str], cooldown_seconds: float = 60.0) -> None:
        self.keys = list(dict.fromkeys(keys))
        self.cooldown_seconds = cooldown_seconds
        self._index = 0
        self._blocked_until: dict[str, float] = {}
        self._rotations = 0

    def current(self) -> str:
        if not self.keys:
            raise KeyError("no provider keys")
        return self.keys[self._index % len(self.keys)]

    def refresh(self, keys: list[str]) -> None:
        updated = list(dict.fromkeys(keys))
        if updated == self.keys:
            return
        current = self.current() if self.keys else None
        self.keys = updated
        self._blocked_until = {
            key: until for key, until in self._blocked_until.items() if key in updated
        }
        self._index = updated.index(current) if current in updated else 0

    def rotate(self, *, block_current: bool = False) -> str:
        """Moves to the first non-cooled key and counts the rotation.

        The last-resort fallback is deliberate: if **all** keys are cooled, we
        still return the next one despite its cooldown rather than raise. A cooled
        key may simply have exhausted its per-minute quota, and one attempt with a
        temporarily rejected key is better than dropping the cycle for certain —
        and the caller's timeout and attempt cap prevent endless rotation.
        """
        current = self.current()
        if block_current:
            self._blocked_until[current] = time.monotonic() + self.cooldown_seconds
        self._rotations += 1
        for step in range(1, len(self.keys) + 1):
            index = (self._index + step) % len(self.keys)
            key = self.keys[index]
            if time.monotonic() >= self._blocked_until.get(key, 0.0):
                self._index = index
                return key
        self._index = (self._index + 1) % len(self.keys)
        return self.current()

    def blocked_count(self, *, now: float | None = None) -> int:
        """How many keys are cooled right now — not how many ever were (cooldowns expire)."""
        moment = time.monotonic() if now is None else now
        return sum(
            1 for key in self.keys if moment < self._blocked_until.get(key, 0.0)
        )

    def blocked_indices(self, *, now: float | None = None) -> list[int]:
        """Positions of the cooled keys in the list — so the dashboard can color a specific key.

        The count alone is not enough: "one of three is cooled" does not say which
        one, so all three would be drawn in one color and the healthy one would be
        blamed. A position is a number in a list: it reveals neither a value nor
        the list's length (FR-013), and the list's order is the order of the enabled
        keys in the file — so it matches what the dashboard shows, line by line.
        """
        moment = time.monotonic() if now is None else now
        return [
            index for index, key in enumerate(self.keys)
            if moment < self._blocked_until.get(key, 0.0)
        ]

    def stats(self, *, now: float | None = None) -> dict[str, Any]:
        """A snapshot of the pool for writing to `meta` — **counts and indicators, no values** (FR-013).

        `index` is an index into a list: it reveals neither a value nor the list's
        length. And `available` is computed rather than stored separately, so the
        two numbers can never contradict each other in the display.
        """
        blocked = self.blocked_indices(now=now)
        return {
            "keys": len(self.keys),
            "blocked": len(blocked),
            "available": max(0, len(self.keys) - len(blocked)),
            "blocked_index": blocked,
            "index": (self._index % len(self.keys)) if self.keys else 0,
            "rotations": self._rotations,
            "cooldown_seconds": round(float(self.cooldown_seconds), 1),
        }


def pool_report(
    pools: Mapping[str, Mapping[str, Any]], *, at: str, owner: str,
) -> str:
    """One JSON line for a row in `meta`. The owner lives in the value, not only in the key.

    It takes computed snapshots (`client.key_stats()`), not pools: the pool is an
    internal property of the client, and passing it out would have opened a second
    route to `keys` itself.

    Every process writes its own row (`provider_keys_<owner>`), so there is no race
    over a shared row: one provider may exist in both `FomoChain` and
    `FomoEVMReplay` in two different states, and a single row for the two would
    show only the last writer and call it the truth.
    """
    return json.dumps(
        {"at": at, "owner": owner, "pools": {name: dict(s) for name, s in pools.items()}},
        ensure_ascii=False,
        sort_keys=True,
    )


def write_pool_report(
    db: Any, owner: str, pools: Mapping[str, Mapping[str, Any]], at: str,
) -> bool:
    """Stamps the pools report into `meta`. Never raises: this is a report, not a measurement.

    It goes through `note_error` on purpose — it is not an error message, but its
    meaning is the same: a bookkeeping write must not take down its writer when the
    database is the resource that is down (see `db.note_error`). And if the method
    is missing (a fake database in a test), we do not stumble.
    """
    try:
        value = pool_report(pools, at=at, owner=owner)
    except Exception:  # noqa: BLE001 — a failed serialization must not drop the cycle
        return False
    note = getattr(db, "note_error", None)
    if note is None:
        return False
    return bool(note(f"provider_keys_{owner}", value))
