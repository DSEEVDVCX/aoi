"""Reset EVM-derived ledger data after a ledger correctness fix.

The raw FOMO events, watch windows, prices, labels, and Solana measurements are
never touched. The default mode is read-only; ``--apply`` performs the reset.
After active EVM backfills finish, ``--finalize-training`` removes only EVM
training rows so the normal builder recreates them from corrected snapshots.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from db import RecorderDB, decode_raw  # noqa: E402


def _allowed_networks() -> set[str]:
    return {
        *(str(network) for network in config.EVM_NETWORKS),
        *(str(network) for network in config.EVM_REPLAY_NETWORKS),
    }


def _validated_networks(
    networks: Sequence[str], *, require_all: bool = False,
) -> tuple[str, ...]:
    nets = tuple(dict.fromkeys(str(network) for network in networks))
    allowed = _allowed_networks()
    refused = sorted(set(nets) - allowed)
    if refused:
        raise ValueError(f"شبكات EVM غير مسموح بها: {', '.join(refused)}")
    if require_all and set(nets) != allowed:
        raise ValueError(
            "يجب إصلاح مجموعة شبكات EVM كاملة في عملية واحدة: "
            + ", ".join(sorted(allowed))
        )
    return nets


def _marks(networks: Sequence[str]) -> str:
    return ", ".join("?" for _ in networks)


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _replay_pending(db: RecorderDB, networks: Sequence[str]) -> int:
    now = int(datetime.now(UTC).timestamp())
    step = max(1, int(config.EVM_REPLAY_STEP_SECONDS))
    final = {"done", "negative", "empty", "no_time", "skip"}
    pending = 0
    for target in db.evm_replay_targets(networks):
        mature = [
            window for window in target.get("replay_windows", [])
            if _epoch(window["watch_until"]) <= now
        ]
        if not mature or (target.get("replay_status") or "") in final:
            continue
        checkpoint = None
        raw = target.get("replay_checkpoint_json")
        if raw is not None:
            try:
                decoded = decode_raw(raw)
                checkpoint = decoded if isinstance(decoded, dict) else None
            except Exception:  # noqa: BLE001 - a corrupt checkpoint stays pending
                checkpoint = None
        last_mature_grid = max(_epoch(window["watch_until"]) for window in mature)
        last_mature_grid = (last_mature_grid // step) * step
        next_grid = int((checkpoint or {}).get("next_grid") or 0)
        if next_grid <= last_mature_grid:
            pending += 1
    return pending


def inspect(db: RecorderDB, networks: Sequence[str]) -> dict[str, int]:
    nets = _validated_networks(networks)
    if not nets:
        return {"balances": 0, "live_snapshots": 0, "replay_snapshots": 0,
                "backfills": 0, "cursors": 0, "training_rows": 0,
                "active_pending": 0, "replay_pending": 0,
                "training_missing": 0}
    marks = _marks(nets)
    live_nets = tuple(net for net in nets if net in {str(n) for n in config.EVM_NETWORKS})
    replay_nets = tuple(
        net for net in nets if net in {str(n) for n in config.EVM_REPLAY_NETWORKS}
    )
    queries = {
        "balances": f"SELECT COUNT(*) FROM evm_balances WHERE network_id IN ({marks})",
        "live_snapshots": (
            "SELECT COUNT(*) FROM chain_concentration "
            f"WHERE network_id IN ({marks}) AND COALESCE(is_replay, 0)=0"
        ),
        "replay_snapshots": (
            "SELECT COUNT(*) FROM chain_concentration "
            f"WHERE network_id IN ({marks}) AND is_replay=1"
        ),
        "backfills": f"SELECT COUNT(*) FROM evm_backfill_state WHERE network_id IN ({marks})",
        "cursors": f"SELECT COUNT(*) FROM evm_block_cursor WHERE network_id IN ({marks})",
        "training_rows": f"SELECT COUNT(*) FROM training_rows WHERE network_id IN ({marks})",
    }
    result = {
        name: int(db._conn.execute(sql, nets).fetchone()[0])
        for name, sql in queries.items()
    }
    if live_nets:
        live_marks = _marks(live_nets)
        result["active_pending"] = int(db._conn.execute(
            "SELECT COUNT(*) FROM watchlist w LEFT JOIN evm_backfill_state b "
            "ON b.network_id=w.network_id AND b.token_address=w.token_address "
            f"WHERE w.active=1 AND w.network_id IN ({live_marks}) "
            "AND COALESCE(b.status, '') <> 'done'",
            live_nets,
        ).fetchone()[0])
    else:
        result["active_pending"] = 0
    if replay_nets:
        result["replay_pending"] = _replay_pending(db, replay_nets)
    else:
        result["replay_pending"] = 0
    result["training_missing"] = int(db._conn.execute(
        f"""SELECT COUNT(*) FROM outcomes o
             WHERE o.network_id IN ({marks}) AND o.status IN ('ok','no_bars')
               AND NOT EXISTS (
                   SELECT 1 FROM training_rows r
                    WHERE r.kind=o.kind AND r.key=o.key
                      AND r.feature_version=?
               )""",
        (*nets, __import__("features").FEATURE_VERSION),
    ).fetchone()[0])
    return result


def reset(db: RecorderDB, networks: Sequence[str]) -> dict[str, int]:
    nets = _validated_networks(networks, require_all=True)
    if not nets:
        return inspect(db, nets)
    marks = _marks(nets)
    concentration_columns = (
        "onchain_top1_pct", "onchain_top5_pct", "onchain_top10_pct",
        "onchain_top20_pct", "onchain_top_accounts", "onchain_age_min",
        "onchain_top1_delta_5m", "onchain_top10_delta_5m",
        "onchain_delta_span_min", "onchain_holder_count",
        "onchain_holders_delta_5m",
    )
    with db.batch():
        db.bump_evm_ledger_generation()
        db._conn.execute(
            f"DELETE FROM evm_balances WHERE network_id IN ({marks})", nets,
        )
        db._conn.execute(
            f"DELETE FROM evm_backfill_state WHERE network_id IN ({marks})", nets,
        )
        db._conn.execute(
            f"DELETE FROM evm_block_cursor WHERE network_id IN ({marks})", nets,
        )
        db._conn.execute(
            f"DELETE FROM chain_fetch_state WHERE network_id IN ({marks})", nets,
        )
        db._conn.execute(
            f"DELETE FROM chain_concentration WHERE network_id IN ({marks})",
            nets,
        )
        db._conn.execute(
            f"DELETE FROM evm_replay_state WHERE network_id IN ({marks})", nets,
        )
        assignments = ", ".join(f"{column}=NULL" for column in concentration_columns)
        db._conn.execute(
            f"UPDATE training_rows SET {assignments} WHERE network_id IN ({marks})",
            nets,
        )
        db.set_meta("evm_training_rebuild_started", "0")
        db.set_meta("evm_ledger_rebuild_required", "1")
    return inspect(db, nets)


def finalize_training(db: RecorderDB, networks: Sequence[str]) -> int:
    nets = _validated_networks(networks, require_all=True)
    generation = db.evm_ledger_generation()
    state = inspect(db, nets)
    if state["active_pending"]:
        raise RuntimeError(
            f"لا يمكن إنهاء الإصلاح: {state['active_pending']} دفتر EVM نشط غير مكتمل"
        )
    if state["replay_pending"]:
        raise RuntimeError(
            f"لا يمكن إنهاء الإصلاح: {state['replay_pending']} إعادة EVM غير مكتملة"
        )
    if not nets:
        return 0
    marks = _marks(nets)
    started = db.get_meta("evm_training_rebuild_started") == "1"
    if not started:
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            cur = db._conn.execute(
                f"DELETE FROM training_rows WHERE network_id IN ({marks})", nets,
            )
            db.set_meta("evm_training_rebuild_started", "1")
        return int(cur.rowcount)
    missing = inspect(db, nets)["training_missing"]
    if missing == 0:
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            if inspect(db, nets)["training_missing"] != 0:
                return 0
            db.set_meta("evm_training_rebuild_started", "0")
            db.set_meta("evm_ledger_rebuild_required", "0")
    return 0


def wait_and_finalize(
    db: RecorderDB, networks: Sequence[str], interval_seconds: int,
) -> None:
    """انتظر اكتمال السلسلة، ثم دع البنّاء المجدول يعيد التدريب تدريجياً."""
    while True:
        state = inspect(db, networks)
        print(f"waiting: {state}", flush=True)
        if state["active_pending"] == 0 and state["replay_pending"] == 0:
            finalize_training(db, networks)
            state = inspect(db, networks)
            if state["training_missing"] == 0 and db.get_meta(
                "evm_ledger_rebuild_required"
            ) == "0":
                print("EVM training rebuild complete", flush=True)
                return
        time.sleep(max(10, int(interval_seconds)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--finalize-training", action="store_true")
    parser.add_argument("--wait-finalize", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    default_networks = sorted({
        *(str(network) for network in config.EVM_NETWORKS),
        *(str(network) for network in config.EVM_REPLAY_NETWORKS),
    })
    parser.add_argument("--networks", nargs="*", default=default_networks)
    args = parser.parse_args()
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        before = inspect(db, args.networks)
        print(f"before: {before}")
        if args.wait_finalize:
            wait_and_finalize(db, args.networks, args.interval)
        elif args.finalize_training:
            removed = finalize_training(db, args.networks)
            print(f"training rows queued for rebuild: {removed}")
        elif args.apply:
            after = reset(db, args.networks)
            print(f"after: {after}")
        else:
            print("read-only check; pass --apply to reset derived EVM ledger data")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
