"""Tests for the selective-return tool: what it touches, and what it
refuses to touch.

The danger here is not a math bug but over-deletion: the tool is run by
hand against the live training database, so what gets examined is the
boundaries — a live snapshot is not touched, a finished token is not
wiped, another network is not hit, and display writes nothing.
"""
import os

import pytest
import reset_evm_replay
from db import RecorderDB

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-13T12:00:00+00:00"
NET = "8453"
OTHER = "4663"
TOK = "0xaaaa000000000000000000000000000000000001"
TOK2 = "0xaaaa000000000000000000000000000000000002"


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()


def _row(token, net, *, replay):
    return {
        "token_address": token, "network_id": net, "recorded_at": NOW,
        "watch_first_seen_at": NOW, "is_control": 0, "top1_pct": 100.0,
        "is_replay": 1 if replay else 0, "raw_json": {},
    }


def _seed(db, token, net, status, *, calls=0, replay_rows=0, live_rows=0):
    db.upsert_watch(token, net, "trending", "sig", 48, NOW)
    db.set_evm_replay_state(token, net, status, NOW, calls=calls)
    for index in range(replay_rows):
        assert db.insert_chain_concentration({
            **_row(token, net, replay=True),
            "recorded_at": f"2026-08-13T10:{index:02d}:00+00:00",
        })
    for index in range(live_rows):
        assert db.insert_chain_concentration({
            **_row(token, net, replay=False),
            "recorded_at": f"2026-08-13T11:{index:02d}:00+00:00",
        })


def _seed_orphan(db, token, net, rows):
    """Replay rows with no `evm_replay_state` — what a path deleted after
    writing leaves behind."""
    db.upsert_watch(token, net, "trending", "sig", 48, NOW)
    for index in range(rows):
        assert db.insert_chain_concentration({
            **_row(token, net, replay=True),
            "recorded_at": f"2026-08-13T09:{index:02d}:00+00:00",
        })


def _count(db, net, replay):
    return db._conn.execute(
        """SELECT COUNT(*) FROM chain_concentration
            WHERE network_id=? AND is_replay=?""",
        (net, 1 if replay else 0),
    ).fetchone()[0]


def test_candidates_match_status_and_network_only(db):
    _seed(db, TOK, NET, "error", calls=240)
    _seed(db, TOK2, NET, "done", calls=99)
    _seed(db, TOK, OTHER, "error", calls=7)

    found = reset_evm_replay.candidates(db, (NET,), ("error",))

    assert [(r["token_address"], r["network_id"]) for r in found] == [(TOK, NET)]
    assert found[0]["calls"] == 240


def test_candidates_are_ordered_by_calls_spent(db):
    """Heaviest by calls first: its line is the one most worth reading
    before approving the deletion."""
    _seed(db, TOK, NET, "partial", calls=5)
    _seed(db, TOK2, NET, "partial", calls=5_660)

    found = reset_evm_replay.candidates(db, (NET,), ("partial",))

    assert [r["token_address"] for r in found] == [TOK2, TOK]


def test_candidates_count_the_replay_rows_that_a_reset_would_delete(db):
    """The count is shown before `--apply` because it is the only
    unrecoverable cost."""
    _seed(db, TOK, NET, "partial", replay_rows=3, live_rows=2)

    found = reset_evm_replay.candidates(db, (NET,), ("partial",))

    assert found[0]["rows_written"] == 3      # live rows are not this tool's business


def test_reset_returns_the_token_to_the_queue_and_leaves_live_rows(db):
    _seed(db, TOK, NET, "error", calls=240, replay_rows=3, live_rows=2)

    done = reset_evm_replay.reset(
        db, reset_evm_replay.candidates(db, (NET,), ("error",)),
    )

    assert done == {"tokens": 1, "rows_deleted": 3, "calls_freed": 240}
    # deleting the state row **is** the return: state is derived, and its
    # absence means "not replayed yet".
    assert db.evm_replay_state(TOK, NET) is None
    assert _count(db, NET, replay=True) == 0
    assert _count(db, NET, replay=False) == 2


def test_reset_does_not_touch_another_network_on_the_same_token(db):
    """The reason the tool exists: a path separated from one network, not
    from the whole ledger."""
    _seed(db, TOK, NET, "error", replay_rows=2)
    _seed(db, TOK, OTHER, "done", replay_rows=5)

    reset_evm_replay.reset(
        db, reset_evm_replay.candidates(db, (NET,), ("error",)),
    )

    assert db.evm_replay_state(TOK, OTHER)["status"] == "done"
    assert _count(db, OTHER, replay=True) == 5


def test_a_done_token_is_refused_not_silently_skipped(db, monkeypatch):
    """`done` is finished work: deleting it drops snapshots recoverable
    only by a full walk.

    And the refusal is by exit code, not silent skipping: an operator who
    wrote `--status done --apply` and read "matched: 0" would think the
    network clean and hunt for the defect somewhere healthy.
    """
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET, "--status", "done", "--apply",
    ])
    assert reset_evm_replay.main() == 2


def test_an_unknown_network_is_refused_before_any_write(db, monkeypatch):
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", "999", "--apply",
    ])
    assert reset_evm_replay.main() == 2


def test_the_default_run_writes_nothing(db, tmp_path, monkeypatch, capsys):
    """Display is the default: a manual tool on a live database must not
    execute by accident."""
    _seed(db, TOK, NET, "error", calls=240, replay_rows=3)
    db._conn.commit()
    monkeypatch.setattr(
        reset_evm_replay.config, "DB_PATH", str(tmp_path / "t.db"),
    )
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET,
    ])

    assert reset_evm_replay.main() == 0

    assert "display only" in capsys.readouterr().out
    assert db.evm_replay_state(TOK, NET)["status"] == "error"
    assert _count(db, NET, replay=True) == 3


def test_apply_resets_only_the_first_max_tokens(db, tmp_path, monkeypatch, capsys):
    """A cap on the batch: 76 tokens returned in one run buy themselves the
    whole cycle's budget."""
    _seed(db, TOK, NET, "error", calls=240)
    _seed(db, TOK2, NET, "error", calls=10)
    db._conn.commit()
    monkeypatch.setattr(
        reset_evm_replay.config, "DB_PATH", str(tmp_path / "t.db"),
    )
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET, "--max", "1", "--apply",
    ])

    assert reset_evm_replay.main() == 0

    assert "returned 1 tokens" in capsys.readouterr().out
    assert db.evm_replay_state(TOK, NET) is None            # heaviest by calls first
    assert db.evm_replay_state(TOK2, NET)["status"] == "error"


def test_an_orphan_is_invisible_to_candidates_which_is_why_orphans_exists(db):
    """The reason `orphans` exists: `--status` cannot catch what has no
    state."""
    _seed_orphan(db, TOK, NET, 4)

    assert reset_evm_replay.candidates(db, (NET,), ("error", "partial", "")) == []
    found = reset_evm_replay.orphans(db, (NET,))
    assert [(r["token_address"], r["rows_written"]) for r in found] == [(TOK, 4)]


def test_orphans_ignores_live_rows_and_other_networks(db):
    """A live snapshot is not an orphan: it was measured at its moment, and
    no replay state is expected of it."""
    _seed_orphan(db, TOK, NET, 2)
    _seed(db, TOK2, NET, "partial", replay_rows=3, live_rows=5)   # has state ⇒ not an orphan
    _seed_orphan(db, TOK, OTHER, 7)

    found = reset_evm_replay.orphans(db, (NET,))

    assert [r["token_address"] for r in found] == [TOK]
    assert found[0]["rows_written"] == 2


def test_orphans_carry_the_candidate_shape_so_one_printer_serves_both(db):
    """`_describe` reads the fields unguarded, so a missing field here
    would crash a whole run."""
    _seed_orphan(db, TOK, NET, 3)
    _seed(db, TOK2, NET, "error", replay_rows=2)

    stray = reset_evm_replay.orphans(db, (NET,))
    picked = reset_evm_replay.candidates(db, (NET,), ("error",))

    assert set(stray[0]) == set(picked[0])
    assert reset_evm_replay._describe(stray)          # no KeyError and no None in the math


def test_an_orphan_is_reported_even_when_not_requested(db, tmp_path, monkeypatch, capsys):
    """Showing costs nothing: a silent orphan stays in the ledger forever."""
    _seed_orphan(db, TOK, NET, 6)
    db._conn.commit()
    monkeypatch.setattr(reset_evm_replay.config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET,
    ])

    assert reset_evm_replay.main() == 0

    out = capsys.readouterr().out
    assert "orphans (informational only, never deleted): 1 tokens · 6 rows" in out
    assert "audit_evm_ledger.py" in out              # the road to a verdict
    assert _count(db, NET, replay=True) == 6         # news, not deletion


def test_no_flag_deletes_an_orphan_because_its_rows_are_the_measured_part(db,
        tmp_path, monkeypatch, capsys):
    """Orphanhood is a lost state, not corrupted rows; and its source is a
    deliberate path (`db.admit`).

    There used to be an `--orphans` that included them in deletion, and it
    was wrong: `audit_evm_ledger.py` read 77 snapshots on 4663 and 8453
    and their coverage came out 100.000000% — deletion would have
    destroyed correct, measured training data and required a new walk.
    What they lack is a state row the next walk writes on its own.
    """
    _seed_orphan(db, TOK, NET, 6)
    db._conn.commit()
    monkeypatch.setattr(reset_evm_replay.config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET, "--status", "error", "--apply",
    ])

    assert reset_evm_replay.main() == 0
    assert _count(db, NET, replay=True) == 6

    # and the argument itself is gone: no back door returns them without
    # review
    monkeypatch.setattr(reset_evm_replay.sys, "argv", [
        "reset_evm_replay.py", "--networks", NET, "--orphans", "--apply",
    ])
    with pytest.raises(SystemExit):
        reset_evm_replay.main()
    assert _count(db, NET, replay=True) == 6
