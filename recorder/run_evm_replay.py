"""عامل الإعادة التاريخية المجدول لقياسات تركّز حائزي EVM.

يعمل منفصلاً عن `FomoChain`: الالتقاط الحي لا ينتظر الأرشيف القديم، والإعادة
تلتقط عملة واحدة في الدورة وتستأنف من `evm_replay_state` بعد أي توقف.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import evm_replay

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
    """يعيد الشبكة الحالية والتالية، مع استرداد آمن من إعداد قديم."""
    saved = str(db.get_meta("evm_replay_next_network") or "")
    current_index = networks.index(saved) if saved in networks else 0
    current = networks[current_index]
    following = networks[(current_index + 1) % len(networks)]
    return current, following


async def run_cycle(rpc, db) -> dict:
    import config

    networks = tuple(str(network) for network in config.EVM_REPLAY_NETWORKS)
    if not networks:
        return {"tokens": 0, "written": 0, "errors": 0, "network": None}

    network, following = _next_network(db, networks)
    stats = await evm_replay.run_replay(
        rpc,
        db,
        networks=[network],
        limit=max(1, int(config.EVM_REPLAY_TOKENS_PER_CYCLE)),
        log=_log,
        budget_seconds=config.EVM_REPLAY_BUDGET_SECONDS_PER_CYCLE,
    )
    db.set_meta("evm_replay_next_network", following)
    db.set_meta("evm_replay_last_run_at", __import__("db").utcnow_iso())
    db.set_meta("evm_replay_last_stats", str({**stats, "network": network}))
    return {**stats, "network": network}


def _check_config() -> int:
    import config
    from db import RecorderDB

    for name in (
        "EVM_REPLAY_NETWORKS", "EVM_REPLAY_INTERVAL_SECONDS",
        "EVM_REPLAY_TOKENS_PER_CYCLE", "EVM_REPLAY_BUDGET_SECONDS_PER_CYCLE",
        "EVM_REPLAY_RUN_LOG_PATH", "GOLDRUSH_REPLAY_CHAINS",
        "GOLDRUSH_BLOCK_CHUNK", "GOLDRUSH_RETRIES",
        "GOLDRUSH_MIN_RANGE",
    ):
        if not hasattr(config, name):
            print(f"config.{name} مفقود", file=sys.stderr)
            return 1
    if not config.EVM_REPLAY_NETWORKS:
        print("لا توجد شبكات إعادة EVM مفعَّلة", file=sys.stderr)
        return 1
    if config.GOLDRUSH_REPLAY_CHAINS:
        try:
            import json

            with open(config.chain_keys_path(), encoding="utf-8") as fh:
                goldrush_ok = bool((json.load(fh) or {}).get("goldrush_api_key"))
        except Exception:  # noqa: BLE001
            goldrush_ok = False
        if not goldrush_ok:
            print("مفتاح GoldRush مفقود للإعادة التاريخية", file=sys.stderr)
            return 1
    if not os.path.exists(config.DB_PATH):
        print(f"قاعدة البيانات غير موجودة: {config.DB_PATH}", file=sys.stderr)
        return 1
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        pending = sum(
            1 for row in db.evm_replay_targets(config.EVM_REPLAY_NETWORKS)
            if (row.get("replay_status") or "") not in
            ("done", "negative", "empty", "no_time", "skip")
        )
    finally:
        db.close()
    print(
        f"ok · شبكات: {','.join(map(str, config.EVM_REPLAY_NETWORKS))}"
        f" · عملة/دورة: {config.EVM_REPLAY_TOKENS_PER_CYCLE}"
        f" · معلّق: {pending}"
    )
    return 0


async def _main(cycles: int | None = None) -> None:
    import config
    from db import RecorderDB
    from goldrush_rpc import GoldRushReplayRPC

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    rpc = GoldRushReplayRPC()
    count = 0
    try:
        while cycles is None or count < cycles:
            started = time.monotonic()
            try:
                stats = await run_cycle(rpc, db)
                if stats.get("tokens") or stats.get("errors"):
                    _log(f"cycle: {stats}")
            except Exception:  # noqa: BLE001 - one cycle must not kill the worker
                import traceback

                _log("cycle crashed:\n" + traceback.format_exc())
            count += 1
            if cycles is not None and count >= cycles:
                break
            remaining = config.EVM_REPLAY_INTERVAL_SECONDS - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
    finally:
        await rpc.aclose()
        db.close()


def main() -> None:
    if "--check-config" in sys.argv:
        raise SystemExit(_check_config())
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    asyncio.run(_main(cycles))


if __name__ == "__main__":
    main()
