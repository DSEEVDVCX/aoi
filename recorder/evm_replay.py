# -*- coding: utf-8 -*-
"""Retroactive replay of EVM holder concentration: concentration rows for a past when the layer was not running.

**Why is this allowed while retroactive signal backfilling is forbidden?** The difference
is not in time but in the source of the information. That one fabricates a monitoring
window for a token the bot never saw at all — importing into training a selection that
would never have occurred. This one creates no row and no window: it takes an
**existing** training row and completes columns that were NULL in it, with information
that was **actually available** at its moment.

And the chain is the only one of our sources that makes this possible: an immutable
ledger dated by blocks. Replaying a token's transfers up to the block that was head at
the old moment yields **exactly what was known at that moment** — nothing from its
future. Prices, liquidity, and holders from FOMO, by contrast, are a momentary state
with no history, so they cannot be replayed and are not attempted.

And three guards make every replayed row either correct or absent — nothing in between:

1. **`is_replay = 1`** on every row. The number is honest, but its path differs (live:
   confirmation delay and cycle cadence; replay: exactly at the block) => the split is a
   real column, not a field in `raw_json`, so training on live measurements alone stays
   possible.
2. **The negative-balance check.** A balance is an accumulation, not a rate: whoever
   starts reading after the first transfer sees sends without receives, so the balance
   goes negative — meaning the numbers are **false, not incomplete**. The live layer
   clamps negatives to zero (its column is a hexadecimal string that carries no sign);
   here it is not clamped but exposed: a token with even one negative address is not
   written at all.
3. **The time margin is added, never subtracted.** Where the log carries no timestamp
   (Robinhood), time is interpolated between anchors, and interpolation errs by
   seconds. The margin pushes a boundary log to **after** the snapshot: the worst that
   can happen is a late measurement (as block confirmations do in the live layer), never
   an early one — the only acceptable direction.

And the cadence is 5 minutes, not a single snapshot per training row: the `onchain_*`
family reads a snapshot at/before t0 **and one 240-900s earlier** to compute the
five-minute deltas (`features.py`), so a single snapshot leaves all the deltas NULL —
and they are the most valuable part of the layer.

Usage:
  python evm_replay.py --check          # what is available and what it costs, with no write calls
  python evm_replay.py --limit 1        # one token (manual verification)
  python evm_replay.py --token 0x… --dry-run
  python evm_replay.py                  # everything available on the replay networks
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import heapq
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402 — running this file directly requires adding HERE first
import evm_rpc  # noqa: E402
from db import RecorderDB, StaleEVMState, decode_raw, utcnow_iso  # noqa: E402
from evm_layer import build_evm_concentration_row  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - depends on host terminal
        pass

# Addresses that do not count as holders — the same set as the live layer, otherwise the columns would disagree.
_BURN = {a.lower() for a in evm_rpc.BURN_ADDRESSES}
_TOP_N = 20
# How many snapshots before the entry moment: the five-minute delta at t0 needs a
# predecessor before it; without one, `onchain_*_delta_5m` stays empty in the first
# row — and that is the most important row of the window.
_LEAD_STEPS = 2


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())


def _iso(ts: int) -> str:
    """Exactly the same format as `utcnow_iso`: reading in `features.py` goes through
    `strftime('%s', recorded_at)` and SQLite understands the `+00:00` offset, not the Z letter."""
    return datetime.fromtimestamp(int(ts), UTC).isoformat()


def grid_points(start_ts: int, end_ts: int, step: int) -> list[int]:
    """Snapshot moments: from start to end at a fixed step, end inclusive.

    The grid is aligned to multiples of the step (`ts // step * step`), not to the entry
    moment: two tokens that entered three minutes apart land on the same grid, so if
    live snapshots are added later, two series with two cadences never tangle.
    """
    step = max(1, int(step))
    first = (int(start_ts) // step) * step
    return list(range(first, int(end_ts) + 1, step))


def watch_grid(
    watch: dict[str, Any], start_ts: int, end_ts: int, step: int,
    live_coverage: dict[str, str] | None = None,
) -> list[int]:
    """Union of the token's actual window points; gaps between re-activations are not filled."""
    windows = watch.get("replay_windows")
    if not isinstance(windows, list) or not windows:
        windows = [{
            "first_seen_at": watch["first_seen_at"],
            "watch_until": watch["watch_until"],
        }]
    coverage = live_coverage or {}
    points: set[int] = set()
    for window in windows:
        if not isinstance(window, dict):
            continue
        lo = _epoch(window["first_seen_at"]) - _LEAD_STEPS * step
        hi = min(_epoch(window["watch_until"]), int(end_ts))
        live_first = coverage.get(str(window["first_seen_at"]))
        if live_first:
            hi = min(hi, _epoch(live_first) - step)
        if hi > lo:
            points.update(grid_points(lo, hi, step))
    return sorted(point for point in points if int(start_ts) <= point <= int(end_ts))


class BlockClock:
    """A time↔block conversion table for one network, stored in `evm_block_time`.

    Shared across tokens by design: an anchor is a property of a block, not of a token,
    and the 48-hour windows overlap heavily => the tenth token on a network makes almost
    no calls of its own. That is why anchors are aligned on a fixed grid (multiples of
    `EVM_REPLAY_ANCHOR_BLOCKS`): random numbers per token would mean zero reuse.

    And an anchor is never invalidated — a block's timestamp does not change — so it is the cheapest memory in the project.
    """

    def __init__(self, db: RecorderDB, network_id: str) -> None:
        self.db = db
        self.net = str(network_id)
        points = db.block_anchors(self.net)
        self.blocks = [b for b, _ in points]
        self.times = [t for _, t in points]
        self.calls = 0

    def __len__(self) -> int:
        return len(self.blocks)

    def add(self, block: int, ts: int, now_iso: str) -> None:
        block, ts = int(block), int(ts)
        i = bisect.bisect_left(self.blocks, block)
        if i < len(self.blocks) and self.blocks[i] == block:
            return
        self.blocks.insert(i, block)
        self.times.insert(i, ts)
        self.db.add_block_anchor(self.net, block, ts, now_iso)

    def time_at(self, block: int) -> int | None:
        """A block's time by linear interpolation between the nearest anchors.

        Linearity is justified by measurement: Robinhood block time is 0.1002s over 100k
        blocks and 0.1003s over 300k (2026-08-13) => the deviation within one span
        (18k blocks = half an hour) is seconds, not minutes. Outside both spans the
        slope is extrapolated from the nearest pair instead of returning `None`: a new
        token has all its transfers above the last anchor, and refusing it would mean
        measuring nothing.
        """
        block = int(block)
        n = len(self.blocks)
        if n == 0:
            return None
        if n == 1:
            return self.times[0] if self.blocks[0] == block else None
        i = bisect.bisect_left(self.blocks, block)
        if i < n and self.blocks[i] == block:
            return self.times[i]
        lo = min(max(i - 1, 0), n - 2)   # interior span, or the nearest pair at the ends
        b0, b1 = self.blocks[lo], self.blocks[lo + 1]
        t0, t1 = self.times[lo], self.times[lo + 1]
        if b1 == b0:
            return t0
        return int(round(t0 + (block - b0) * (t1 - t0) / (b1 - b0)))

    def has_exact(self, block: int) -> bool:
        i = bisect.bisect_left(self.blocks, int(block))
        return i < len(self.blocks) and self.blocks[i] == int(block)

    async def probe(
        self, rpc: Any, block: int, now_iso: str, sleep=asyncio.sleep,
        retries: int | None = None,
    ) -> int | None:
        """Fetches a block timestamp and saves it as an anchor. A known block is not called.

        Throttling (429) is waited out and retried **here**, not raised: the public
        Robinhood node throttles after nine calls (measured 2026-08-13, identically at
        0.4s and 1.0s spacing — it is a quota, not a spacing issue), and the first live
        run died on the first token with `EVMRateLimit` from `eth_getBlockByNumber`.
        Raising it would drop the whole token because of one crowded second, while the
        logs path waits and retries the same range — so the anchor deserves the same
        treatment. And every attempt counts as a call even when rejected: the quota is
        consumed by the request, not by the reply.
        """
        block = int(block)
        if block < 0:
            return None
        if self.has_exact(block):
            return self.time_at(block)
        tries = (
            int(config.EVM_REPLAY_ANCHOR_RETRIES) if retries is None else int(retries)
        )
        for attempt in range(max(0, tries) + 1):
            self.calls += 1
            try:
                ts = await rpc.block_timestamp(self.net, block)
            except evm_rpc.EVMRateLimit:
                if attempt >= max(0, tries):
                    raise
                await sleep(config.EVM_RATE_LIMIT_BACKOFF_SECONDS)
                continue
            break
        if ts is None:
            return None
        self.add(block, ts, now_iso)
        return ts

    async def ensure_blocks(
        self, rpc: Any, blocks: Iterable[int], now_iso: str, sleep,
        max_calls: int, head: int | None = None,
    ) -> int:
        """Ensures a span around every required block, on the fixed grid.

        Returns the number of blocks left without a span (the budget ran out) — the
        caller counts them as logs without a time and drops them rather than guessing.
        """
        step = max(1, int(config.EVM_REPLAY_ANCHOR_BLOCKS))
        wanted: set[int] = set()
        for b in blocks:
            b = int(b)
            wanted.add((b // step) * step)
            wanted.add((b // step) * step + step)
        missing = sorted(x for x in wanted if not self.has_exact(x))
        if head is not None:
            missing = [x for x in missing if x <= int(head)]
        left = 0
        for i, block in enumerate(missing):
            if self.calls >= max_calls:
                left = len(missing) - i
                break
            if self.calls:
                await sleep(config.EVM_PACING_SECONDS)
            await self.probe(rpc, block, now_iso, sleep)
        return left

    def _guess_block(self, target_ts: int, head: int) -> int:
        """A first estimate of the block number at a time, the inverse of interpolation."""
        n = len(self.times)
        if n == 0:
            return max(0, head // 2)
        i = bisect.bisect_left(self.times, int(target_ts))
        if n == 1:
            return self.blocks[0]
        lo = min(max(i - 1, 0), n - 2)
        t0, t1 = self.times[lo], self.times[lo + 1]
        b0, b1 = self.blocks[lo], self.blocks[lo + 1]
        if t1 == t0:
            return b0
        guess = b0 + (int(target_ts) - t0) * (b1 - b0) / (t1 - t0)
        return int(min(max(0, guess), head))

    async def block_at_time(
        self, rpc: Any, target_ts: int, head: int, now_iso: str, sleep,
        max_calls: int, tolerance: int = 300,
    ) -> int:
        """The highest block whose timestamp is ≤ the requested time — by interpolation, not binary search.

        Binary search costs ~25 calls on a network of 30 million blocks, interpolation
        costs 3–4: block time is nearly constant, so the first estimate lands within
        minutes and a single call corrects it.

        And the error is **deliberately in the old direction**: a block older than
        requested costs extra log calls and then finishes, whereas a newer block misses
        transfers => negative balances and rows that are never written. So the answer is
        the lowest anchor that satisfies the condition, not the nearest one.
        """
        target = int(target_ts)
        if len(self.blocks) < 2:
            await self.probe(rpc, head, now_iso, sleep)
            await sleep(config.EVM_PACING_SECONDS)
            await self.probe(rpc, max(0, head - 1_000_000), now_iso, sleep)
        for _ in range(5):
            if self.calls >= max_calls:
                break
            guess = self._guess_block(target, head)
            if self.has_exact(guess):
                ts = self.time_at(guess)
            else:
                await sleep(config.EVM_PACING_SECONDS)
                ts = await self.probe(rpc, guess, now_iso, sleep)
            if ts is None or abs(ts - target) <= tolerance:
                break
        below = [b for b, t in zip(self.blocks, self.times) if t <= target]
        return max(below) if below else 0


def resolve_log_times(
    logs: Sequence[dict[str, Any]], clock: BlockClock, margin: int,
) -> tuple[dict[int, int], int]:
    """{block number: its time} for every block that appears in the logs, plus the count of blocks with no time.

    The timestamp comes from the log itself when the node provides one (Base and BSC
    do) — no interpolation and no margin then; it is the truth. Robinhood returns
    `blockTimestamp: '0x0'` (measured) => interpolation **plus a margin**: too much
    pushes a boundary log out of the snapshot, and too little lets in a transfer from
    its future.

    And `'0x0'` is treated as an absence, not as a time: a 1970 block would have let
    every transfer into every snapshot — exactly the opposite of what we want.
    """
    out: dict[int, int] = {}
    unknown = 0
    for log in logs:
        block = evm_rpc._num(log.get("blockNumber"))
        if block is None or block in out:
            continue
        raw = evm_rpc._num(log.get("blockTimestamp"))
        if raw:
            out[block] = raw
            continue
        est = clock.time_at(block)
        if est is None:
            unknown += 1
            continue
        out[block] = est + int(margin)
    return out, unknown


def replay_rows(
    logs: Sequence[dict[str, Any]],
    block_ts: dict[int, int],
    grid: Sequence[int],
    token_address: str,
    network_id: str,
    watch: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """A token's logs + a grid of moments => `chain_concentration` rows with their dates.

    A pure function with no network and no database: it is the heart of correctness
    here, so it must be testable without either. And it is a single pass: transfers are
    sorted by time once, then one pointer walks with the grid — so the cost is linear,
    not (moments × transfers).
    """
    token = token_address.lower()
    events: list[tuple[int, int, str, str, int]] = []
    skipped = 0
    for log in logs:
        rec = evm_rpc.decode_transfer(log)
        if rec is None or rec["token_address"] != token:
            skipped += 1
            continue
        ts = block_ts.get(rec["block"])
        if ts is None:
            skipped += 1
            continue
        events.append((ts, rec["block"], rec["from"], rec["to"], rec["value"]))
    events.sort(key=lambda e: (e[0], e[1]))

    balances: dict[str, int] = {}
    negatives: set[str] = set()
    rows: list[dict[str, Any]] = []
    empty = 0
    idx = 0
    for point in grid:
        while idx < len(events) and events[idx][0] <= point:
            _, _, src, dst, value = events[idx]
            idx += 1
            for holder, delta in ((src, -value), (dst, value)):
                if delta == 0:
                    continue
                new = balances.get(holder, 0) + delta
                balances[holder] = new
                # No clamping at zero as the live layer does: a negative here is
                # **evidence** that the reading started late, and erasing it turns the
                # evidence into a number that looks sound but is false.
                if new < 0 and holder not in _BURN:
                    negatives.add(holder)
        live = {h: v for h, v in balances.items() if v > 0 and h not in _BURN}
        if not live:
            empty += 1
            continue
        supply = sum(live.values())
        top = heapq.nlargest(_TOP_N, live.items(), key=lambda kv: kv[1])
        row = build_evm_concentration_row(
            {"holder_count": len(live), "supply": supply}, top, token,
            str(network_id), _iso(point), watch["first_seen_at"],
            watch.get("entry_signal_id"), int(watch.get("is_control") or 0),
        )
        if row is None:
            empty += 1
            continue
        row["is_replay"] = 1
        rows.append(row)

    meta = {
        "events": len(events), "skipped": skipped, "empty": empty,
        "negatives": len(negatives), "applied": idx,
    }
    # One negative address in a token => not a single row. Partiality here is not "less
    # precision" but ratios computed over a missing supply: a token whose mint we missed
    # shows 90% concentration when the truth is 9%.
    if negatives:
        return [], meta
    return rows, meta


def _replay_segment(
    logs: Sequence[dict[str, Any]], block_ts: dict[int, int], grid: Sequence[int],
    token_address: str, network_id: str, watch: dict[str, Any],
    initial_balances: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    """A cumulative version of the replay core; returns the balance checkpoint for the next segment."""
    token = token_address.lower()
    events: list[tuple[int, int, str, str, int]] = []
    skipped = 0
    for log in logs:
        rec = evm_rpc.decode_transfer(log)
        if rec is None or rec["token_address"] != token:
            skipped += 1
            continue
        ts = block_ts.get(rec["block"])
        if ts is None:
            skipped += 1
            continue
        events.append((ts, rec["block"], rec["from"], rec["to"], rec["value"]))
    events.sort(key=lambda event: (event[0], event[1]))

    balances = dict(initial_balances or {})
    negatives: set[str] = set()
    rows: list[dict[str, Any]] = []
    empty = 0
    idx = 0
    for point in grid:
        while idx < len(events) and events[idx][0] <= point:
            _, _, src, dst, value = events[idx]
            idx += 1
            for holder, delta in ((src, -value), (dst, value)):
                if delta == 0:
                    continue
                new = balances.get(holder, 0) + delta
                balances[holder] = new
                if new < 0 and holder not in _BURN:
                    negatives.add(holder)
        live = {h: v for h, v in balances.items() if v > 0 and h not in _BURN}
        if not live:
            empty += 1
            continue
        supply = sum(live.values())
        top = heapq.nlargest(_TOP_N, live.items(), key=lambda item: item[1])
        row = build_evm_concentration_row(
            {"holder_count": len(live), "supply": supply}, top, token,
            str(network_id), _iso(point), watch["first_seen_at"],
            watch.get("entry_signal_id"), int(watch.get("is_control") or 0),
        )
        if row is not None:
            row["is_replay"] = 1
            rows.append(row)

    # Apply what falls after the last point too, so the checkpoint represents the end of
    # the range read, not just the last snapshot. These events affect the first
    # snapshot of the next segment.
    while idx < len(events):
        _, _, src, dst, value = events[idx]
        idx += 1
        for holder, delta in ((src, -value), (dst, value)):
            if delta == 0:
                continue
            new = balances.get(holder, 0) + delta
            balances[holder] = new
            if new < 0 and holder not in _BURN:
                negatives.add(holder)

    meta = {
        "events": len(events), "skipped": skipped, "empty": empty,
        "negatives": len(negatives), "applied": idx,
    }
    if negatives:
        return [], meta, balances
    return rows, meta, balances


def _window(
    db: RecorderDB, watch: dict[str, Any], step: int,
) -> tuple[int, int, dict[str, str]]:
    """The token's time-grid bounds: from before the entry to the end of the window.

    The upper bound stops where **live coverage begins**: two measurements of the same
    moment by two routes (live with confirmation delay and cycle cadence, replay exactly
    at the block) differ slightly, and interleaving them in one series creates phantom
    five-minute deltas — the very thing the features read. So replay fills up to the
    first live row and never touches what comes after it.
    """
    token = str(watch["token_address"]).lower()
    net = str(watch["network_id"])
    start_ts = _epoch(watch["first_seen_at"]) - _LEAD_STEPS * step
    end_ts = _epoch(watch["watch_until"])
    return start_ts, end_ts, db.chain_live_coverage(token, net)


async def replay_token(
    rpc: Any, db: RecorderDB, watch: dict[str, Any], clock: BlockClock,
    head: int, now_iso: str, sleep=asyncio.sleep, write: bool = True,
    replace_existing: bool = False, deadline: float | None = None,
    hyper: Any = None,
) -> dict[str, Any]:
    """Replays one whole token: one range of logs, then one pass over the grid.

    The history walk rides the paid HyperSync route when one is keyed for the
    network, with the public node as the fallback on any refusal — the same
    transport split as the backfill. Measured before routing it here
    (`probe_hypersync_replay.py`, 2026-09-06): an exact log-for-log match
    with the public node on three tokens / 16,222 logs at 2-3× the speed —
    and one dense Base window where the public node spent 60 requests /
    235 s and returned **zero** logs that HyperSync delivered complete in
    36 requests, the failure shape behind replay's ~5-10 tokens/day.

    One measured cost, accepted: HyperSync logs carry no `blockTimestamp`
    (the field does not exist in its selection; block queries answer empty),
    so a routed network joins Robinhood's interpolated-time regime — anchor
    interpolation plus the always-added margin instead of the node's exact
    time. The margin is the guard that was built for exactly this: a
    boundary log lands after its snapshot, never before it. Timestamps and
    anchors therefore stay on the public node, whose calls are single-block,
    cached in `evm_block_time`, and shared across tokens.
    """
    net = str(watch["network_id"])
    token = str(watch["token_address"]).lower()
    use_hyper = hyper is not None and getattr(
        hyper, "can_attempt", getattr(hyper, "covers", lambda _n: False)
    )(net)
    generation = db.evm_ledger_generation()
    expected_status = watch.get("replay_status")
    expected_from = watch.get("replay_from_block")
    expected_checkpoint = watch.get("replay_checkpoint_json")
    expected_revision = watch.get("replay_revision")
    expected_windows = watch.get("replay_windows")
    step = int(config.EVM_REPLAY_STEP_SECONDS)
    out: dict[str, Any] = {
        "token": token, "network": net, "status": "skip", "rows": 0,
        "written": 0, "calls": 0, "anchor_calls": 0, "events": 0,
        "negatives": 0, "unknown_time": 0, "from_block": None,
        "to_block": None, "note": "", "hyper_fallbacks": 0,
    }
    start_ts, end_ts, live_coverage = _window(db, watch, step)
    full_grid = watch_grid(watch, start_ts, end_ts, step, live_coverage)
    if end_ts <= start_ts or not full_grid:
        out["note"] = "live coverage precedes the window" if live_coverage else "empty window"
        out["status"] = "skip"
        if write:
            with db.batch():
                db.assert_evm_ledger_generation(generation)
                db.assert_chain_live_coverage(token, net, live_coverage)
                if isinstance(expected_windows, list):
                    db.assert_evm_replay_windows(token, net, expected_windows)
                db.assert_evm_replay_state(
                    token, net, expected_status, expected_from, expected_checkpoint,
                    expected_revision,
                )
                if replace_existing:
                    db.reset_evm_replay_token(token, net)
                db.set_evm_replay_state(
                    token, net, "skip", now_iso, from_block=0, to_block=0,
                    transfers=0, snapshots=0, calls=0, balance_check="ok",
                    last_error=out["note"],
                )
        return out

    # Two separate budgets: anchors and logs. One shared cap lets a noisy token eat the
    # anchor budget, leaving its logs without times — calls that produce no rows.
    base_calls = clock.calls
    anchor_ceiling = base_calls + max(4, int(config.EVM_REPLAY_ANCHOR_MAX_CALLS))
    log_budget = max(1, int(config.EVM_REPLAY_MAX_CALLS))
    to_block = max(0, int(head) - int(config.EVM_CONFIRMATIONS))
    if (
        _epoch(now_iso) - end_ts > int(config.EVM_REPLAY_HEAD_GRACE_SECONDS)
        and clock.calls < anchor_ceiling
    ):
        window_block = await clock.block_at_time(
            rpc, end_ts, to_block, now_iso, sleep,
            max_calls=anchor_ceiling,
        )
        to_block = min(to_block, window_block)

    checkpoint: dict[str, Any] | None = None
    # `window` is included with them: its verdict was invalidated by a new window
    # (`db.stale_evm_replay_verdict`) but its walk still stands. Leaving it out here
    # means the row is read and then its walk is discarded, restarting from genesis —
    # the very breakage this row was kept to avoid.
    if (watch.get("replay_status") or "") in ("partial", "budget", "error", "window"):
        raw_checkpoint = watch.get("replay_checkpoint_json")
        if raw_checkpoint is not None:
            try:
                decoded = decode_raw(raw_checkpoint)
                checkpoint = decoded if isinstance(decoded, dict) else None
            except Exception:  # noqa: BLE001 — a corrupt checkpoint restarts safely from the beginning
                checkpoint = None
    if checkpoint is not None and watch.get("replay_from_block") is not None:
        from_block = int(watch["replay_from_block"])
    else:
        # A balance is cumulative since the contract's creation. A time window or the
        # absence of negative balances proves nothing about completeness: a holder who
        # received before the window and never moved afterwards disappears silently.
        # We start from genesis and resume via checkpoint; slower, but the only complete proof.
        from_block = 0
    checkpoint_balances = (checkpoint or {}).get("balances", {})
    resettable_status = (watch.get("replay_status") or "") in (
        "negative", "no_time", "empty", "skip",
    )
    origin_calls = 0
    if (
        net in (*config.EVM_CREATION_BLOCK_NETWORKS, *config.EVM_MINT_SCAN_NETWORKS)
        and not checkpoint_balances
        and (int(watch.get("replay_transfers") or 0) == 0 or resettable_status)
    ):
        # Two ways to ask "when was it born?", depending on what the node keeps, and a
        # network has one of them, not both: an archive allows binary search on
        # `eth_getCode`, and where there is no archive (Robinhood ~128 blocks) the mint
        # filter answers in a single call. The saving here is larger than in the live
        # layer: replay walks every token from its birth on every resume cycle.
        origin_calls = 1
        if net in config.EVM_MINT_SCAN_NETWORKS:
            minted = None
            hyper_mint = use_hyper
            if hyper_mint:
                # One query instead of a public mint filter; a refusal here is
                # not a token failure — the public scan answers in its place.
                try:
                    minted = await hyper.first_mint_block(net, token, to_block)
                except Exception:  # noqa: BLE001 — the public mint scan is the fallback
                    hyper_mint = False
            if not hyper_mint:
                minted = await rpc.first_mint_block(net, token, to_block)
            if minted is not None:
                from_block = max(from_block, minted)
        else:
            creation = await rpc.contract_creation_block(net, token, to_block)
            if creation is not None:
                from_block = max(from_block, creation)

    rows: list[dict[str, Any]] = []
    meta: dict[str, int] = {"events": 0, "skipped": 0, "empty": 0,
                            "negatives": 0, "applied": 0}
    calls_used = origin_calls
    unknown = 0
    complete = False
    covered_to = to_block
    attempt_from = from_block
    initial_balances = {
        str(holder): int(value)
        for holder, value in (checkpoint or {}).get("balances", {}).items()
    }
    checkpoint_negative = any(
        value < 0 and holder not in _BURN
        for holder, value in initial_balances.items()
    )
    next_grid = int((checkpoint or {}).get("next_grid") or grid_points(start_ts, start_ts, step)[0])
    final_balances = dict(initial_balances)
    for _attempt in (0,):
        try:
            logs, used, complete, resume = await (
                hyper if use_hyper else rpc
            ).get_logs_paged(
                net, [token], attempt_from, to_block, max_calls=log_budget,
                sleep=sleep, deadline=deadline,
            )
        except Exception:  # the public node walks the same range instead
            if not use_hyper:
                raise
            # The paid route refused (key, quota, transport) and nothing was
            # applied yet: `get_logs_paged` returns its logs only on success,
            # so the public retry reads the range exactly once, never twice.
            out["hyper_fallbacks"] += 1
            logs, used, complete, resume = await rpc.get_logs_paged(
                net, [token], attempt_from, to_block, max_calls=log_budget,
                sleep=sleep, deadline=deadline,
            )
        calls_used += used
        covered_to = to_block if complete else int(resume) - 1
        blocks = [
            b for b in (evm_rpc._num(lg.get("blockNumber")) for lg in logs)
            if b is not None
        ]
        if any(not evm_rpc._num(lg.get("blockTimestamp")) for lg in logs):
            await clock.ensure_blocks(
                rpc, blocks, now_iso, sleep, anchor_ceiling, head=to_block,
            )
        if covered_to >= attempt_from:
            await clock.probe(rpc, covered_to, now_iso, sleep)
        block_ts, unknown = resolve_log_times(
            logs, clock, config.EVM_REPLAY_TIME_MARGIN_SECONDS,
        )
        made_progress = covered_to >= attempt_from
        cap_ts = clock.time_at(covered_to) if made_progress else None
        window_end = end_ts if cap_ts is None else min(end_ts, int(cap_ts))
        grid = [] if not made_progress else [
            point for point in full_grid
            if point <= window_end and point >= next_grid
        ]
        rows, meta, final_balances = _replay_segment(
            logs, block_ts, grid, token, net, watch, initial_balances,
        )
        break

    window_complete = bool(
        complete and cap_ts is not None
        and int(cap_ts) >= full_grid[-1]
    )

    out.update({
        "calls": calls_used, "anchor_calls": clock.calls - base_calls,
        "events": meta["events"], "negatives": meta["negatives"],
        "unknown_time": unknown, "from_block": attempt_from,
        "to_block": covered_to, "rows": len(rows),
    })

    # Three reasons not to write, each with its own status: negative balances (false
    # numbers), logs without a time (we cannot tell which snapshot a transfer enters, so
    # it would be guessed), and no transfer at all (a token that never moved — an
    # absence, not a zero, FR-007).
    prior_transfers = int(watch.get("replay_transfers") or 0) if checkpoint else 0
    prior_calls = int(watch.get("replay_calls") or 0) if checkpoint else 0
    has_negative = bool(meta["negatives"] or checkpoint_negative)
    if has_negative and not window_complete:
        status, check = "partial", "ok"
        out["note"] = "partial ledger contains negative balances; will re-verify once complete"
    elif has_negative:
        status, check = "negative", "negative"
        out["note"] = "negative balances after ledger completion => no row"
    elif unknown:
        status, check = "no_time", "ok"
        out["note"] = f"{unknown} blocks without timestamps => no row"
    elif not window_complete:
        status, check = "partial", "ok"
        if not rows:
            out["note"] = "range is partial and has not reached the first snapshot yet"
    elif not rows:
        status, check = "empty", "ok"
        out["note"] = "no transfer in range"
    else:
        status, check = "done", "ok"

    # A cap **per token** — the thing `EVM_REPLAY_MAX_CALLS` (the per-cycle cap) does not
    # do: a token that returns `partial` every cycle resumes forever and eats the budget
    # away from tokens that finish. Measured on Base: two tokens spent 15,270 and
    # 11,665 calls for zero rows, against ~4,960 calls for the heaviest legitimate walk.
    # Exceeding it is therefore not slowness but a silent failure. And it is recorded
    # under its own name: `budget` is final so it is not retried, but the checkpoint
    # stays saved and resumes from where it stopped the day the cap is raised or
    # `--redo` is passed.
    if status == "partial" and prior_calls + calls_used >= int(
        config.EVM_REPLAY_TOKEN_CALL_CAP
    ):
        status, check = "budget", "ok"
        out["note"] = (
            f"{prior_calls + calls_used} calls reached the token call cap "
            f"({config.EVM_REPLAY_TOKEN_CALL_CAP}) => stopping with a resume checkpoint"
        )

    written = 0
    if write:
        # Rows and checkpoint are one transaction. A process dying between them used to
        # replay the same segment from a stale checkpoint; INSERT OR IGNORE prevents the
        # visible duplication but leaves counters and progress state inconsistent.
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            db.assert_chain_live_coverage(token, net, live_coverage)
            if isinstance(expected_windows, list):
                db.assert_evm_replay_windows(token, net, expected_windows)
            db.assert_evm_replay_state(
                token, net, expected_status, expected_from, expected_checkpoint,
                expected_revision,
            )
            if replace_existing and checkpoint is None:
                db.delete_evm_replay_rows(token, net)
            for row in rows:
                if db.insert_chain_concentration(row):
                    written += 1
            if status in ("negative", "no_time"):
                # An earlier checkpoint may have written rows before a later corruption
                # was discovered; keeping some of them makes the token look partially
                # complete while it is wholly rejected.
                db.delete_evm_replay_rows(token, net)
                written = 0
            prior_snapshots = int(watch.get("replay_snapshots") or 0) if checkpoint else 0
            if status in ("negative", "no_time"):
                prior_snapshots = 0
            # In the completed state `from_block` stays historical: the start of the
            # range that was examined. In `partial` it carries the actual resume point.
            state_from_block = (
                from_block if window_complete else
                (covered_to + 1 if complete else int(resume))
            )
            next_point = (grid[-1] + step) if grid else next_grid
            saved_checkpoint = (
                {"balances": {h: str(v) for h, v in final_balances.items()},
                 "next_grid": next_point}
                if status in ("partial", "budget") else None
            )
            # **No `set_chain_state`**: that is the live layer's cadence table, and
            # writing to it here would tell the live layer the token was just measured,
            # so it would postpone its real snapshot.
            db.set_evm_replay_state(
                token, net, status, now_iso, from_block=state_from_block,
                to_block=covered_to, transfers=prior_transfers + meta["applied"],
                snapshots=prior_snapshots + written, calls=prior_calls + calls_used,
                balance_check=check, last_error=out["note"] or None,
                checkpoint=saved_checkpoint,
            )
    out["written"] = written
    out["status"] = status
    return out


# Final statuses, not retried except with `--redo`: completed, or proven unachievable,
# or out of its call budget (`budget`). `partial`/`error` are **not** final: the former
# ran out of the *cycle* cap, the latter is a failure that may be transient (throttle,
# timeout) — folding either into the final set means abandoning a token over one bad
# second.
# And the list is **one** because it used to be three: here, in `repair_evm_ledger`,
# and in `run_evm_replay`. A copy that forgets a new status means a stuck counter that
# never lands in a report, or a token "due" in the report and not due in execution.
FINAL_STATUSES = ("done", "negative", "empty", "no_time", "skip", "budget")


async def run_replay(
    rpc: Any, db: RecorderDB, networks: Sequence[str] | None = None,
    limit: int | None = None, token: str | None = None,
    sleep=asyncio.sleep, write: bool = True, redo: bool = False,
    log=None, budget_seconds: float | None = None, hyper: Any = None,
) -> dict[str, Any]:
    """The whole task: every due token on every allowed replay network.

    One token's failure does not sink its network or the next network — the same guard
    as the live layer. State is kept per token, so cutting the run at any moment loses
    only the token in flight, and a later run resumes it.
    """
    emit = log or (lambda _m: None)
    allowed = {str(n) for n in config.EVM_REPLAY_NETWORKS}
    asked = [str(n) for n in (networks or config.EVM_REPLAY_NETWORKS)]
    nets = [n for n in asked if n in allowed]
    stats: dict[str, Any] = {
        "networks": len(nets), "tokens": 0, "rows": 0, "written": 0,
        "calls": 0, "anchor_calls": 0, "done": 0, "partial": 0,
        "negative": 0, "empty": 0, "no_time": 0, "skip": 0, "budget": 0,
        "errors": 0, "hyper_fallbacks": 0,
        "refused_networks": [n for n in asked if n not in allowed],
    }
    remaining = None if limit is None else int(limit)
    budget = (
        config.EVM_REPLAY_BUDGET_SECONDS
        if budget_seconds is None else float(budget_seconds)
    )
    deadline = time.monotonic() + budget if budget > 0 else None
    for net in nets:
        targets = [
            w for w in db.evm_replay_targets([net])
            if redo or (w.get("replay_status") or None) not in FINAL_STATUSES
        ]
        if not redo:
            targets.sort(key=lambda w: (
                w.get("replay_last_try_at") or "",
                w.get("first_seen_at") or "",
            ))
        if token:
            targets = [w for w in targets if w["token_address"].lower() == token.lower()]
        if not targets:
            emit(f"[{net}] no due tokens")
            continue
        head = await rpc.block_number(net)
        clock = BlockClock(db, net)
        emit(f"[{net}] head {head} · due {len(targets)} · saved anchors {len(clock)}")
        anchors_before = clock.calls
        for watch in targets:
            if remaining is not None and remaining <= 0:
                break
            if deadline is not None and time.monotonic() >= deadline:
                emit(f"[{net}] time budget exhausted — will resume later")
                break
            now_iso = utcnow_iso()
            generation = db.evm_ledger_generation()
            try:
                res = await replay_token(
                    rpc, db, watch, clock, head, now_iso, sleep=sleep, write=write,
                    replace_existing=redo, deadline=deadline, hyper=hyper,
                )
            except StaleEVMState:
                # Another worker or a reset got there first; do not downgrade its successful state to error.
                continue
            except Exception as exc:  # noqa: BLE001 — one token does not sink a network
                stats["errors"] += 1
                msg = f"{type(exc).__name__}: {exc}"[:300]
                emit(f"  ✗ {watch['token_address'][:12]}… {msg}")
                if write and not redo:
                    try:
                        with db.batch():
                            db.assert_evm_ledger_generation(generation)
                            db.assert_evm_replay_state(
                                watch["token_address"], net,
                                watch.get("replay_status"),
                                watch.get("replay_from_block"),
                                watch.get("replay_checkpoint_json"),
                                watch.get("replay_revision"),
                            )
                            db.mark_evm_replay_error(
                                watch["token_address"], net, now_iso, msg,
                            )
                    except StaleEVMState:
                        stats["errors"] -= 1
                continue
            stats["tokens"] += 1
            stats[res["status"]] = stats.get(res["status"], 0) + 1
            for key in ("rows", "written", "calls", "hyper_fallbacks"):
                stats[key] += res[key]
            if remaining is not None:
                remaining -= 1
            emit(
                f"  {res['status']:<8} {res['token'][:12]}… "
                f"rows {res['written']}/{res['rows']} · transfers {res['events']} "
                f"· calls {res['calls']}+{res['anchor_calls']} "
                f"· blocks {res['from_block']}→{res['to_block']}"
                + (f" · {res['note']}" if res["note"] else "")
            )
        stats["anchor_calls"] += clock.calls - anchors_before
    return stats


def _log_line(msg: str) -> None:
    print(msg, flush=True)
    try:
        with open(config.EVM_REPLAY_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{utcnow_iso()} {msg}\n")
    except OSError:
        pass


def _check(db: RecorderDB) -> int:
    """What is due and what it costs — without a single network call."""
    print(
        "Replay networks: "
        + (", ".join(str(n) for n in config.EVM_REPLAY_NETWORKS) or "—")
        + f" · step {config.EVM_REPLAY_STEP_SECONDS}s"
        f" · cap {config.EVM_REPLAY_MAX_CALLS} calls/token"
    )
    total_rows = 0
    for net in (str(n) for n in config.EVM_REPLAY_NETWORKS):
        targets = db.evm_replay_targets([net])
        by_status: dict[str, int] = {}
        for w in targets:
            key = str(w.get("replay_status") or "—")
            by_status[key] = by_status.get(key, 0) + 1
        due = [
            w for w in targets
            if (w.get("replay_status") or None) not in FINAL_STATUSES
        ]
        active = sum(1 for w in targets if int(w.get("active") or 0))
        # 48 hours ÷ 5 minutes = 576 snapshots for a full window; a token whose window has ended has the full window.
        est = len(due) * (48 * 3600 // int(config.EVM_REPLAY_STEP_SECONDS))
        total_rows += est
        print(
            f"[{net}] watches {len(targets)} (active {active}) · due {len(due)}"
            f" · saved anchors {len(db.block_anchors(net))}"
            f" · statuses: {by_status or '—'} => up to ~{est:,} rows"
        )
    print(f"Total ceiling: ~{total_rows:,} replayed concentration rows (is_replay=1)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Retroactive replay of EVM holder concentration")
    ap.add_argument("--networks", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="computes without writing a single row or state")
    ap.add_argument("--redo", action="store_true",
                    help="also replays tokens whose status is final")
    ap.add_argument("--check", action="store_true", help="a report without a single network call")
    args = ap.parse_args()

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    if args.check:
        try:
            return _check(db)
        finally:
            db.close()

    async def _go() -> dict[str, Any]:
        import evm_rpc

        rpc = evm_rpc.EVMRPC()
        try:
            return await run_replay(
                rpc, db, networks=args.networks, limit=args.limit,
                token=args.token, write=not args.dry_run, redo=args.redo,
                log=_log_line,
            )
        finally:
            await rpc.aclose()

    started = time.monotonic()
    try:
        stats = asyncio.run(_go())
    finally:
        db.close()
    stats["seconds"] = round(time.monotonic() - started, 1)
    _log_line(f"replay: {stats}")
    return 0 if not stats["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
