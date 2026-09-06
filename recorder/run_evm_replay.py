"""The scheduled worker for historical replay of EVM holder-concentration
measurements.

It runs separately from `FomoChain`: live capture does not wait for the old
archive, and the replay captures one token per cycle and resumes from
`evm_replay_state` after any stop.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import bsc_layer
import envio_hypersync
import evm_contract
import evm_replay
import evm_layer

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _log(message: str) -> None:
    import config
    from db import utcnow_iso

    try:
        with open(config.EVM_REPLAY_RUN_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{utcnow_iso()} {message}\n")
    except OSError:
        pass


def _next_network(db, networks: tuple[str, ...]) -> tuple[str, str]:
    """Returns the current and next network, with safe recovery from a
    stale setting."""
    saved = str(db.get_meta("evm_replay_next_network") or "")
    current_index = networks.index(saved) if saved in networks else 0
    current = networks[current_index]
    following = networks[(current_index + 1) % len(networks)]
    return current, following


def _stamp(db, pairs: dict) -> None:
    """Bookkeeping stamps that write when they can and never fail a cycle
    that succeeded.

    A bare `set_meta` here used to make a database lock log "cycle crashed"
    for a cycle that completed and whose rows were actually written. The
    worst this method can lose is the rotation position between networks, so
    the same network is replayed once more — cheaper than a lie in the log
    and on the dashboard.
    """
    for key, value in pairs.items():
        db.note_error(key, value)


# Not a work counter: the network's name, the number of requested networks,
# the refused ones, and the time.
_NOT_WORK = frozenset({"network", "networks", "refused_networks", "seconds"})


def _worked(stats: dict) -> bool:
    """Did this cycle do any work or hit an error? **By exclusion, not
    enumeration**.

    The condition used to be `stats["tokens"] or stats["errors"]`, but the
    live-assist branch returns entirely different keys (`evm_backfill_*`)
    and returns before reaching those — so the log stayed silent for four
    hours while the worker ran fine, and diagnosis required reading `meta`
    instead of it. An allow-list of keys would have repeated the failure at
    the first new key, so we exclude what is not a counter and count the
    rest: a new key is counted automatically.
    """
    for key, value in stats.items():
        if key in _NOT_WORK or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value:
            return True
    return False


async def run_cycle(rpc, db, nodereal=None, hyper=None) -> dict:
    import config

    from db import utcnow_iso

    # This is the sole EVM writer. Keeping live application, backfill,
    # snapshots, contract checks, and historical replay in one connection
    # prevents concurrent SQLite writers from holding incompatible batches.
    # `hyper` (Envio HyperSync) accelerates the history reads of the networks
    # it covers — the backfill walk and, since 2026-09-06, the replay walk
    # (measured log-for-log equal to the public node in
    # `probe_hypersync_replay.py`); both fall back to the public node on any
    # refusal. `None` is the normal no-key state: everything then runs on the
    # public node exactly as before.
    live = await evm_layer.run_evm_cycle(rpc, db, utcnow_iso(), hyper=hyper)
    if nodereal is not None:
        live.update(await bsc_layer.run_bsc_cycle(nodereal, db, utcnow_iso()))
    live.update(await evm_contract.run_evm_contract_cycle(rpc, db, utcnow_iso()))
    networks = tuple(str(network) for network in config.EVM_REPLAY_NETWORKS)
    if not networks:
        return {**live, "tokens": 0, "written": 0, "errors": 0, "network": None}

    network, following = _next_network(db, networks)
    stats = await evm_replay.run_replay(
        rpc,
        db,
        networks=[network],
        limit=max(1, int(config.EVM_REPLAY_TOKENS_PER_CYCLE)),
        log=_log,
        budget_seconds=config.EVM_REPLAY_BUDGET_SECONDS_PER_CYCLE,
        hyper=hyper,
    )
    now = utcnow_iso()
    combined = {**live, **stats, "network": network}
    live_ok = not int(live.get("evm_errors") or 0) and not int(
        live.get("evm_backfill_errors") or 0
    )
    contract_ok = not int(live.get("evm_contract_errors") or 0)
    bsc_ok = "bsc_errors" in live and not int(live.get("bsc_errors") or 0)
    _stamp(db, {
        "evm_last_run_at": now,
        "evm_last_stats": str(live),
        "evm_replay_next_network": following,
        "evm_replay_last_run_at": now,
        "evm_replay_last_stats": str(combined),
        **({"evm_last_ok_at": now} if live_ok else {}),
        **({"evm_contract_last_ok_at": now} if contract_ok else {}),
        **({"bsc_nodereal_last_ok_at": now} if bsc_ok else {}),
        **({"evm_replay_last_ok_at": now} if not stats.get("errors") else {}),
    })
    return combined


def _check_config() -> int:
    import config
    from db import RecorderDB

    for name in (
        "EVM_REPLAY_NETWORKS", "EVM_REPLAY_INTERVAL_SECONDS",
        "EVM_REPLAY_TOKENS_PER_CYCLE", "EVM_REPLAY_BUDGET_SECONDS_PER_CYCLE",
        "EVM_REPLAY_RUN_LOG_PATH", "EVM_REPLAY_HEARTBEAT_SECONDS",
        "EVM_CREATION_BLOCK_NETWORKS",
        "EVM_BACKFILL_ASSIST_NETWORKS", "EVM_BACKFILL_ASSIST_BUDGET_SECONDS",
        "EVM_REPLAY_HEAD_GRACE_SECONDS",
    ):
        if not hasattr(config, name):
            print(f"config.{name} is missing", file=sys.stderr)
            return 1
    if not config.EVM_REPLAY_NETWORKS:
        print("no EVM replay networks enabled", file=sys.stderr)
        return 1
    if not os.path.exists(config.DB_PATH):
        print(f"database not found: {config.DB_PATH}", file=sys.stderr)
        return 1
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        pending = sum(
            1 for row in db.evm_replay_targets(config.EVM_REPLAY_NETWORKS)
            if (row.get("replay_status") or "") not in
            evm_replay.FINAL_STATUSES
        )
    finally:
        db.close()
    # No key line here: the whole path runs on official nodes without a key
    # (see the deleted `GOLDRUSH_REPLAY_CHAINS` in config and trap #29). If a
    # keyed provider is ever added, add its count here — **a count, not a
    # value** (FR-013).
    print(
        f"ok · networks: {','.join(map(str, config.EVM_REPLAY_NETWORKS))}"
        f" · tokens/cycle: {config.EVM_REPLAY_TOKENS_PER_CYCLE}"
        f" · pending: {pending}"
        f" · log heartbeat: {config.EVM_REPLAY_HEARTBEAT_SECONDS}s"
    )
    return 0


async def _main(cycles: int | None = None) -> None:
    import config
    import evm_rpc
    from nodereal_rpc import NodeRealRPC
    from db import RecorderDB, utcnow_iso

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    rpc = evm_rpc.EVMRPC()
    nodereal = NodeRealRPC()
    # HyperSync for the Base backfill — constructed once, key refreshed from
    # disk per call inside it. A missing key makes every `covers()` check
    # return False and the cycle runs keyless, so construction can never fail
    # the worker.
    hyper = envio_hypersync.EnvioHyperSync()
    count = 0
    # `None`, not `monotonic()`: the first cycle always logs no matter how
    # idle it is, because the startup line is the only proof that the worker
    # came up after a restart.
    last_logged: float | None = None
    try:
        while cycles is None or count < cycles:
            started = time.monotonic()
            try:
                stats = await run_cycle(rpc, db, nodereal, hyper=hyper)
                if _worked(stats):
                    _log(f"cycle: {stats}")
                    last_logged = started
                elif last_logged is None or (
                    started - last_logged >= config.EVM_REPLAY_HEARTBEAT_SECONDS
                ):
                    # A heartbeat: "alive, with no work due". Total silence
                    # looks exactly like death.
                    _log(f"idle: {stats}")
                    last_logged = started
            except Exception as exc:  # noqa: BLE001 - one cycle must not kill the worker
                import traceback

                _log("cycle crashed:\n" + traceback.format_exc())
                # Recovery **before** the stamp: a read snapshot overtaken in
                # WAL rejects every write from this connection with
                # `database is locked`, and no timeout helps — so if
                # `note_error` were written first, it would fail too, and the
                # one line the dashboard shows would be lost. And this worker
                # is the most dangerous of the four on this front: all of
                # `run_cycle` is one connection with four writing layers. The
                # detail and the measured incident are in
                # `db.recover_connection`.
                try:
                    _log(f"connection recovery: {db.recover_connection()}")
                except Exception as rec_exc:  # noqa: BLE001 — a rescue hand that must not kill the loop
                    _log(f"connection recovery failed: {type(rec_exc).__name__}")
                # And in `meta` as well: the log is a file on disk that
                # nobody reads, and the dashboard used to show every queue
                # but this one — so a persistent failure here was silent
                # twice. One line with the type and the message; the full
                # trace in the log.
                db.note_error(
                    "last_error_evm_replay",
                    f"{utcnow_iso()}: {type(exc).__name__}: {exc}"[:400],
                )
                # A failure is a line too ⇒ it postpones the heartbeat: a
                # crash says more than a heartbeat does.
                last_logged = started
            # And no pool report for this owner: there is no key on the
            # replay path, and an empty queue on the dashboard is worse than
            # its absence. (`provider_keys_replay` used to hold the GoldRush
            # pool alone, and the row was deleted with the provider.)
            count += 1
            if cycles is not None and count >= cycles:
                break
            remaining = config.EVM_REPLAY_INTERVAL_SECONDS - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
    finally:
        await rpc.aclose()
        await hyper.aclose()
        await nodereal.aclose()
        db.close()


def main() -> None:
    if "--check-config" in sys.argv:
        raise SystemExit(_check_config())
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    asyncio.run(_main(cycles))


if __name__ == "__main__":
    main()
