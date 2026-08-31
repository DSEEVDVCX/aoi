# -*- coding: utf-8 -*-
"""EVM layer cycle: a balance ledger we build ourselves from `Transfer` logs.

**Why a ledger and not a provider?** Because the ERC-20 standard does not store
a holder list, so no node call returns it and no provider gives it accurately:
Blockscout gives only the top 50, at 5.6 seconds per token; NodeReal only BSC,
and with an hourly budget; and Etherscan refuses without a key. And measured,
the ledger is cheaper than all of them: one `eth_getLogs` call with a filter
carrying **every** address of the network (Robinhood 57 addresses in 0.5s, Base
22 in 0.4s) ⇒ eight calls per minute cover the whole EVM watchlist.

And it gives **more** than any provider, not less: an exact holder count with no
rank cap, and the first block in which each address received — so the question
"how many new holders in the last five minutes" becomes a count in the database
without a single call, a question no provider answers, since all of them are a
snapshot with no entry history.

Three steps in the cycle, and their order is mandatory:
  1. **Apply** — one call per network, from its cursor to (head − confirmations).
  2. **Backfill** — new tokens, from block zero to the cursor, under a call cap.
  3. **Snapshot** — a read from the ledger into `chain_concentration`, with no network call.

Backfill comes **after** apply, not before: if a token were backfilled up to the
cursor and then logs were applied from that same cursor, the range would be
applied twice and the balances would double. The dividing line is the `to_block`
saved in the backfill state.

Read-only (FR-012). No key anywhere in this layer ⇒ no secret to revoke.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Sequence

import config
import evm_rpc
from db import RecorderDB, StaleEVMState, utcnow_iso
from evm_rpc import EVMLogLimit

_CHAIN_TIERS = ((1, "top1_pct"), (5, "top5_pct"), (10, "top10_pct"), (20, "top20_pct"))


def _batches(items: Sequence[str], size: int) -> list[list[str]]:
    """Splits addresses into batches of a size the provider accepts (its limit, not ours)."""
    size = max(1, int(size))
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def _deltas_by_token(
    logs: Sequence[dict[str, Any]],
) -> dict[str, dict[str, tuple[int, int, int | None]]]:
    """Raw logs → {token: {address: (signed delta, last block)}}.

    Aggregating before writing is deliberate: an address that moves ten times in
    a minute becomes one write, and the summing happens in Python with unbounded
    integers (uint256 exceeds 64 bits, so it does not fit in SQL).

    Burn addresses are **not excluded here** but at snapshot time: the zero
    address's balance is information (how much was actually burned), and the
    exclusion belongs in computing the ratios, not in the ledger.
    """
    out: dict[str, dict[str, tuple[int, int, int | None]]] = {}
    for log in logs:
        rec = evm_rpc.decode_transfer(log)
        if rec is None:
            continue
        token = rec["token_address"]
        per_token = out.setdefault(token, {})
        for holder, delta in ((rec["from"], -rec["value"]), (rec["to"], rec["value"])):
            if delta == 0:
                continue
            prev_delta, prev_block, first_receive = per_token.get(holder, (0, 0, None))
            if delta > 0 and (first_receive is None or rec["block"] < first_receive):
                first_receive = rec["block"]
            per_token[holder] = (
                prev_delta + delta, max(prev_block, rec["block"]), first_receive,
            )
    return out


def build_evm_concentration_row(
    stats: dict[str, Any],
    top: Sequence[tuple[str, int]],
    token_address: str,
    network_id: str,
    recorded_at: str,
    watch_first_seen_at: str,
    entry_signal_id: str | None,
    is_control: int = 0,
    decimals: int | None = None,
) -> dict[str, Any] | None:
    """Ledger stats + top balances → a `chain_concentration` row.

    The same table the Solana layer writes to, so the existing `onchain_*`
    feature family covers EVM with no new feature code — and the only column
    that differs is `holder_count` (exact here, and it stays NULL on Solana,
    where the source returns at most 20 accounts).

    An empty ledger ⇒ `None`, not a row of zeros: a token not yet backfilled is
    not a token with no holders (FR-007). And `supply` is the sum of the live
    balances, not `totalSupply()`: the ratios are computed over what can
    actually be sold, so a token half of which was burned shows its true
    concentration.

    The `supply` column here is **diagnostic, not a feature** (no function in
    `features.py` reads it), so its floating-point approximation does no harm —
    and the exact value is preserved as text in `raw_json.supply_base`. The
    ratios, in contrast, are computed with exact Python integers before the
    conversion, so nothing is lost.
    """
    supply = int(stats.get("supply") or 0)
    holder_count = int(stats.get("holder_count") or 0)
    if holder_count == 0 and supply == 0:
        return None
    amounts = sorted((int(v) for _, v in top), reverse=True)
    row: dict[str, Any] = {
        "token_address": token_address,
        "network_id": network_id,
        "recorded_at": recorded_at,
        "watch_first_seen_at": watch_first_seen_at,
        "entry_signal_id": entry_signal_id,
        "is_control": 1 if is_control else 0,
        # Supply in display units when the decimals are known, in base units when
        # not — the ratios are unaffected (numerator and denominator in the same unit).
        "supply": (supply / (10 ** decimals)) if decimals else float(supply),
        "decimals": decimals,
        "holder_count": holder_count,
        "top_accounts": len(amounts),
        "raw_json": {
            "source": "evm_ledger",
            "supply_base": str(supply),
            "holder_count": holder_count,
            "top": [[a, str(v)] for a, v in top],
        },
    }
    for _, col in _CHAIN_TIERS:
        row[col] = None
    if supply > 0 and amounts:
        for n, col in _CHAIN_TIERS:
            row[col] = 100.0 * sum(amounts[:n]) / supply
    return row


async def _apply_network(
    rpc: Any, db: RecorderDB, network_id: str, recorded_at: str,
    stats: dict[str, int], sleep,
) -> None:
    """Step 1 for one network: from the cursor to (head − confirmations)."""
    generation = db.evm_ledger_generation()
    all_watched = db.evm_watched([network_id])
    if not all_watched:
        return
    expected_done = [
        str(w["token_address"]).lower()
        for w in all_watched if (w.get("backfill_status") or "") == "done"
    ]
    head = await rpc.block_number(network_id)
    target = head - config.EVM_CONFIRMATIONS
    if target <= 0:
        return

    cursor = db.evm_cursor(network_id)
    if cursor is None:
        # First run: no range to apply. The cursor is placed at the target and
        # the backfill takes care of the entire history — applying "from zero"
        # here is pointless: the backfill does it per token under a call cap,
        # whereas here one filter for the whole network could truncate repeatedly.
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            db.assert_evm_cursor(network_id, None)
            db.assert_evm_done_tokens(network_id, expected_done)
            db.set_evm_cursor(network_id, target, recorded_at, "ok")
        stats["evm_cursor_init"] += 1
        return
    watched = [
        w for w in all_watched if (w.get("backfill_status") or "") == "done"
    ]
    if not watched:
        # No completed ledger needs applying, so the boundary may be advanced to
        # the target, then the new tokens backfilled up to it. They are excluded
        # from the apply above, so the range is not duplicated.
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            db.assert_evm_cursor(network_id, int(cursor["last_block"]))
            db.assert_evm_done_tokens(network_id, expected_done)
            db.set_evm_cursor(network_id, target, recorded_at, "ok")
        return
    addresses = [w["token_address"].lower() for w in watched]
    start = int(cursor["last_block"]) + 1
    if start > target:
        return  # no new block (slow network, or a cycle that ran ahead of schedule)

    batch_size = config.EVM_ADDRESS_BATCH.get(str(network_id), 5)
    calls = 0
    fetched: list[tuple[list[dict[str, Any]], int]] = []
    for chunk in _batches(addresses, batch_size):
        if calls:
            await sleep(config.EVM_PACING_SECONDS)
        logs, used, complete, resume = await rpc.get_logs_paged(
            network_id, chunk, start, target,
            max_calls=max(1, config.EVM_APPLY_MAX_CALLS - calls), sleep=sleep,
        )
        calls += used
        reached = target if complete else resume - 1
        fetched.append((logs, reached))

    # No batch may be applied past what the slowest batch completed: the cursor
    # is one per network, and if we wrote a completed batch's future and then
    # rewound it because its sibling lagged, its transfers would be doubled.
    common_reached = min((reached for _, reached in fetched), default=target)
    applied = 0
    # Balances and cursor are one transaction. A crash between them used to
    # replay the same logs on the next boot and double the ledger, with no
    # visible effect.
    with db.batch():
        db.assert_evm_ledger_generation(generation)
        db.assert_evm_cursor(network_id, int(cursor["last_block"]))
        db.assert_evm_done_tokens(network_id, addresses)
        for logs, _ in fetched:
            safe_logs = [
                log for log in logs
                if (evm_rpc._num(log.get("blockNumber")) or -1) <= common_reached
            ]
            for token, deltas in _deltas_by_token(safe_logs).items():
                applied += db.evm_apply_transfers(
                    network_id, token, deltas, recorded_at,
                    allow_negative=evm_rpc.BURN_ADDRESSES,
                )
        db.set_evm_cursor(
            network_id, max(int(cursor["last_block"]), common_reached), recorded_at,
            "ok", logs_applied=applied,
        )
    stats["evm_logs"] += applied
    stats["evm_calls"] += calls
    if common_reached < target:
        stats["evm_lagging"] += 1


async def _backfill_token(
    rpc: Any, db: RecorderDB, watch: dict[str, Any], recorded_at: str,
    stats: dict[str, int], sleep, deadline: float | None = None,
    max_calls: int | None = None,
) -> None:
    """Step 2 for one token: its entire history up to the current cursor.

    The upper bound is the network cursor **at the moment the backfill starts**,
    and that is what prevents double-applying: every block above it will come
    from step 1, and every block below it comes from here.
    """
    generation = db.evm_ledger_generation()
    net = str(watch["network_id"])
    token = watch["token_address"].lower()
    cursor = db.evm_cursor(net)
    if cursor is None:
        return  # no cursor yet ⇒ no known upper bound; the next cycle
    to_block = int(cursor["last_block"])
    state = watch.get("backfill_status")
    expected_from = watch.get("from_block")
    expected_to = watch.get("to_block")
    from_block = config.EVM_BACKFILL_FROM_BLOCK
    if state in ("partial", "retry") and watch.get("from_block") is not None:
        # `retry` is treated like `partial` **by necessity**, not as a nicety:
        # the resume point stays saved on transient failure (`COALESCE` in
        # `set_evm_backfill_state`), and starting from zero while it remains
        # means re-applying an already-applied range ⇒ **doubling every balance
        # in it**. And the point is written only after a successful call, so it
        # is always an honest bound.
        from_block = int(watch["from_block"])  # resume from where the cap stopped
        # A partial token is excluded from live apply, so it must read up to the
        # cursor at the start of this cycle, not up to a stale target that the
        # head outgrows at the same pace, keeping it `partial` forever.
        to_block = max(to_block, int(watch.get("to_block") or to_block))
    creation_due = state is None or watch.get("from_block") is None
    # "When was this token born?" has two routes depending on what the node
    # keeps, and a network is on one of them, not both: an archive allows a
    # binary search on `eth_getCode`, and where there is no archive (Robinhood
    # keeps ~128 blocks) the mint filter answers in a single call.
    origin_networks = (
        *config.EVM_CREATION_BLOCK_NETWORKS, *config.EVM_MINT_SCAN_NETWORKS,
    )
    if (
        not creation_due
        and net in origin_networks
        and int(watch.get("backfill_transfers") or 0) == 0
    ):
        ledger = db.evm_ledger_stats(net, token, exclude=evm_rpc.BURN_ADDRESSES)
        creation_due = int(ledger.get("holder_count") or 0) == 0
    if creation_due and net in config.EVM_MINT_SCAN_NETWORKS:
        # Measured: three of four Robinhood tokens were minted above 67% of the
        # chain, i.e. 27–37 **million** empty blocks were walked before the
        # first transfer. `None` means "unknown", so we stay on
        # `EVM_BACKFILL_FROM_BLOCK`: a wrong lower bound is worse than a long
        # walk, because a holder who received before it shows a negative
        # balance ⇒ the whole token gets rejected.
        minted = await rpc.first_mint_block(net, token, to_block)
        if minted is not None:
            from_block = max(from_block, minted)
    elif creation_due and net in config.EVM_CREATION_BLOCK_NETWORKS:
        creation = await rpc.contract_creation_block(net, token, to_block)
        if creation is not None:
            from_block = max(from_block, creation)
    try:
        logs, calls, complete, resume = await rpc.get_logs_paged(
            net, [token], from_block, to_block,
            max_calls=(config.EVM_BACKFILL_MAX_CALLS if max_calls is None else max_calls),
            sleep=sleep, deadline=deadline,
        )
    except EVMLogLimit as exc:
        # A single block exceeds the cap — no split is possible. It is recorded, not retried every minute.
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            db.assert_evm_backfill_state(
                net, token, state, expected_from, expected_to,
            )
            db.set_evm_backfill_state(
                net, token, "error", recorded_at,
                last_error=f"EVMLogLimit: {exc}"[:300],
            )
        stats["evm_backfill_errors"] += 1
        return

    # If the network cursor advanced while this token was being backfilled,
    # finishing the old range is not enough: the token was excluded from live
    # apply, so it must catch up on the gap before `done`.
    latest_cursor = db.evm_cursor(net)
    latest_block = int(latest_cursor["last_block"]) if latest_cursor else to_block
    caught_up = complete and to_block >= latest_block
    state_status = "done" if caught_up else "partial"
    next_from = (
        to_block + 1 if complete else resume
    )
    next_to = max(to_block, latest_block) if complete else to_block

    with db.batch():
        db.assert_evm_ledger_generation(generation)
        db.assert_evm_backfill_state(
            net, token, state, expected_from, expected_to,
        )
        if caught_up:
            # `done` hands ownership of the later blocks to live apply. If the
            # cursor moved after the catch-up check but before the commit, the
            # gap is lost, so we guard only the final transition; `partial` can
            # keep advancing in parallel without starving.
            db.assert_evm_cursor(net, latest_block)
        transfers = 0
        for tok, deltas in _deltas_by_token(logs).items():
            if tok != token:
                continue  # single-address filter; anything else is a response we do not trust
            transfers += db.evm_apply_transfers(
                net, tok, deltas, recorded_at,
                allow_negative=evm_rpc.BURN_ADDRESSES,
            )
        # The balance and the resume point are one atomic unit. Committing one
        # without the other makes the next boot replay the same range and
        # double every balance in it.
        db.set_evm_backfill_state(
            net, token, state_status, recorded_at,
            from_block=next_from, to_block=next_to,
            transfers=int(watch.get("backfill_transfers") or 0) + transfers,
            calls=calls,
        )
    stats["evm_backfill_calls"] += calls
    if caught_up:
        stats["evm_backfilled"] += 1
    else:
        stats["evm_backfill_partial"] += 1


def _snapshot_token(
    db: RecorderDB, watch: dict[str, Any], recorded_at: str, stats: dict[str, int],
) -> None:
    """Step 3 for one token: a concentration snapshot from the ledger, with no network call."""
    if (watch.get("backfill_status") or "") != "done":
        raise ValueError("cannot take a snapshot of an incomplete EVM ledger")
    generation = db.evm_ledger_generation()
    net = str(watch["network_id"])
    token = watch["token_address"].lower()
    with db.batch():
        db.assert_evm_ledger_generation(generation)
        db.assert_evm_backfill_state(
            net, token, watch.get("backfill_status"),
            watch.get("from_block"), watch.get("to_block"),
        )
        ledger = db.evm_ledger_stats(net, token, exclude=evm_rpc.BURN_ADDRESSES)
        top = db.evm_top_balances(net, token, 20, exclude=evm_rpc.BURN_ADDRESSES)
        row = build_evm_concentration_row(
            ledger, top, token, net, recorded_at,
            watch["first_seen_at"], watch.get("entry_signal_id"),
            int(watch.get("is_control") or 0),
        )
        if row is None:
            db.set_chain_state(token, net, "empty", None, recorded_at)
        else:
            db.insert_chain_concentration(row)
            db.set_chain_state(token, net, "ok", row["top1_pct"], recorded_at)
    if row is None:
        stats["evm_snap_empty"] += 1
        return
    stats["evm_snapshots"] += 1


async def run_evm_cycle(
    rpc: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep,
) -> dict[str, int]:
    """A full cycle: apply, then backfill, then snapshot, for each enabled network.

    One network's error does not take down the rest, and one token's error does
    not take down its network — the same guard as `chain_layer`. Status is
    written to both `evm_block_cursor.last_error` and `meta.last_error_evm`:
    the former for per-network diagnosis, the latter because the dashboard
    reads `meta` alone.
    """
    stats = {
        "evm_networks": 0, "evm_calls": 0, "evm_logs": 0, "evm_cursor_init": 0,
        "evm_lagging": 0, "evm_backfill_due": 0, "evm_backfilled": 0,
        "evm_backfill_partial": 0, "evm_backfill_calls": 0,
        # Transient and retryable (timeout, rate limit, a node that stumbled)
        # versus permanent, which no split can save: two counters, because the
        # first settles on its own and the second needs a hand.
        "evm_backfill_retry": 0, "evm_backfill_errors": 0,
        # Tokens deferred because the time budget ran out — not a failure: the next cycle takes them.
        "evm_backfill_skipped": 0,
        "evm_snapshots": 0, "evm_snap_empty": 0,
        "evm_errors": 0,
    }

    networks = [str(n) for n in config.EVM_NETWORKS]
    if not networks:
        return stats

    # 1) Periodic apply — one call per address batch, per network.
    for net in networks:
        stats["evm_networks"] += 1
        try:
            await _apply_network(rpc, db, net, recorded_at, stats, sleep)
        except StaleEVMState:
            # Another worker finished a backfill or moved the cursor during the
            # network call. Its state is the truth; we neither turn it into an
            # operational error nor write over its progress.
            continue
        except Exception as exc:  # noqa: BLE001 — one network must not take down the cycle
            stats["evm_errors"] += 1
            msg = f"{type(exc).__name__}: {exc}"[:300]
            db.note_error("last_error_evm", f"{recorded_at}: [{net}] {msg}")

    watched = db.evm_watched(networks)

    # 2) Backfill — the longest-waiting first, with a per-cycle token cap.
    # `retry` stays in the list and `error` does not: the difference between
    # them is the difference between a transient fault (timeout, rate limit, a
    # node that stumbled) and a permanent one (a single block exceeding the cap,
    # which no split can save). Merging them into one "error" bucket drops the
    # token from the ledger **forever** over one bad second — it actually
    # happened on the first live cycle: 3 of 58 tokens.
    pending = [
        w for w in watched
        if (w.get("backfill_status") or None) in (None, "partial", "retry")
    ]
    # Last tried is last to be tried again: ordering by attempt time puts the
    # transient fault back at the tail of the queue, with no "how many times"
    # column and no timer.
    pending.sort(key=lambda w: (w.get("backfill_last_try_at") or "",))
    stats["evm_backfill_due"] = len(pending)
    # A time budget for the whole step: backfill is the only one that may be cut
    # off (it resumes from its point with no lost logs), while everything after
    # it in the same cycle and the same process loses its pace if the backfill
    # eats the period — step 3 (snapshots) here, then `bsc_layer` and
    # `evm_contract` and `evm_replay` in `run_evm_replay.run_cycle`. Measured:
    # 118 seconds against a 60-second period. (And not "Solana": `chain_layer`
    # is a separate process — corrected 08-22, details at
    # `EVM_BACKFILL_BUDGET_SECONDS` in config.)
    deadline = time.monotonic() + config.EVM_BACKFILL_BUDGET_SECONDS
    for i, w in enumerate(pending[: config.EVM_BACKFILL_TOKENS_PER_CYCLE]):
        if i and time.monotonic() >= deadline:
            stats["evm_backfill_skipped"] += 1
            continue
        generation = db.evm_ledger_generation()
        try:
            await _backfill_token(rpc, db, w, recorded_at, stats, sleep, deadline)
        except StaleEVMState:
            # Another worker or a reset beat us to it. Its state is the truth; we do not write retry over it.
            continue
        except Exception as exc:  # noqa: BLE001
            stats["evm_backfill_retry"] += 1
            try:
                with db.batch():
                    db.assert_evm_ledger_generation(generation)
                    db.assert_evm_backfill_state(
                        w["network_id"], w["token_address"],
                        w.get("backfill_status"), w.get("from_block"),
                        w.get("to_block"),
                    )
                    db.set_evm_backfill_state(
                        w["network_id"], w["token_address"], "retry", recorded_at,
                        last_error=f"{type(exc).__name__}: {exc}"[:300],
                    )
            except StaleEVMState:
                stats["evm_backfill_retry"] -= 1
                continue
            db.note_error(
                "last_error_evm",
                f"{recorded_at}: {w['token_address']}: {type(exc).__name__}: {exc}",
            )
        if i + 1 < min(len(pending), config.EVM_BACKFILL_TOKENS_PER_CYCLE):
            await sleep(config.EVM_PACING_SECONDS)

    # 3) Snapshot — for the fully backfilled only. A snapshot of a
    # half-backfilled token is a false number, not an incomplete one. And the
    # read is repeated after the backfill rather than taken from `watched`
    # above: that is the state from before step 2, so a token that just finished
    # backfilling looks incomplete there and loses a cycle for no reason.
    ready = [
        w for w in db.evm_watched(networks)
        if (w.get("backfill_status") or "") == "done"
    ]
    stale_before = (
        datetime.fromisoformat(recorded_at)
        - timedelta(seconds=config.EVM_SNAPSHOT_SECONDS)
    ).isoformat()
    due = db.evm_snapshot_due(
        limit=config.EVM_SNAPSHOT_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=stale_before,
        networks=networks,
    )
    ready_keys = {(w["token_address"].lower(), str(w["network_id"])): w for w in ready}
    for w in due:
        key = (str(w["token_address"]).lower(), str(w["network_id"]))
        target = ready_keys.get(key)
        if target is None:
            continue
        try:
            _snapshot_token(db, target, recorded_at, stats)
        except StaleEVMState:
            continue
        except Exception as exc:  # noqa: BLE001
            stats["evm_errors"] += 1
            db.note_error(
                "last_error_evm",
                f"{recorded_at}: {w['token_address']}: {type(exc).__name__}: {exc}",
            )
    return stats


async def run_evm_backfill_assist(
    rpc: Any, db: RecorderDB, networks: Sequence[str], recorded_at: str | None = None,
    sleep=asyncio.sleep,
) -> dict[str, int]:
    """Backfills the live ledgers only, to run as a priority ahead of the historical replay.

    It applies no periodic logs and writes no snapshots; `FomoChain` remains
    the owner of those two steps. This worker speeds new tokens up without
    creating a second path for applying the same range.
    """
    now = recorded_at or utcnow_iso()
    stats = {
        "evm_backfill_due": 0, "evm_backfilled": 0, "evm_backfill_partial": 0,
        "evm_backfill_calls": 0, "evm_backfill_retry": 0,
        "evm_backfill_errors": 0, "evm_backfill_skipped": 0,
    }
    watched = db.evm_watched([str(n) for n in networks])
    pending = [
        w for w in watched
        if (w.get("backfill_status") or "") in ("", "partial", "retry")
    ]
    pending.sort(key=_assist_priority)
    stats["evm_backfill_due"] = len(pending)
    deadline = time.monotonic() + config.EVM_BACKFILL_ASSIST_BUDGET_SECONDS
    for index, watch in enumerate(pending[:config.EVM_BACKFILL_TOKENS_PER_CYCLE]):
        if index and time.monotonic() >= deadline:
            stats["evm_backfill_skipped"] += 1
            continue
        generation = db.evm_ledger_generation()
        try:
            await _backfill_token(
                rpc, db, watch, now, stats, sleep, deadline,
                max_calls=config.EVM_REPLAY_MAX_CALLS,
            )
        except StaleEVMState:
            continue
        except Exception as exc:  # noqa: BLE001 - the next cycle retries this token
            stats["evm_backfill_retry"] += 1
            try:
                with db.batch():
                    db.assert_evm_ledger_generation(generation)
                    db.assert_evm_backfill_state(
                        watch["network_id"], watch["token_address"],
                        watch.get("backfill_status"), watch.get("from_block"),
                        watch.get("to_block"),
                    )
                    db.set_evm_backfill_state(
                        watch["network_id"], watch["token_address"], "retry", now,
                        last_error=f"{type(exc).__name__}: {exc}"[:300],
                    )
            except StaleEVMState:
                stats["evm_backfill_retry"] -= 1
        if index + 1 < min(len(pending), config.EVM_BACKFILL_TOKENS_PER_CYCLE):
            await sleep(config.EVM_PACING_SECONDS)
    return stats


def _assist_priority(watch: dict[str, Any]) -> tuple[int, int, int, str]:
    """Newest first, then the cheapest and the nearest; the live worker keeps the rotation fair."""
    if not watch.get("backfill_status"):
        return (0, 0, 0, "")
    start = watch.get("from_block")
    end = watch.get("to_block")
    remaining = (
        max(0, int(end) - int(start) + 1)
        if start is not None and end is not None else 2 ** 63 - 1
    )
    calls = int(watch.get("backfill_calls") or 2 ** 31 - 1)
    return (1, calls, remaining, str(watch.get("backfill_last_try_at") or ""))
