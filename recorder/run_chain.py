# -*- coding: utf-8 -*-
"""Chain-layer launcher for the scheduled task FomoChain (pythonw, stderr hidden).

A fourth process alongside the recorder, the labeler and the build-rows task.
Why separate rather than a step in the recorder's cycle — see the intro of
`chain_layer.py`: the budget is full, and the separation is what makes a
5-minute cadence possible.

A third writer on the database, safe for the same reasons as
`run_build_rows.py`: WAL is on, `RecorderDB` sets `timeout=30`, and inserts
are `INSERT OR IGNORE` with a time-bearing key, so repeats do no harm.

**The key is never printed or logged** (FR-013): it is read from
`recorder/chain_keys.json` on every call, and every error message passes
through redaction inside `solana_rpc`. `--check-config` says "present/absent"
and never the value. EVM's key belongs to `run_evm_replay.py` alone so two
writers never contend over SQLite.

Usage:
  python run_chain.py                # infinite loop (the scheduled task)
  python run_chain.py 1              # single cycle (manual check)
  python run_chain.py --check-config # pre-scheduling check
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_BOOT_LOG = os.path.join(HERE, "chain_boot.log")


def _log_boot(msg: str) -> None:
    try:
        with open(_BOOT_LOG, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


def _log(msg: str) -> None:
    import config

    _write_log(config.CHAIN_LOG_PATH, msg)


def _write_log(path: str, msg: str) -> None:
    import config
    from db import utcnow_iso

    line = f"{utcnow_iso()} {msg}\n"
    try:
        if (
            config.LOG_MAX_BYTES > 0
            and os.path.exists(path)
            and os.path.getsize(path) > config.LOG_MAX_BYTES
        ):
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def _stamp(db, pairs: dict[str, str]) -> None:
    """Bookkeeping stamps written when possible that never sink a successful cycle.

    A bare `set_meta` used to sit in the cycle body: a database lock there
    jumps to the guard, which logs "stalled" and writes `last_error_*` for a
    cycle that **actually ran** — a lie on the dashboard on top of a lost
    stamp. The recorder exited with code 1 the same way on 2026-08-17 (see
    `db.note_error`).
    """
    for key, value in pairs.items():
        db.note_error(key, value)


def _ok_stamps(stats: dict, at: str) -> dict[str, str]:
    """A "last success" stamp per queue — not one stamp for the whole process.

    The dashboard marks an error recovered when a success from its own source
    came after it. The threshold used was the **recorder's** (`last_ok_cycle_at`),
    which only `recorder.py` writes; if the recorder died, the threshold froze
    and the chain/evm badges stayed red forever no matter how many clean
    cycles `FomoChain` completed. So each queue gets its stamp from its own
    writer.

    Rule: zero errors in that queue ⇒ stamp — regardless of how much work was
    due, the same rule as the recorder's `last_ok_cycle_at`. Requiring actual
    work would keep an idle queue (no tokens on its networks) red forever with
    no path to recovery.
    """
    def _clean(*counters: str) -> bool:
        return not any(int(stats.get(name) or 0) for name in counters)

    out: dict[str, str] = {}
    if "chain_errors" in stats and _clean("chain_errors"):
        out["chain_last_ok_at"] = at
    if "auth_errors" in stats and _clean("auth_errors"):
        out["chain_auth_last_ok_at"] = at
    return out


async def main_loop(cycles: int | None = None) -> None:
    import time

    import asyncio

    import config
    from chain_layer import run_chain_auth_cycle, run_chain_cycle
    from db import RecorderDB, utcnow_iso
    from provider_keys import write_pool_report
    from solana_rpc import ChainKeyMissing, SolanaRPC

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    rpc = SolanaRPC()
    n = 0
    try:
        while cycles is None or n < cycles:
            started = time.time()
            try:
                stats = await run_chain_cycle(rpc, db, utcnow_iso())
                # The slow layer runs in the same process and same
                # connection: its hourly queue needs only six tokens a
                # minute, so a fifth scheduled task for it would be a whole
                # process for ~8 seconds of an otherwise empty minute.
                stats.update(await run_chain_auth_cycle(rpc, db, utcnow_iso()))
                stats["seconds"] = round(time.time() - started, 1)
                now = utcnow_iso()
                _stamp(db, {
                    "chain_last_run_at": now, "chain_last_stats": str(stats),
                    **_ok_stamps(stats, now),
                })
                # Log only when work happened — a line every minute forever
                # is noise.
                if stats["chain_due"] or stats["auth_due"]:
                    _log(f"chain: {stats}")
            except ChainKeyMissing as exc:
                # One clear line then wait: the loop must not die (the key
                # may land on disk later without restarting the task) and
                # must not spam every minute.
                _log(f"chain key missing: {exc}")
                db.note_error("last_error_chain", f"{utcnow_iso()}: chain key missing")
                await asyncio.sleep(max(config.CHAIN_INTERVAL_SECONDS, 60))
            except Exception:  # noqa: BLE001 — the cycle's shield; the loop must not die
                import traceback

                _log("chain cycle crashed:\n" + traceback.format_exc())
                # Then rescue the connection: the shield alone turns a
                # permanent fault into a dropped cycle every minute forever.
                # Measured: **382 consecutive cycles** all crashed at
                # `set_chain_state` with `database is locked` from 2026-08-22
                # 19:26:58Z to 08-23 11:13:47Z, and only a task restart at
                # 10:57Z released them — not the loop. The lock itself was
                # free: a fresh connection took `BEGIN IMMEDIATE` in 0s in 12
                # of 12 samples, so the stuck thing was our read snapshot,
                # not the database (`db.recover_connection`).
                try:
                    _log(f"connection recovery: {db.recover_connection()}")
                except Exception as rec_exc:  # noqa: BLE001 — the rescue hand must not kill the loop
                    _log(f"connection recovery failed: {type(rec_exc).__name__}")
            # The pool report sits **outside** the chain guard: key state is
            # the most important thing to read when the cycle stumbles, so it
            # must not sink with the branch that stumbled.
            try:
                # The EVM layer is not in the report: official RPCs with no
                # key, so no pool.
                write_pool_report(db, "chain", {
                    "helius": rpc.key_stats(),
                }, utcnow_iso())
            except Exception:  # noqa: BLE001 — a report, not a measurement
                pass
            n += 1
            if cycles is not None and n >= cycles:
                break
            # Sleep the rest of the period, not the whole period: a cycle
            # that consumed 20s must not wait another 60, or the real cadence
            # drifts far from the advertised one.
            rest = config.CHAIN_INTERVAL_SECONDS - (time.time() - started)
            if rest > 0:
                await asyncio.sleep(rest)
    finally:
        await rpc.aclose()
        db.close()


def _check_config() -> int:
    """Pre-scheduling check. **Never prints the key** (FR-013)."""
    import config
    from db import RecorderDB
    from provider_keys import read_keys

    for name in (
        "CHAIN_INTERVAL_SECONDS", "CHAIN_PER_CYCLE", "CHAIN_REFRESH_SECONDS",
        "CHAIN_ERROR_RETRY_SECONDS", "CHAIN_PACING_SECONDS",
        "CHAIN_TIMEOUT_SECONDS", "CHAIN_NETWORKS", "SOLANA_RPC_URL",
        "CHAIN_TRANSIENT_RETRIES", "CHAIN_TRANSIENT_BACKOFF_SECONDS",
        "CHAIN_MAX_SLOT_LAG",
        "CHAIN_LOG_PATH", "CHAIN_AUTH_PER_CYCLE", "CHAIN_AUTH_REFRESH_SECONDS",
        "CHAIN_AUTH_ERROR_RETRY_SECONDS",
    ):
        if not hasattr(config, name):
            print(f"config.{name} is missing", file=sys.stderr)
            return 1
    if not os.path.exists(config.DB_PATH):
        print(f"database not found: {config.DB_PATH}", file=sys.stderr)
        return 1

    key_path = config.chain_keys_path()
    helius_keys = read_keys("helius_api_keys", "helius_api_key", "HELIUS_API_KEY")
    key_ok = bool(helius_keys)
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        row = db._conn.execute(
            "SELECT COUNT(*) n FROM watchlist WHERE active=1 AND network_id IN "
            f"({', '.join('?' for _ in config.CHAIN_NETWORKS)})",
            tuple(config.CHAIN_NETWORKS),
        ).fetchone()
        active = int(row["n"])
    finally:
        db.close()

    sweep = (
        active / config.CHAIN_PER_CYCLE * (config.CHAIN_INTERVAL_SECONDS / 60.0)
        if config.CHAIN_PER_CYCLE else 0.0
    )
    auth_sweep = (
        active / config.CHAIN_AUTH_PER_CYCLE * (config.CHAIN_INTERVAL_SECONDS / 60.0)
        if config.CHAIN_AUTH_PER_CYCLE else 0.0
    )
    print(
        f"{'ok' if key_ok else 'warning: no key'} · key: "
        f"{str(len(helius_keys)) + ' present' if key_ok else 'absent — ' + key_path} "
        f"· networks: {', '.join(config.CHAIN_NETWORKS)} "
        f"· active on them: {active} "
        f"· every {config.CHAIN_INTERVAL_SECONDS}s × {config.CHAIN_PER_CYCLE} "
        f"-> full sweep every ~{sweep:.1f}m (target "
        f"{config.CHAIN_REFRESH_SECONDS / 60:.0f}m)"
        f" · slow layer: ×{config.CHAIN_AUTH_PER_CYCLE} -> ~{auth_sweep:.1f}m "
        f"(target {config.CHAIN_AUTH_REFRESH_SECONDS / 60:.0f}m)"
    )
    # **Counts only, never any value** (FR-013). A single key means "no
    # fallback on rejection", so it is said explicitly: multiplicity exists in
    # the code and is useless in a pool of one.
    def _count(keys: list[str], *, required: bool) -> str:
        if not keys:
            return "absent" if required else "absent (optional)"
        return f"{len(keys)}" + (" — no fallback on rejection" if len(keys) == 1 else "")

    print(
        f"keys: helius {_count(helius_keys, required=True)} · file: {key_path}"
    )
    return 0 if key_ok else 1


def main() -> None:
    import asyncio

    import config  # adds api/src to sys.path on import

    cycles = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        cycles = int(sys.argv[1])

    _log_boot(f"boot ok, db={config.DB_PATH}, cycles={cycles}")
    asyncio.run(main_loop(cycles=cycles))


if __name__ == "__main__":
    try:
        if "--check-config" in sys.argv:
            raise SystemExit(_check_config())
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — boot errors before the loop starts
        import traceback

        _log_boot("BOOT FAILURE:\n" + traceback.format_exc())
        raise
