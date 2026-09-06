import json
import os
from datetime import datetime

import pytest
import repair_evm_ledger
from db import RecorderDB

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-13T12:00:00+00:00"
NET = "4663"
TOK = "0xaaaa000000000000000000000000000000000001"


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()


def _seed(db):
    db.upsert_watch(TOK, NET, "large_buy", "sig", 48, NOW)
    db.evm_apply_transfers(
        NET, TOK,
        {"0x1111111111111111111111111111111111111111": (100, 1)}, NOW,
    )
    db.set_evm_cursor(NET, 10, NOW, "ok")
    db.set_evm_backfill_state(NET, TOK, "done", NOW, from_block=11, to_block=10)
    db.set_chain_state(TOK, NET, "ok", 100.0, NOW)
    db.insert_chain_concentration({
        "token_address": TOK, "network_id": NET, "recorded_at": NOW,
        "watch_first_seen_at": NOW, "is_control": 0,
        "top1_pct": 100.0, "is_replay": 0,
        "raw_json": {},
    })


def test_reset_removes_all_evm_ledger_derivatives(db, monkeypatch):
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_REPLAY_NETWORKS", (NET,))
    _seed(db)
    replay = {
        "token_address": TOK, "network_id": NET,
        "recorded_at": "2026-08-13T11:00:00+00:00", "watch_first_seen_at": NOW,
        "is_control": 0, "top1_pct": 100.0, "is_replay": 1, "raw_json": {},
    }
    assert db.insert_chain_concentration(replay)

    after = repair_evm_ledger.reset(db, [NET])

    assert after["balances"] == 0
    assert after["live_snapshots"] == 0
    assert after["replay_snapshots"] == 0
    assert after["backfills"] == 0
    assert after["cursors"] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration WHERE is_replay=1"
    ).fetchone()[0] == 0
    assert db.get_meta("evm_ledger_rebuild_required") == "1"


def test_finalize_refuses_while_active_backfill_is_pending(db, monkeypatch):
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_REPLAY_NETWORKS", (NET,))
    db.upsert_watch(TOK, NET, "large_buy", "sig", 48, NOW)
    with pytest.raises(RuntimeError, match="incomplete"):
        repair_evm_ledger.finalize_training(db, [NET])


def test_finalize_refuses_while_replay_is_pending(db, monkeypatch):
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_REPLAY_NETWORKS", (NET,))
    db.upsert_watch(TOK, NET, "large_buy", "sig", 0, NOW)
    db.set_evm_backfill_state(NET, TOK, "done", NOW)
    with pytest.raises(RuntimeError, match="incomplete EVM replays"):
        repair_evm_ledger.finalize_training(db, [NET])


def test_non_live_network_does_not_wait_for_live_backfill(db, monkeypatch):
    base = "8453"
    db.upsert_watch(TOK, base, "large_buy", "sig", 0, NOW)
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", (NET,))
    state = repair_evm_ledger.inspect(db, [base])
    assert state["active_pending"] == 0
    assert state["replay_pending"] == 1


def test_future_active_window_does_not_block_archive_finalize(db):
    db.upsert_watch(TOK, "8453", "large_buy", "sig", 48)
    state = repair_evm_ledger.inspect(db, ["8453"])
    assert state["replay_pending"] == 0


def test_mature_window_still_blocks_when_same_token_has_a_future_window(db):
    old = "2026-08-10T12:00:00+00:00"
    db.upsert_watch(TOK, "8453", "large_buy", "old", 1, old)
    db._conn.execute(
        "UPDATE watchlist SET active=0 WHERE token_address=? AND network_id=?",
        (TOK, "8453"),
    )
    db._conn.commit()
    db.upsert_watch(TOK, "8453", "large_buy", "new", 48)

    state = repair_evm_ledger.inspect(db, ["8453"])

    assert state["replay_pending"] == 1


def test_covered_mature_window_does_not_wait_for_same_tokens_future_window(db):
    old = "2026-08-10T12:00:00+00:00"
    db.upsert_watch(TOK, "8453", "large_buy", "old", 1, old)
    db._conn.execute(
        "UPDATE watchlist SET active=0 WHERE token_address=? AND network_id=?",
        (TOK, "8453"),
    )
    db._conn.commit()
    db.upsert_watch(TOK, "8453", "large_buy", "new", 48)
    old_end = int(datetime.fromisoformat(old).timestamp()) + 3600
    db.set_evm_replay_state(
        TOK, "8453", "partial", NOW, from_block=10,
        checkpoint={"balances": {}, "next_grid": old_end + 300},
    )

    state = repair_evm_ledger.inspect(db, ["8453"])

    assert state["replay_pending"] == 0


def test_reset_rejects_a_non_evm_network_without_deleting_it(db):
    solana = "1399811149"
    db.upsert_watch("SoLmint111", solana, "large_buy", "sig", 48, NOW)
    db.insert_chain_concentration({
        "token_address": "SoLmint111", "network_id": solana,
        "recorded_at": NOW, "watch_first_seen_at": NOW,
        "is_control": 0, "top1_pct": 25.0, "is_replay": 0, "raw_json": {},
    })

    with pytest.raises(ValueError, match="disallowed"):
        repair_evm_ledger.reset(db, [solana])

    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration WHERE network_id=?", (solana,),
    ).fetchone()[0] == 1


def test_finalize_queues_training_rebuild_only_once(db, monkeypatch):
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_REPLAY_NETWORKS", (NET,))
    db.set_meta("evm_ledger_rebuild_required", "1")
    queued = repair_evm_ledger.finalize_training(db, [NET])
    assert queued == 0
    assert db.get_meta("evm_training_rebuild_started") == "1"

    again = repair_evm_ledger.finalize_training(db, [NET])
    assert again == 0
    assert db.get_meta("evm_ledger_rebuild_required") == "0"


def test_reset_requires_the_complete_configured_network_set(db):
    with pytest.raises(ValueError, match="full EVM network set"):
        repair_evm_ledger.reset(db, [NET])


def test_default_networks_include_live_and_replay(monkeypatch):
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", ("4663",))
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_REPLAY_NETWORKS", ("4663", "8453"))
    expected = {"4663", "8453"}
    actual = {
        *(str(network) for network in repair_evm_ledger.config.EVM_NETWORKS),
        *(str(network) for network in repair_evm_ledger.config.EVM_REPLAY_NETWORKS),
    }
    assert actual == expected


def test_capture_cohort_freezes_live_tokens_and_replay_windows(db, monkeypatch):
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_REPLAY_NETWORKS", ("8453",))
    db.upsert_watch(TOK, NET, "large_buy", "sig", 48, NOW)
    db.upsert_watch(TOK, "8453", "large_buy", "replay", 1, "2026-08-10T12:00:00+00:00")

    cohort = repair_evm_ledger.capture_cohort(db, [NET, "8453"])

    assert cohort["networks"] == ["4663", "8453"]
    assert cohort["active_backfills"] == [{"network_id": NET, "token_address": TOK}]
    assert cohort["replay_windows"] == [{
        "network_id": "8453", "token_address": TOK.lower(),
        "first_seen_at": "2026-08-10T12:00:00+00:00",
        "watch_until": "2026-08-10T13:00:00+00:00",
    }]
    assert json.loads(db.get_meta(repair_evm_ledger.COHORT_META_KEY)) == cohort


def test_capture_cohort_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_REPLAY_NETWORKS", ())
    db.upsert_watch(TOK, NET, "large_buy", "sig", 48, NOW)

    first = repair_evm_ledger.capture_cohort(db, [NET])
    db.upsert_watch("0xbbbb000000000000000000000000000000000002", NET,
                    "large_buy", "new", 48, NOW)

    assert repair_evm_ledger.capture_cohort(db, [NET]) == first


def test_cohort_finalization_ignores_new_coins_after_capture(db, monkeypatch):
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_REPLAY_NETWORKS", ())
    db.upsert_watch(TOK, NET, "large_buy", "sig", 48, NOW)
    db.set_evm_backfill_state(NET, TOK, "done", NOW)
    repair_evm_ledger.capture_cohort(db, [NET])
    db.set_meta("evm_ledger_rebuild_required", "1")

    new_token = "0xbbbb000000000000000000000000000000000002"
    db.upsert_watch(new_token, NET, "large_buy", "new", 48, NOW)

    assert repair_evm_ledger.inspect(db, [NET])["active_pending"] == 0
    repair_evm_ledger.finalize_training(db, [NET])
    assert db.get_meta("evm_training_rebuild_started") == "1"


def test_expired_cohort_token_remains_in_worker_queue(db, monkeypatch):
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_REPLAY_NETWORKS", ())
    db.upsert_watch(TOK, NET, "large_buy", "sig", 48, NOW)
    repair_evm_ledger.capture_cohort(db, [NET])
    db._conn.execute(
        "UPDATE watchlist SET active=0 WHERE token_address=? AND network_id=?",
        (TOK, NET),
    )
    db._conn.commit()

    assert [row["token_address"] for row in db.evm_watched([NET])] == [TOK]


def test_completed_rebuild_clears_cohort(db, monkeypatch):
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(repair_evm_ledger.config, "EVM_REPLAY_NETWORKS", ())
    repair_evm_ledger.capture_cohort(db, [NET])
    db.set_meta("evm_ledger_rebuild_required", "1")
    db.set_meta("evm_training_rebuild_started", "1")

    repair_evm_ledger.finalize_training(db, [NET])

    assert db.get_meta(repair_evm_ledger.COHORT_META_KEY) is None
    assert db.get_meta("evm_ledger_rebuild_required") == "0"
