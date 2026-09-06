"""Reset EVM-derived ledger data after a ledger correctness fix.

The raw FOMO events, watch windows, prices, labels, and Solana measurements are
never touched. The default mode is read-only; ``--apply`` performs the reset.
After active EVM backfills finish, ``--finalize-training`` removes only EVM
training rows so the normal builder recreates them from corrected snapshots.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import evm_replay  # noqa: E402 — the final-status list lives in one place, not a copy
from db import RecorderDB, decode_raw, utcnow_iso  # noqa: E402

COHORT_META_KEY = "evm_repair_cohort"


def allowed_networks() -> set[str]:
    return {
        *(str(network) for network in config.EVM_NETWORKS),
        *(str(network) for network in config.EVM_REPLAY_NETWORKS),
    }


def validated_networks(
    networks: Sequence[str], *, require_all: bool = False,
) -> tuple[str, ...]:
    nets = tuple(dict.fromkeys(str(network) for network in networks))
    allowed = allowed_networks()
    refused = sorted(set(nets) - allowed)
    if refused:
        raise ValueError(f"disallowed EVM networks: {', '.join(refused)}")
    if require_all and set(nets) != allowed:
        raise ValueError(
            "the full EVM network set must be repaired in one operation: "
            + ", ".join(sorted(allowed))
        )
    return nets


def _marks(networks: Sequence[str]) -> str:
    return ", ".join("?" for _ in networks)


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _cohort(db: RecorderDB) -> dict | None:
    raw = db.get_meta(COHORT_META_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def capture_cohort(db: RecorderDB, networks: Sequence[str]) -> dict:
    existing = _cohort(db)
    if existing is not None:
        return existing
    nets = validated_networks(networks, require_all=True)
    live = {str(network) for network in config.EVM_NETWORKS}
    replay = {str(network) for network in config.EVM_REPLAY_NETWORKS}
    active_networks = tuple(sorted(live & set(nets)))
    active_backfills = [
        {"network_id": str(row["network_id"]),
         "token_address": str(row["token_address"]).lower()}
        for row in db._conn.execute(
            f"""SELECT network_id, token_address FROM watchlist
                WHERE active=1 AND network_id IN ({_marks(active_networks)})
                ORDER BY network_id, token_address""", active_networks,
        ).fetchall()
    ] if active_networks else []
    replay_windows = []
    for target in db.evm_replay_targets(tuple(sorted(replay & set(nets)))):
        for window in target.get("replay_windows", []):
            replay_windows.append({
                "network_id": str(target["network_id"]),
                "token_address": str(target["token_address"]).lower(),
                "first_seen_at": str(window["first_seen_at"]),
                "watch_until": str(window["watch_until"]),
            })
    cohort = {
        "version": 1,
        "captured_at": utcnow_iso(),
        "networks": sorted(nets),
        "active_backfills": active_backfills,
        "replay_windows": sorted(
            replay_windows,
            key=lambda row: (row["network_id"], row["token_address"], row["first_seen_at"]),
        ),
    }
    db.set_meta(COHORT_META_KEY, json.dumps(cohort, sort_keys=True))
    return cohort


def _replay_pending(
    db: RecorderDB, networks: Sequence[str],
    cohort_windows: Sequence[dict] | None = None,
) -> int:
    now = int(datetime.now(UTC).timestamp())
    step = max(1, int(config.EVM_REPLAY_STEP_SECONDS))
    final = set(evm_replay.FINAL_STATUSES)
    pending = 0
    targets = db.evm_replay_targets(networks)
    if cohort_windows is not None:
        wanted: dict[tuple[str, str], list[dict]] = {}
        for window in cohort_windows:
            key = (str(window["network_id"]), str(window["token_address"]).lower())
            wanted.setdefault(key, []).append(window)
        by_key = {
            (str(target["network_id"]), str(target["token_address"]).lower()): target
            for target in targets
        }
        targets = []
        for key, windows in wanted.items():
            target = dict(by_key.get(key) or {
                "network_id": key[0], "token_address": key[1],
                "replay_windows": windows, "replay_status": None,
                "replay_checkpoint_json": None,
            })
            target["replay_windows"] = windows
            targets.append(target)
    for target in targets:
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
    nets = validated_networks(networks)
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
    cohort = _cohort(db)
    if live_nets:
        live_marks = _marks(live_nets)
        if cohort:
            result["active_pending"] = sum(
                1 for item in cohort.get("active_backfills", [])
                if str(item.get("network_id")) in live_nets
                and (db.evm_backfill_state(
                    item["network_id"], item["token_address"]
                ) or {}).get("status") != "done"
            )
        else:
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
        result["replay_pending"] = _replay_pending(
            db, replay_nets,
            cohort.get("replay_windows") if cohort else None,
        )
    else:
        result["replay_pending"] = 0
    training_params: tuple = (*nets, __import__("features").FEATURE_VERSION)
    training_cutoff = _epoch(cohort["captured_at"]) if cohort else None
    training_filter = ""
    if training_cutoff is not None:
        training_filter = " AND o.entry_ts <= ?"
        training_params = (*nets, training_cutoff, __import__("features").FEATURE_VERSION)
    result["training_missing"] = int(db._conn.execute(
        f"""SELECT COUNT(*) FROM outcomes o
             WHERE o.network_id IN ({marks}) AND o.status IN ('ok','no_bars')
               {training_filter}
               AND NOT EXISTS (
                   SELECT 1 FROM training_rows r
                    WHERE r.kind=o.kind AND r.key=o.key
                      AND r.feature_version=?
               )""",
         training_params,
    ).fetchone()[0])
    return result


def reset(db: RecorderDB, networks: Sequence[str]) -> dict[str, int]:
    nets = validated_networks(networks, require_all=True)
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
    nets = validated_networks(networks, require_all=True)
    generation = db.evm_ledger_generation()
    state = inspect(db, nets)
    if state["active_pending"]:
        raise RuntimeError(
            f"cannot finalize the repair: {state['active_pending']} "
            f"incomplete active EVM ledgers"
        )
    if state["replay_pending"]:
        raise RuntimeError(
            f"cannot finalize the repair: {state['replay_pending']} "
            f"incomplete EVM replays"
        )
    if not nets:
        return 0
    marks = _marks(nets)
    cohort = _cohort(db)
    training_filter = ""
    training_params: tuple = nets
    if cohort:
        training_filter = " AND entry_ts <= ?"
        training_params = (*nets, _epoch(cohort["captured_at"]))
    started = db.get_meta("evm_training_rebuild_started") == "1"
    if not started:
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            cur = db._conn.execute(
                f"DELETE FROM training_rows WHERE network_id IN ({marks}){training_filter}",
                training_params,
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
            db._conn.execute("DELETE FROM meta WHERE key=?", (COHORT_META_KEY,))
    return 0


def wait_and_finalize(
    db: RecorderDB, networks: Sequence[str], interval_seconds: int,
) -> None:
    """Wait for the chain to finish, then let the scheduled builder rebuild
    training incrementally."""
    while True:
        state = inspect(db, networks)
        print(f"waiting: {state}", flush=True)
        if state["active_pending"] == 0 and state["replay_pending"] == 0:
            finalize_training(db, networks)
            if db.get_meta("evm_ledger_rebuild_required") == "0":
                print("EVM training rebuild complete", flush=True)
                return
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
    parser.add_argument("--capture-cohort", action="store_true")
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
        if args.capture_cohort:
            print(json.dumps(capture_cohort(db, args.networks), sort_keys=True))
        elif args.wait_finalize:
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
