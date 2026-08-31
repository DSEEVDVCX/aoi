"""Tests for historical replay: time, cumulative balances, and the live
coverage boundary.

No network calls in any test. What is verified here is what, if it broke
silently, would poison the whole training set and never show up in any log:

  * **The point-in-time law**: a transfer that happened after the snapshot's
    moment never enters it.
  * **The margin is added, never subtracted**: a borderline log is delayed,
    not anticipated.
  * **A negative balance is exposed, not baked in**: a token whose reading
    started late is not written at all.
  * **Replay stops where live coverage begins**: no interleaving two chains
    at two rhythms.
"""
import os

import pytest

import config
import evm_replay
import evm_rpc
from db import RecorderDB, StaleEVMState, decode_raw

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-13T12:00:00+00:00"
NET = "4663"
TOK = "0xaaaa000000000000000000000000000000000001"
TOK2 = "0xaaaa000000000000000000000000000000000002"
A = "0x1111111111111111111111111111111111111111"
B = "0x2222222222222222222222222222222222222222"
ZERO = "0x0000000000000000000000000000000000000000"

# A timeline for the fake node: 0.1s per block (Robinhood's measured pace),
# and block `HEAD`'s time is exactly `NOW`. The chain spans 2 million seconds
# ≈ 23 days — wider than the `EVM_REPLAY_LOOKBACK_SECONDS` window (7 days), so
# the start of the range falls inside the chain, not at block zero: without
# that, the "widen the range to the start of the chain" branch is never
# tested at all.
T0 = evm_replay._epoch(NOW)
HEAD = 20_000_000
BASE = T0 - 2_000_000


def _blk(seconds_ago):
    """The block number whose time is `T0 - seconds_ago`."""
    return HEAD - 10 * int(seconds_ago)


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _topic(addr):
    return "0x" + "0" * 24 + addr[2:]


def _log(frm, to, value, block, token=TOK, ts=None):
    """A raw log. `ts=None` mimics Robinhood: `blockTimestamp: '0x0'`."""
    out = {
        "address": token,
        "topics": [evm_rpc.TRANSFER_TOPIC, _topic(frm), _topic(to)],
        "data": hex(value),
        "blockNumber": hex(block),
        "blockTimestamp": "0x0" if ts is None else hex(ts),
    }
    return out


def _watch(first_seen=NOW, until=None, token=TOK):
    return {
        "token_address": token, "network_id": NET, "first_seen_at": first_seen,
        "watch_until": until or first_seen, "entry_signal_id": "sig-1",
        "is_control": 0, "active": 1,
    }


async def _noop(_seconds):
    return None


class _FakeRPC:
    """A fake node: canned logs, and block timestamps from a known
    timeline."""

    def __init__(self, logs=(), head=1000, rate=0.1, base_ts=1_000_000,
                 complete=True, resume=None, fail_ts=False, mint=None):
        self.logs = list(logs)
        self.head = head
        self.rate = rate
        self.base_ts = base_ts
        self.complete = complete
        self.resume = resume
        self.fail_ts = fail_ts
        self.mint = mint
        self.ranges = []
        self.blocks_asked = []
        self.mint_scans = []

    async def first_mint_block(self, _net, address, head):
        """`None` by default = "unknown" ⇒ a walk from genesis, as the rest
        of the tests describe.

        And this is not a simplification but a case that must stay covered:
        a noisy token truncates the scan's response, so its bottom is not
        trusted. The successful scan is tested by passing an explicit
        `mint=`.
        """
        self.mint_scans.append((address.lower(), int(head)))
        return self.mint

    async def block_number(self, _net):
        return self.head

    async def block_timestamp(self, _net, block):
        self.blocks_asked.append(int(block))
        if self.fail_ts or int(block) > self.head:
            return None
        return int(self.base_ts + int(block) * self.rate)

    async def get_logs_paged(self, _net, _addrs, lo, hi, **_kw):
        self.ranges.append((lo, hi))
        resume = hi if self.resume is None else self.resume
        return list(self.logs), 1, self.complete, resume


class _GaggedRPC(_FakeRPC):
    """A node that gags its first `gags` timestamp calls, then answers —
    the public node's lot."""

    def __init__(self, gags=1, **kw):
        super().__init__(**kw)
        self.gags = int(gags)

    async def block_timestamp(self, net, block):
        if self.gags > 0:
            self.gags -= 1
            raise evm_rpc.EVMRateLimit("eth_getBlockByNumber [4663] HTTP 429")
        return await super().block_timestamp(net, block)


# ---------------------------------------------------------------------------
# The time grid
# ---------------------------------------------------------------------------
def test_grid_aligns_to_step_multiples_and_includes_end():
    pts = evm_replay.grid_points(1003, 1902, 300)
    assert pts == [900, 1200, 1500, 1800]


def test_grid_is_empty_when_end_precedes_start():
    assert evm_replay.grid_points(2000, 1000, 300) == []


def test_reactivated_token_replays_each_window_without_filling_gap(db):
    first = _iso_dt(T0 - 10_000)
    second = _iso_dt(T0 - 3_600)
    db.upsert_watch(TOK, NET, "trending", "sig-1", 1, first)
    db._conn.execute(
        "UPDATE watchlist SET active=0 WHERE token_address=? AND network_id=?",
        (TOK, NET),
    )
    db._conn.commit()
    db.upsert_watch(TOK, NET, "trending", "sig-2", 1, second)

    target = db.evm_replay_targets([NET])[0]
    assert len(target["replay_windows"]) == 2
    start, end, coverage = evm_replay._window(db, target, 300)
    grid = evm_replay.watch_grid(target, start, end, 300, coverage)
    gap_lo = evm_replay._epoch(first) + 3600
    gap_hi = evm_replay._epoch(second) - 600
    assert not any(gap_lo < point < gap_hi for point in grid)


def test_post_gate_young_window_is_not_sent_to_evm_workers(db, monkeypatch):
    """A rejected age consumes no live RPC and no historical replay."""
    monkeypatch.setattr(config, "AGE_GATE_ENABLED_AT", "2026-08-22T00:00:00+00:00")
    entry = "2026-08-23T12:00:00+00:00"
    db.upsert_static({
        "token_address": TOK, "network_id": NET, "recorded_at": entry,
        "token_created_at": "2026-08-23T00:00:00+00:00", "raw_json": "{}",
    })
    db.upsert_watch(TOK, NET, "large_buy", "young", 48, entry)

    assert db.evm_watched([NET]) == []
    assert db.evm_replay_targets([NET]) == []


def test_replay_window_assertion_ignores_a_rejected_later_window(db, monkeypatch):
    monkeypatch.setattr(config, "AGE_GATE_ENABLED_AT", "2026-08-22T00:00:00+00:00")
    old_entry = NOW
    young_entry = "2026-08-23T12:00:00+00:00"
    db.upsert_watch(TOK, NET, "large_buy", "old", 48, old_entry)
    db._conn.execute(
        "UPDATE watchlist SET active=0 WHERE token_address=? AND network_id=?",
        (TOK, NET),
    )
    db._conn.commit()
    db.upsert_static({
        "token_address": TOK, "network_id": NET, "recorded_at": young_entry,
        "token_created_at": "2026-08-23T00:00:00+00:00", "raw_json": "{}",
    })
    db.upsert_watch(TOK, NET, "large_buy", "young", 48, young_entry)

    target = db.evm_replay_targets([NET])[0]
    assert [row["first_seen_at"] for row in target["replay_windows"]] == [old_entry]
    assert db.evm_replay_windows(TOK, NET) == target["replay_windows"]


def test_old_live_coverage_does_not_swallow_a_later_window(db):
    first = _iso_dt(T0 - 10_000)
    second = _iso_dt(T0 - 3_600)
    db.upsert_watch(TOK, NET, "trending", "sig-1", 1, first)
    db._conn.execute(
        "UPDATE watchlist SET active=0 WHERE token_address=? AND network_id=?",
        (TOK, NET),
    )
    db._conn.commit()
    db.upsert_watch(TOK, NET, "trending", "sig-2", 1, second)
    db.insert_chain_concentration({
        "token_address": TOK, "network_id": NET,
        "recorded_at": _iso_dt(T0 - 9_400), "watch_first_seen_at": first,
        "entry_signal_id": "sig-1", "is_control": 0, "is_replay": 0,
        "top1_pct": 100.0, "raw_json": {},
    })

    target = db.evm_replay_targets([NET])[0]
    start, end, coverage = evm_replay._window(db, target, 300)
    grid = evm_replay.watch_grid(target, start, end, 300, coverage)

    second_start = evm_replay._epoch(second) - 600
    assert any(point >= second_start for point in grid)


# ---------------------------------------------------------------------------
# The block clock
# ---------------------------------------------------------------------------
def test_clock_interpolates_between_anchors(db):
    clock = evm_replay.BlockClock(db, NET)
    clock.add(1000, 100_000, NOW)
    clock.add(2000, 100_100, NOW)          # 0.1s per block
    assert clock.time_at(1500) == 100_050
    assert clock.time_at(1000) == 100_000  # an exact anchor, not extrapolation


def test_clock_extends_slope_beyond_the_last_anchor(db):
    """Every transfer of a new token sits above the last anchor — refusing
    them would mean measuring nothing."""
    clock = evm_replay.BlockClock(db, NET)
    clock.add(1000, 100_000, NOW)
    clock.add(2000, 100_100, NOW)
    assert clock.time_at(3000) == 100_200
    assert clock.time_at(0) == 99_900


def test_clock_anchors_persist_and_reload(db):
    """An anchor is a property of a block, not of a token ⇒ it is read from
    the database for the next token."""
    clock = evm_replay.BlockClock(db, NET)
    clock.add(1000, 100_000, NOW)
    again = evm_replay.BlockClock(db, NET)
    assert (again.blocks, again.times) == ([1000], [100_000])
    assert evm_replay.BlockClock(db, "8453").blocks == []


async def test_clock_probe_does_not_recall_a_known_block(db):
    rpc = _FakeRPC()
    clock = evm_replay.BlockClock(db, NET)
    assert await clock.probe(rpc, 500, NOW) == 1_000_050
    assert await clock.probe(rpc, 500, NOW) == 1_000_050
    assert rpc.blocks_asked == [500] and clock.calls == 1


async def test_a_gagged_anchor_is_waited_out_not_raised(db):
    """A gag (429) on an anchor call waits and retries — it does not drop
    the token.

    The first live run died right here: nine calls, then a 429 from
    Robinhood.
    """
    rpc = _GaggedRPC(gags=2, head=1000)
    clock = evm_replay.BlockClock(db, NET)
    assert await clock.probe(rpc, 500, NOW, _noop) == 1_000_050
    assert clock.calls == 3                 # two gagged attempts, then an answer


async def test_a_gag_that_never_lifts_is_raised_not_swallowed(db):
    """Swallowing it would write "no time for this block" — false
    information stored as if measured."""
    rpc = _GaggedRPC(gags=99, head=1000)
    clock = evm_replay.BlockClock(db, NET)
    with pytest.raises(evm_rpc.EVMRateLimit):
        await clock.probe(rpc, 500, NOW, _noop)
    assert clock.calls == int(config.EVM_REPLAY_ANCHOR_RETRIES) + 1


async def test_block_at_time_answers_on_the_older_side(db):
    """The error is deliberate toward the past: a newer block loses
    transfers ⇒ false numbers."""
    rpc = _FakeRPC(head=1_000_000)
    clock = evm_replay.BlockClock(db, NET)
    target = 1_050_000                      # = exactly block 500,000

    block = await clock.block_at_time(
        rpc, target, 1_000_000, NOW, _noop, max_calls=20,
    )

    assert block <= 500_000
    assert clock.time_at(block) <= target
    assert clock.calls <= 8                 # extrapolation, not bisection


async def test_block_at_time_falls_back_to_genesis(db):
    """A time older than every anchor ⇒ block zero: the start of the chain
    is the correct answer."""
    rpc = _FakeRPC(head=1000)
    clock = evm_replay.BlockClock(db, NET)
    got = await clock.block_at_time(rpc, 1, 1000, NOW, _noop, max_calls=20)
    assert got == 0


# ---------------------------------------------------------------------------
# Log time: the truth when given, extrapolation plus a margin when absent
# ---------------------------------------------------------------------------
def test_real_block_timestamp_is_used_without_margin(db):
    """Base and BSC give the timestamp ⇒ no extrapolation and no margin:
    it is the truth."""
    clock = evm_replay.BlockClock(db, NET)
    clock.add(0, 0, NOW)
    clock.add(100, 100, NOW)
    times, unknown = evm_replay.resolve_log_times(
        [_log(A, B, 5, 50, ts=777)], clock, margin=5,
    )
    assert (times, unknown) == ({50: 777}, 0)


def test_zero_timestamp_is_absence_not_epoch_1970(db):
    """If `'0x0'` were read as a time, every transfer would enter every
    snapshot — the opposite of what is wanted."""
    clock = evm_replay.BlockClock(db, NET)
    clock.add(0, 100_000, NOW)
    clock.add(1000, 100_100, NOW)
    times, unknown = evm_replay.resolve_log_times(
        [_log(A, B, 5, 500, ts=None)], clock, margin=5,
    )
    assert times == {500: 100_055}          # 100_050 + margin 5
    assert unknown == 0


def test_block_without_any_anchor_is_unknown_not_guessed(db):
    clock = evm_replay.BlockClock(db, NET)
    times, unknown = evm_replay.resolve_log_times(
        [_log(A, B, 5, 500, ts=None)], clock, margin=5,
    )
    assert (times, unknown) == ({}, 1)


# ---------------------------------------------------------------------------
# The heart of correctness: the grid pass
# ---------------------------------------------------------------------------
def test_balances_are_cumulative_across_grid_points():
    logs = [_log(ZERO, A, 1000, 10, ts=1000), _log(A, B, 200, 20, ts=2000)]
    times = {10: 1000, 20: 2000}
    rows, meta = evm_replay.replay_rows(
        logs, times, [900, 1500, 2500], TOK, NET, _watch(),
    )
    assert [r["recorded_at"][:19] for r in rows] == [
        evm_replay._iso(1500)[:19], evm_replay._iso(2500)[:19],
    ]
    assert [r["holder_count"] for r in rows] == [1, 2]
    assert [r["top1_pct"] for r in rows] == [100.0, 80.0]
    assert meta["empty"] == 1               # the moment 900 precedes the first transfer


def test_a_transfer_after_the_point_never_enters_it():
    """The point-in-time law: one second after the snapshot is enough to
    exclude it."""
    logs = [_log(ZERO, A, 1000, 10, ts=1000), _log(A, B, 400, 20, ts=1501)]
    rows, _ = evm_replay.replay_rows(
        logs, {10: 1000, 20: 1501}, [1500], TOK, NET, _watch(),
    )
    assert len(rows) == 1
    assert rows[0]["holder_count"] == 1     # B does not exist yet
    assert rows[0]["top1_pct"] == 100.0


def test_transfer_exactly_at_the_point_is_included():
    logs = [_log(ZERO, A, 1000, 10, ts=1000), _log(A, B, 400, 20, ts=1500)]
    rows, _ = evm_replay.replay_rows(
        logs, {10: 1000, 20: 1500}, [1500], TOK, NET, _watch(),
    )
    assert rows[0]["holder_count"] == 2


def test_margin_delays_a_borderline_log_never_advances_it(db):
    """A log on the boundary is kept out of the snapshot, not let in:
    delaying a measurement, not anticipating it."""
    clock = evm_replay.BlockClock(db, NET)
    clock.add(0, 0, NOW)
    clock.add(100, 1000, NOW)               # 10s per block
    logs = [_log(ZERO, A, 1000, 10, ts=None), _log(A, B, 400, 50, ts=None)]
    times, _ = evm_replay.resolve_log_times(logs, clock, margin=5)

    assert times == {10: 105, 50: 505}      # extrapolation 100 and 500 plus 5
    rows, _ = evm_replay.replay_rows(logs, times, [500], TOK, NET, _watch())
    assert rows[0]["holder_count"] == 1     # the second transfer was delayed out of the snapshot


def test_negative_balance_writes_nothing_at_all():
    """A late start ⇒ a send with no receive: the numbers are false, not
    incomplete."""
    logs = [_log(A, B, 200, 10, ts=1000)]   # no prior mint
    rows, meta = evm_replay.replay_rows(
        logs, {10: 1000}, [1500, 1800], TOK, NET, _watch(),
    )
    assert rows == []
    assert meta["negatives"] == 1


def test_burn_address_may_go_negative_without_voiding_the_token():
    """The mint leaves the zero address, so its balance is necessarily
    negative — and that is not a defect."""
    logs = [_log(ZERO, A, 1000, 10, ts=1000)]
    rows, meta = evm_replay.replay_rows(
        logs, {10: 1000}, [1500], TOK, NET, _watch(),
    )
    assert meta["negatives"] == 0
    assert rows[0]["holder_count"] == 1     # the zero address is not a holder


# ---------------------------------------------------------------------------
# The async layer: a whole token
# ---------------------------------------------------------------------------
def _rpc(logs=(), head=HEAD, **kw):
    return _FakeRPC(logs=list(logs), head=head, rate=0.1, base_ts=BASE, **kw)


def _live_watch(first=3600, until=60, token=TOK):
    return _watch(_iso_dt(T0 - first), _iso_dt(T0 - until), token)


def _iso_dt(ts):
    return evm_replay._iso(ts)


def _story(token=TOK):
    """A mint at `T0-3000`, then a transfer at `T0-1500`, with no timestamp
    (Robinhood)."""
    return [
        _log(ZERO, A, 1000, _blk(3000), token=token),
        _log(A, B, 200, _blk(1500), token=token),
    ]


async def test_replay_token_writes_a_full_grid_marked_is_replay(db):
    rpc = _rpc(_story())
    clock = evm_replay.BlockClock(db, NET)

    res = await evm_replay.replay_token(
        rpc, db, _live_watch(), clock, HEAD, NOW, sleep=_noop,
    )

    assert res["status"] == "done"
    assert res["written"] == res["rows"] == 9      # T0-2700 … T0-300 in steps of 300
    rows = db._conn.execute(
        "SELECT recorded_at, holder_count, top1_pct, is_replay, raw_json"
        "  FROM chain_concentration ORDER BY recorded_at"
    ).fetchall()
    assert rows[0]["recorded_at"] == _iso_dt(T0 - 2700)
    assert rows[-1]["recorded_at"] == _iso_dt(T0 - 300)
    # The transfer's time is T0-1495 (extrapolation plus margin) ⇒ it enters
    # from the T0-1200 snapshot.
    assert [r["holder_count"] for r in rows] == [1] * 5 + [2] * 4
    assert rows[-1]["top1_pct"] == 80.0
    assert {r["is_replay"] for r in rows} == {1}
    assert isinstance(decode_raw(rows[0]["raw_json"]), dict)


async def test_replay_state_is_written_and_live_pacing_is_untouched(db):
    """State is saved in its own table, and `chain_fetch_state` is
    untouched.

    Writing the live layer's pacing from here would tell it the token was
    just measured, delaying its real snapshot — a historical replay costing
    us a present measurement.
    """
    rpc = _rpc(_story())
    clock = evm_replay.BlockClock(db, NET)
    await evm_replay.replay_token(
        rpc, db, _live_watch(), clock, HEAD, NOW, sleep=_noop,
    )

    state = db.evm_replay_state(TOK, NET)
    assert state["status"] == "done"
    assert state["snapshots"] == 9 and state["balance_check"] == "ok"
    assert state["to_block"] == HEAD - int(config.EVM_CONFIRMATIONS)
    assert state["from_block"] == 0
    assert db._conn.execute("SELECT COUNT(*) c FROM chain_fetch_state").fetchone()["c"] == 0
    # And the anchors are saved for the next token on the same network.
    assert len(evm_replay.BlockClock(db, NET)) > 0


async def test_dry_run_writes_neither_rows_nor_state(db):
    rpc = _rpc(_story())
    clock = evm_replay.BlockClock(db, NET)
    res = await evm_replay.replay_token(
        rpc, db, _live_watch(), clock, HEAD, NOW, sleep=_noop, write=False,
    )
    assert res["rows"] == 9 and res["written"] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM chain_concentration"
    ).fetchone()["c"] == 0
    assert db.evm_replay_state(TOK, NET) is None


async def test_replay_rows_roll_back_when_state_write_fails(db, monkeypatch):
    rpc = _rpc([_log(ZERO, A, 1000, _blk(5000))])
    clock = evm_replay.BlockClock(db, NET)

    def _fail(*_args, **_kwargs):
        raise RuntimeError("state write failed")

    monkeypatch.setattr(db, "set_evm_replay_state", _fail)
    with pytest.raises(RuntimeError, match="state write failed"):
        await evm_replay.replay_token(
            rpc, db, _live_watch(), clock, HEAD, NOW, sleep=_noop,
        )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration WHERE is_replay=1"
    ).fetchone()[0] == 0


async def test_replay_stops_where_live_coverage_begins(db):
    """The first live row trims the window before it by a full step — no
    interleaving two chains."""
    seed, _ = evm_replay.replay_rows(
        [_log(ZERO, A, 1000, 10, ts=1000)], {10: 1000}, [1500], TOK, NET, _watch(),
    )
    live = dict(seed[0])
    live["recorded_at"] = _iso_dt(T0 - 1800)
    live["watch_first_seen_at"] = _live_watch()["first_seen_at"]
    live["is_replay"] = 0
    assert db.insert_chain_concentration(live)

    rpc = _rpc(_story())
    clock = evm_replay.BlockClock(db, NET)
    res = await evm_replay.replay_token(
        rpc, db, _live_watch(), clock, HEAD, NOW, sleep=_noop,
    )

    assert res["status"] == "done"
    last = db._conn.execute(
        "SELECT MAX(recorded_at) m FROM chain_concentration WHERE is_replay=1"
    ).fetchone()["m"]
    assert last == _iso_dt(T0 - 2100)          # 1800 + a 300 step
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM chain_concentration WHERE is_replay=1 AND recorded_at>=?",
        (_iso_dt(T0 - 1800),),
    ).fetchone()["c"] == 0


async def test_replay_aborts_if_live_coverage_starts_during_fetch(db):
    db_path = db._conn.execute("PRAGMA database_list").fetchone()["file"]
    other = RecorderDB(db_path, SCHEMA)

    class _LiveStartsDuringFetch(_FakeRPC):
        async def get_logs_paged(self, *args, **kwargs):
            seed, _ = evm_replay.replay_rows(
                [_log(ZERO, A, 1000, 10, ts=1000)],
                {10: 1000}, [1500], TOK, NET, _watch(),
            )
            live = dict(seed[0])
            live["recorded_at"] = _iso_dt(T0 - 1800)
            live["is_replay"] = 0
            other.insert_chain_concentration(live)
            return await super().get_logs_paged(*args, **kwargs)

    rpc = _LiveStartsDuringFetch(
        logs=_story(), head=HEAD, rate=0.1, base_ts=BASE,
    )
    try:
        with pytest.raises(StaleEVMState, match="live coverage"):
            await evm_replay.replay_token(
                rpc, db, _live_watch(), evm_replay.BlockClock(db, NET), HEAD, NOW,
                sleep=_noop,
            )
    finally:
        other.close()

    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration WHERE is_replay=1"
    ).fetchone()[0] == 0
    assert db.evm_replay_state(TOK, NET) is None


async def test_replay_aborts_if_a_window_is_added_during_fetch(db):
    first = _iso_dt(T0 - 7200)
    second = _iso_dt(T0 - 3600)
    db.upsert_watch(TOK, NET, "trending", "sig-1", 1, first)
    watch = db.evm_replay_targets([NET])[0]
    db_path = db._conn.execute("PRAGMA database_list").fetchone()["file"]
    other = RecorderDB(db_path, SCHEMA)

    class _WindowAddedDuringFetch(_FakeRPC):
        async def get_logs_paged(self, *args, **kwargs):
            other.add_signal_comparison_window(
                TOK, NET, "large_buy", "sig-2", 1, second, 0.001, 2,
            )
            return await super().get_logs_paged(*args, **kwargs)

    rpc = _WindowAddedDuringFetch(
        logs=_story(), head=HEAD, rate=0.1, base_ts=BASE,
    )
    try:
        with pytest.raises(StaleEVMState, match="replay windows"):
            await evm_replay.replay_token(
                rpc, db, watch, evm_replay.BlockClock(db, NET), HEAD, NOW,
                sleep=_noop,
            )
    finally:
        other.close()

    assert db.evm_replay_state(TOK, NET) is None
    assert len(db.evm_replay_targets([NET])[0]["replay_windows"]) == 2


async def test_a_window_swallowed_by_live_coverage_writes_nothing(db):
    """Live coverage older than the entry ⇒ nothing is replayed, and no
    error."""
    seed, _ = evm_replay.replay_rows(
        [_log(ZERO, A, 1000, 10, ts=1000)], {10: 1000}, [1500], TOK, NET, _watch(),
    )
    live = dict(seed[0])
    live["recorded_at"] = _iso_dt(T0 - 7200)
    live["watch_first_seen_at"] = _live_watch()["first_seen_at"]
    live["is_replay"] = 0
    db.insert_chain_concentration(live)

    clock = evm_replay.BlockClock(db, NET)
    res = await evm_replay.replay_token(
        _rpc(_story()), db, _live_watch(), clock, HEAD, NOW, sleep=_noop,
    )
    assert (res["status"], res["rows"], res["calls"]) == ("skip", 0, 0)


async def test_redo_to_skip_removes_old_replay_rows_atomically(db):
    _seed_watch(db, TOK, 3600, 1)
    await evm_replay.run_replay(
        _rpc(_story()), db, networks=[NET], sleep=_noop,
    )
    live_rows = db._conn.execute(
        "SELECT * FROM chain_concentration WHERE is_replay=1 LIMIT 1"
    ).fetchone()
    live = dict(live_rows)
    live["recorded_at"] = _iso_dt(T0 - 7200)
    live["is_replay"] = 0
    db.insert_chain_concentration(live)

    stats = await evm_replay.run_replay(
        _rpc(_story()), db, networks=[NET], sleep=_noop, redo=True,
    )
    assert stats["skip"] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration WHERE is_replay=1"
    ).fetchone()[0] == 0
    assert db.evm_replay_state(TOK, NET)["status"] == "skip"


async def test_an_unknown_block_time_writes_no_row_at_all(db):
    """Anchors forbidden ⇒ no time for the log ⇒ `no_time` with no guessed
    row."""
    rpc = _rpc(_story(), fail_ts=True)
    clock = evm_replay.BlockClock(db, NET)
    res = await evm_replay.replay_token(
        rpc, db, _live_watch(), clock, HEAD, NOW, sleep=_noop,
    )
    assert res["status"] == "no_time" and res["unknown_time"] == 2
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM chain_concentration"
    ).fetchone()["c"] == 0


async def test_a_token_with_no_transfer_is_empty_not_zero(db):
    res = await evm_replay.replay_token(
        _rpc([]), db, _live_watch(), evm_replay.BlockClock(db, NET), HEAD, NOW,
        sleep=_noop,
    )
    assert (res["status"], res["rows"]) == ("empty", 0)
    assert db.evm_replay_state(TOK, NET)["balance_check"] == "ok"


async def test_negative_balance_retries_from_genesis_once(db):
    """Replay starts from genesis; a negative after that is real corruption,
    not a short window."""
    rpc = _rpc([_log(A, B, 200, _blk(1500))])
    clock = evm_replay.BlockClock(db, NET)

    res = await evm_replay.replay_token(
        rpc, db, _live_watch(), clock, HEAD, NOW, sleep=_noop,
    )

    assert res["status"] == "negative" and res["rows"] == 0
    assert rpc.ranges[0][0] == 0
    assert res["from_block"] == 0
    assert len(rpc.ranges) == 1
    assert db.evm_replay_state(TOK, NET)["balance_check"] == "negative"


async def test_negative_prefix_stays_partial_until_the_range_is_complete(db):
    """A negative in an early part does not become a final verdict before
    the rest of the range is read."""
    _seed_watch(db, TOK, 3600, 1)
    first_rpc = _rpc(
        [_log(A, B, 200, _blk(1500))], complete=False, resume=_blk(1800),
    )

    first = await evm_replay.run_replay(
        first_rpc, db, networks=[NET], sleep=_noop,
    )

    assert first["partial"] == 1
    state = db.evm_replay_state(TOK, NET)
    assert state["status"] == "partial"
    assert decode_raw(state["checkpoint_json"])["balances"][A] == "-200"

    second = await evm_replay.run_replay(
        _rpc([], complete=True), db, networks=[NET], sleep=_noop,
    )

    assert second["negative"] == 1
    assert db.evm_replay_state(TOK, NET)["status"] == "negative"


# ---------------------------------------------------------------------------
# The whole job
# ---------------------------------------------------------------------------
def _seed_watch(db, token, first, hours):
    db.upsert_watch(
        token, NET, "trending", "sig-1", hours, _iso_dt(T0 - first - 60)
    )


async def test_run_replay_refuses_a_network_without_a_free_archive(db):
    """BSC is outside `EVM_REPLAY_NETWORKS` by measurement: no free node
    serves an old log."""
    _seed_watch(db, TOK, 3600, 1)
    stats = await evm_replay.run_replay(
        _rpc(_story()), db, networks=["56"], sleep=_noop,
    )
    assert stats["refused_networks"] == ["56"]
    assert (stats["networks"], stats["tokens"]) == (0, 0)


async def test_run_replay_skips_final_statuses_unless_redo(db):
    _seed_watch(db, TOK, 7200, 2)
    _seed_watch(db, TOK2, 3600, 1)
    db.set_evm_replay_state(TOK, NET, "done", NOW)

    once = await evm_replay.run_replay(
        _rpc(_story(TOK2)), db, networks=[NET], sleep=_noop,
    )
    assert once["tokens"] == 1 and once["done"] == 1

    again = await evm_replay.run_replay(
        _rpc(_story(TOK2)), db, networks=[NET], sleep=_noop, redo=True,
    )
    assert again["tokens"] == 2


async def test_redo_replaces_existing_replay_rows(db):
    _seed_watch(db, TOK, 3600, 1)
    await evm_replay.run_replay(
        _rpc(_story()), db, networks=[NET], sleep=_noop,
    )
    before = db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration WHERE is_replay=1"
    ).fetchone()[0]

    changed = [
        _log(ZERO, A, 1000, _blk(3000)),
        _log(A, B, 500, _blk(1500)),
    ]
    await evm_replay.run_replay(
        _rpc(changed), db, networks=[NET], sleep=_noop, redo=True,
    )

    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration WHERE is_replay=1"
    ).fetchone()[0] == before
    latest = db._conn.execute(
        "SELECT top1_pct FROM chain_concentration WHERE is_replay=1 "
        "ORDER BY recorded_at DESC LIMIT 1"
    ).fetchone()
    assert latest["top1_pct"] == 50.0


async def test_redo_failure_preserves_existing_rows(db):
    _seed_watch(db, TOK, 3600, 1)
    await evm_replay.run_replay(
        _rpc(_story()), db, networks=[NET], sleep=_noop,
    )
    before = db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration WHERE is_replay=1"
    ).fetchone()[0]

    class _Failing(_FakeRPC):
        async def get_logs_paged(self, *_args, **_kwargs):
            raise evm_rpc.EVMRPCError("boom")

    stats = await evm_replay.run_replay(
        _Failing(head=HEAD, rate=0.1, base_ts=BASE), db,
        networks=[NET], sleep=_noop, redo=True,
    )
    assert stats["errors"] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration WHERE is_replay=1"
    ).fetchone()[0] == before
    assert db.evm_replay_state(TOK, NET)["status"] == "done"


async def test_active_window_stays_partial_until_head_reaches_end(db):
    future_end = _iso_dt(T0 + 3600)
    watch = _watch(_iso_dt(T0 - 3600), future_end)
    res = await evm_replay.replay_token(
        _rpc(_story()), db, watch, evm_replay.BlockClock(db, NET), HEAD, NOW,
        sleep=_noop,
    )
    assert res["status"] == "partial"


async def test_finished_window_caps_replay_at_window_end(db, monkeypatch):
    """A finished window does not fetch the network's blocks that came
    after it."""
    watch = _watch(_iso_dt(T0 - 7200), _iso_dt(T0 - 3600))
    monkeypatch.setattr(config, "EVM_REPLAY_HEAD_GRACE_SECONDS", 0)
    rpc = _rpc([_log(ZERO, A, 1000, _blk(5000))])

    result = await evm_replay.replay_token(
        rpc, db, watch, evm_replay.BlockClock(db, NET), HEAD, NOW,
        sleep=_noop,
    )

    assert result["status"] == "done"
    assert rpc.ranges[0][1] < HEAD - 1000


async def test_partial_is_retried_because_a_gag_is_not_a_verdict(db):
    """`partial` is not final: the call cap ran out or a passing gag — no
    verdict on the token."""
    _seed_watch(db, TOK, 3600, 1)
    db.set_evm_replay_state(TOK, NET, "partial", NOW)
    stats = await evm_replay.run_replay(
        _rpc(_story()), db, networks=[NET], sleep=_noop,
    )
    assert stats["tokens"] == 1


async def test_partial_replay_resumes_from_saved_block(db):
    """A partial state starts from the saved resume point, not from the
    start of the window every time."""
    _seed_watch(db, TOK, 3600, 1)

    first_rpc = _rpc(_story(), complete=False, resume=_blk(1800))
    first = await evm_replay.run_replay(
        first_rpc, db, networks=[NET], sleep=_noop,
    )
    assert first["partial"] == 1
    state = db.evm_replay_state(TOK, NET)
    assert state["from_block"] == _blk(1800)
    checkpoint = decode_raw(state["checkpoint_json"])
    assert checkpoint["balances"]
    assert checkpoint["next_grid"] > 0

    second_rpc = _rpc(_story(), complete=True)
    second = await evm_replay.run_replay(
        second_rpc, db, networks=[NET], sleep=_noop,
    )
    assert second_rpc.ranges[0][0] == _blk(1800)
    assert second["done"] == 1


async def test_replay_starts_at_the_first_mint_where_there_is_no_archive(db):
    """Replay needs the mint scan more than the live layer does: it walks
    every token from its genesis.

    And what was measured on the live chain is that three of four Robinhood
    tokens were minted above 67% of the chain's length, so `from_block = 0`
    reads 27–37 million blocks carrying not a single transfer — exactly the
    calls that used to run out before the first snapshot.
    """
    _seed_watch(db, TOK, 3600, 1)
    mint = _blk(4000)
    rpc = _rpc(_story(), mint=mint)

    result = await evm_replay.run_replay(
        rpc, db, networks=[NET], sleep=_noop,
    )

    assert result["tokens"] == 1
    assert rpc.mint_scans and rpc.mint_scans[0][0] == TOK
    assert rpc.ranges[0][0] == mint


async def test_an_unknown_mint_block_replays_from_genesis_instead_of_guessing(db):
    """`None` = "unknown" ⇒ the full walk. A false lower bound drops the
    whole token."""
    _seed_watch(db, TOK, 3600, 1)
    rpc = _rpc(_story(), mint=None)

    await evm_replay.run_replay(rpc, db, networks=[NET], sleep=_noop)

    assert rpc.mint_scans and rpc.ranges[0][0] == 0


async def test_a_window_staled_token_resumes_from_its_checkpoint_not_genesis(db):
    """`window` is an invalidated verdict with a walk still standing — so
    it resumes from the checkpoint, not from genesis.

    A new window used to delete the whole state row, so the token read as
    "never attempted" and was walked from block zero.
    `db.stale_evm_replay_verdict` now keeps the walk and invalidates the
    verdict alone — which is worth nothing unless `window` is in the
    checkpoint-resume set here: the row is read but its walk ignored, and
    the failure returns as it was.
    """
    _seed_watch(db, TOK, 3600, 1)
    next_grid = evm_replay.grid_points(T0 - 4200, T0 - 4200, 300)[0]
    db.set_evm_replay_state(
        TOK, NET, "partial", NOW, from_block=100, to_block=99,
        transfers=3, snapshots=0, calls=40,
        checkpoint={"balances": {A: "1000"}, "next_grid": next_grid},
    )
    db.stale_evm_replay_verdict(TOK, NET)
    db._conn.commit()
    assert db.evm_replay_state(TOK, NET)["status"] == "window"

    rpc = _rpc([], complete=False, resume=200)
    await evm_replay.run_replay(rpc, db, networks=[NET], sleep=_noop)

    assert rpc.ranges, "the token was not picked — `window` became final by mistake"
    assert rpc.ranges[0][0] == 100, "a walk from genesis: the checkpoint was ignored"
    # And the resumed balance is present: the resume total is not rebuilt
    # from nothing.
    state = db.evm_replay_state(TOK, NET)
    assert decode_raw(state["checkpoint_json"])["balances"] == {A: "1000"}
    assert state["calls"] >= 40, "the cap counter resets when the verdict is invalidated"


async def test_a_token_that_spends_its_call_cap_stops_final_with_a_checkpoint(
    db, monkeypatch,
):
    """A cap per token: an eternal `partial` eats the budget of tokens that
    finish.

    `EVM_REPLAY_MAX_CALLS` caps the **cycle**, not the token, so a token
    that returns `partial` every cycle resumes forever. Measured on Base:
    two tokens spent 15,270 and 11,665 calls for zero rows, versus ~4,960
    calls for the heaviest legitimate walk — so an overrun is a silent
    defect, not slowness.
    """
    monkeypatch.setattr(config, "EVM_REPLAY_TOKEN_CALL_CAP", 5)
    _seed_watch(db, TOK, 3600, 1)
    next_grid = evm_replay.grid_points(T0 - 4200, T0 - 4200, 300)[0]
    db.set_evm_replay_state(
        TOK, NET, "partial", NOW, from_block=100, to_block=99,
        transfers=3, snapshots=0, calls=5,
        checkpoint={"balances": {A: "1000"}, "next_grid": next_grid},
    )

    result = await evm_replay.run_replay(
        _rpc([], complete=False, resume=200), db, networks=[NET], sleep=_noop,
    )

    assert result["budget"] == 1
    state = db.evm_replay_state(TOK, NET)
    assert state["status"] == "budget"
    # The resume point and the checkpoint are both saved: stopping is not
    # burning finished work — raising the cap or `--redo` continues from
    # here, not from zero.
    assert state["from_block"] == 200
    assert decode_raw(state["checkpoint_json"])["balances"] == {A: "1000"}
    assert "token call cap" in (state["last_error"] or "")

    # And it is not retried: `budget` is final, otherwise the cap would be a
    # counter with no effect.
    again = await evm_replay.run_replay(
        _rpc([], complete=False, resume=200), db, networks=[NET], sleep=_noop,
    )
    assert again["tokens"] == 0
    redone = await evm_replay.run_replay(
        _rpc([], complete=False, resume=200), db, networks=[NET], sleep=_noop,
        redo=True,
    )
    assert redone["tokens"] == 1


async def test_the_call_cap_never_overrides_a_verdict_the_data_gives(db, monkeypatch):
    """The cap applies to `partial` alone: a negative balance is a verdict
    on the data, not on the budget.

    Mixing the two hides `negative` — that is, false numbers — behind "the
    cap ran out", so the token looks like unfinished work when it is
    rejected outright, and a door of endless replay opens for it whenever
    the cap is raised.
    """
    monkeypatch.setattr(config, "EVM_REPLAY_TOKEN_CALL_CAP", 1)
    _seed_watch(db, TOK, 3600, 1)
    # A transfer from a holder that never received anything ⇒ a negative
    # balance once the range completes.
    rpc = _rpc([_log(A, B, 500, _blk(3000), ts=T0 - 3000)])

    result = await evm_replay.run_replay(rpc, db, networks=[NET], sleep=_noop)

    assert result["negative"] == 1 and result["budget"] == 0
    assert db.evm_replay_state(TOK, NET)["balance_check"] == "negative"


async def test_empty_partial_replay_jumps_to_contract_creation(db, monkeypatch):
    """An empty checkpoint may have skipped only the blocks that preceded
    the contract's existence."""
    _seed_watch(db, TOK, 3600, 1)
    next_grid = evm_replay.grid_points(T0 - 4200, T0 - 4200, 300)[0]
    db.set_evm_replay_state(
        TOK, NET, "partial", NOW, from_block=100, to_block=99,
        transfers=0, snapshots=0, calls=1,
        checkpoint={"balances": {}, "next_grid": next_grid},
    )
    monkeypatch.setattr(config, "EVM_CREATION_BLOCK_NETWORKS", (NET,))
    # The network is in one set, not both: here we declare that it keeps an
    # archive, so `eth_getCode` is the answer. The mint scan is its
    # substitute where there is no archive — it has no peer.
    monkeypatch.setattr(config, "EVM_MINT_SCAN_NETWORKS", ())
    rpc = _rpc([], complete=False, resume=800)

    async def creation_block(network_id, address, head):
        assert (network_id, address) == (NET, TOK)
        return 700

    rpc.contract_creation_block = creation_block

    result = await evm_replay.run_replay(
        rpc, db, networks=[NET], sleep=_noop,
    )

    assert result["partial"] == 1
    assert rpc.ranges[0][0] == 700


async def test_redo_negative_replay_ignores_old_counters_for_creation_jump(db, monkeypatch):
    """A previously rejected attempt must not force a fresh replay to scan
    from genesis."""
    _seed_watch(db, TOK, 3600, 1)
    db.set_evm_replay_state(
        TOK, NET, "negative", NOW, from_block=700, to_block=900,
        transfers=123, snapshots=0, calls=45, balance_check="negative",
    )
    monkeypatch.setattr(config, "EVM_CREATION_BLOCK_NETWORKS", (NET,))
    # The network is in one set, not both: here we declare that it keeps an
    # archive, so `eth_getCode` is the answer. The mint scan is its
    # substitute where there is no archive — it has no peer.
    monkeypatch.setattr(config, "EVM_MINT_SCAN_NETWORKS", ())
    rpc = _rpc([], complete=False, resume=800)

    async def creation_block(network_id, address, head):
        assert (network_id, address) == (NET, TOK)
        return 700

    rpc.contract_creation_block = creation_block

    result = await evm_replay.run_replay(
        rpc, db, networks=[NET], sleep=_noop, redo=True,
    )

    assert result["partial"] == 1
    assert rpc.ranges[0][0] == 700


async def test_partial_replay_does_not_skip_a_block_when_rpc_makes_no_progress(db):
    _seed_watch(db, TOK, 3600, 1)

    first_rpc = _rpc([], complete=False, resume=0)
    first = await evm_replay.run_replay(
        first_rpc, db, networks=[NET], sleep=_noop,
    )
    assert first["partial"] == 1
    assert db.evm_replay_state(TOK, NET)["from_block"] == 0

    second_rpc = _rpc([], complete=False, resume=0)
    second = await evm_replay.run_replay(
        second_rpc, db, networks=[NET], sleep=_noop,
    )
    assert second_rpc.ranges[0][0] == 0
    assert second["partial"] == 1
    assert db.evm_replay_state(TOK, NET)["from_block"] == 0


async def test_no_progress_does_not_write_future_rows_from_stale_checkpoint(db):
    _seed_watch(db, TOK, 3600, 1)
    next_grid = evm_replay.grid_points(T0 - 4200, T0 - 4200, 300)[0]
    db.set_evm_replay_state(
        TOK, NET, "partial", NOW, from_block=100, to_block=99,
        checkpoint={"balances": {A: "1000"}, "next_grid": next_grid},
    )

    rpc = _rpc([], complete=False, resume=100)
    result = await evm_replay.run_replay(
        rpc, db, networks=[NET], sleep=_noop,
    )

    state = db.evm_replay_state(TOK, NET)
    assert result["partial"] == 1
    assert state["from_block"] == 100
    assert decode_raw(state["checkpoint_json"])["next_grid"] == next_grid
    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration WHERE is_replay=1"
    ).fetchone()[0] == 0


async def test_partial_redo_resumes_its_replacement_checkpoint(db):
    _seed_watch(db, TOK, 3600, 1)
    await evm_replay.run_replay(
        _rpc(_story()), db, networks=[NET], sleep=_noop,
    )

    first_rpc = _rpc(_story(), complete=False, resume=_blk(1800))
    first = await evm_replay.run_replay(
        first_rpc, db, networks=[NET], sleep=_noop, redo=True,
    )
    assert first["partial"] == 1
    assert db.evm_replay_state(TOK, NET)["status"] == "partial"

    second_rpc = _rpc(_story(), complete=True)
    second = await evm_replay.run_replay(
        second_rpc, db, networks=[NET], sleep=_noop, redo=True,
    )
    assert second_rpc.ranges[0][0] == _blk(1800)
    assert second["done"] == 1


async def test_incomplete_empty_prefix_stays_partial(db):
    """A partial range before the first transfer is not a final verdict
    that the token is empty."""
    _seed_watch(db, TOK, 3600, 1)
    rpc = _rpc([], complete=False, resume=_blk(1800))

    stats = await evm_replay.run_replay(rpc, db, networks=[NET], sleep=_noop)

    assert stats["partial"] == 1
    assert stats["empty"] == 0
    assert db.evm_replay_state(TOK, NET)["status"] == "partial"


async def test_one_token_error_does_not_stop_the_rest(db):
    _seed_watch(db, TOK, 7200, 2)
    _seed_watch(db, TOK2, 3600, 1)

    class _Flaky(_FakeRPC):
        async def get_logs_paged(self, net, addrs, lo, hi, **kw):
            if TOK in [a.lower() for a in addrs]:
                raise evm_rpc.EVMRPCError("boom")
            return await super().get_logs_paged(net, addrs, lo, hi, **kw)

    stats = await evm_replay.run_replay(
        _Flaky(logs=_story(TOK2), head=HEAD, rate=0.1, base_ts=BASE),
        db, networks=[NET], sleep=_noop,
    )

    assert (stats["errors"], stats["tokens"], stats["done"]) == (1, 1, 1)
    assert db.evm_replay_state(TOK, NET)["status"] == "error"
    assert db.evm_replay_state(TOK2, NET)["status"] == "done"
    assert db._conn.execute(
        "SELECT COUNT(DISTINCT token_address) c FROM chain_concentration"
    ).fetchone()["c"] == 1


async def test_limit_stops_after_one_token_and_leaves_the_next_due(db):
    _seed_watch(db, TOK, 7200, 2)
    _seed_watch(db, TOK2, 3600, 1)
    stats = await evm_replay.run_replay(
        _rpc(_story()), db, networks=[NET], limit=1, sleep=_noop,
    )
    assert stats["tokens"] == 1
    assert db.evm_replay_state(TOK2, NET) is None


async def test_limit_rotates_a_recent_partial_behind_untried_tokens(db):
    _seed_watch(db, TOK, 7200, 2)
    _seed_watch(db, TOK2, 3600, 1)
    db.set_evm_replay_state(
        TOK, NET, "partial", NOW, from_block=100, to_block=99,
        transfers=1, snapshots=0, calls=1,
        checkpoint={"balances": {A: "1"}, "next_grid": T0 - 3600},
    )

    stats = await evm_replay.run_replay(
        _rpc(_story(TOK2)), db, networks=[NET], limit=1, sleep=_noop,
    )

    assert stats["tokens"] == 1
    assert db.evm_replay_state(TOK, NET)["status"] == "partial"
    assert db.evm_replay_state(TOK2, NET) is not None


async def test_check_reports_without_touching_the_network(db):
    _seed_watch(db, TOK, 3600, 1)
    assert evm_replay._check(db) == 0





