"""Tests for the EVM layer: log decoding, splitting on truncation, the ledger, and the cycle (no network).

No network call in any test: the client is fake and returns handmade logs,
and the ledger is read from a temporary database. What is verified is what
silence corrupts: a missing balance, a range applied twice, a snapshot built
on a half-filled ledger.
"""
import json
import os
import sqlite3

import config
import evm_layer
import evm_rpc
import httpx
import pytest
from db import RecorderDB, decode_raw

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-13T12:00:00+00:00"
NET = "4663"
TOK = "0xaaaa000000000000000000000000000000000001"

A = "0x1111111111111111111111111111111111111111"
B = "0x2222222222222222222222222222222222222222"
C = "0x3333333333333333333333333333333333333333"
ZERO = "0x0000000000000000000000000000000000000000"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _watch(db, token=TOK, *, network=NET, control=False):
    db.upsert_watch(token, network, "large_buy", f"sig-{token[:8]}", 48, NOW)
    if control:
        db._conn.execute(
            "UPDATE watchlist SET is_control=1, entry_signal_id=NULL "
            "WHERE token_address=?",
            (token,),
        )
        db._conn.commit()


# ---------------------------------------------------------------------------
# Transient faults: a read timeout is a wait, not a break (measured on 4663,
# 2026-08-17)
# ---------------------------------------------------------------------------
def _mock_rpc(handler):
    rpc = evm_rpc.EVMRPC(urls={NET: "https://node.test/rpc"})
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return rpc


async def test_read_timeout_is_classified_as_rate_limit_not_hard_error():
    """It used to fall into the generic `except Exception` ⇒ EVMRPCError was never raised."""
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    rpc = _mock_rpc(handler)
    try:
        with pytest.raises(evm_rpc.EVMRateLimit):
            await rpc._call(NET, "eth_getLogs", [])
    finally:
        await rpc.aclose()


async def test_block_number_retries_a_transient_read_timeout(monkeypatch):
    """The network's anchor: its failure drops the whole 4663 scan, not one token."""
    monkeypatch.setattr(config, "EVM_RATE_LIMIT_BACKOFF_SECONDS", 0)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x64"})

    rpc = _mock_rpc(handler)
    try:
        assert await rpc.block_number(NET) == 100
    finally:
        await rpc.aclose()
    assert len(calls) == 2


async def test_block_number_gives_up_after_the_configured_retries(monkeypatch):
    monkeypatch.setattr(config, "EVM_RATE_LIMIT_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(config, "EVM_HEAD_RETRIES", 2)
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("timed out", request=request)

    rpc = _mock_rpc(handler)
    try:
        with pytest.raises(evm_rpc.EVMRateLimit):
            await rpc.block_number(NET)
    finally:
        await rpc.aclose()
    assert len(calls) == 3          # one attempt + two retries, then it raises instead of going silent


def _topic(addr):
    return "0x" + "0" * 24 + addr[2:]


def _log(frm, to, value, block, token=TOK):
    """A raw `Transfer` log as the node returns it (hex values as text)."""
    return {
        "address": token,
        "topics": [evm_rpc.TRANSFER_TOPIC, _topic(frm), _topic(to)],
        "data": hex(value),
        "blockNumber": hex(block),
    }


# ---------------------------------------------------------------------------
# Decoding the log
# ---------------------------------------------------------------------------
def test_decode_transfer_reads_value_from_data_not_topics():
    rec = evm_rpc.decode_transfer(_log(A, B, 12_345, 900))
    assert rec == {
        "token_address": TOK, "from": A, "to": B, "value": 12_345, "block": 900,
    }


def test_decode_transfer_keeps_uint256_exactly():
    """A value beyond 64 bits: the arithmetic is unbounded in Python; passing through a float would corrupt it."""
    big = 2 ** 200 + 7
    rec = evm_rpc.decode_transfer(_log(A, B, big, 1))
    assert rec["value"] == big


def test_decode_transfer_rejects_non_standard_event():
    """A shared signature with different fields: it is ignored, not guessed."""
    bad = _log(A, B, 1, 1)
    bad["topics"] = bad["topics"][:2]
    assert evm_rpc.decode_transfer(bad) is None


def test_decode_transfer_rejects_wrong_signature_and_malformed_words():
    wrong = _log(A, B, 1, 1)
    wrong["topics"][0] = "0x" + "11" * 32
    assert evm_rpc.decode_transfer(wrong) is None

    short_topic = _log(A, B, 1, 1)
    short_topic["topics"][1] = _topic(A)[:-2]
    assert evm_rpc.decode_transfer(short_topic) is None

    oversized_value = _log(A, B, 1, 1)
    oversized_value["data"] = "0x1" + "00" * 32
    assert evm_rpc.decode_transfer(oversized_value) is None

    bad_token = _log(A, B, 1, 1)
    bad_token["address"] = "not-an-address"
    assert evm_rpc.decode_transfer(bad_token) is None


def test_deltas_aggregate_and_sign():
    """An address that moves twice ⇒ one write, and the sign distinguishes sender from receiver."""
    logs = [_log(A, B, 100, 10), _log(B, C, 30, 11)]
    out = evm_layer._deltas_by_token(logs)
    assert out[TOK][A] == (-100, 10, None)
    assert out[TOK][B] == (70, 11, 10)        # +100 then −30
    assert out[TOK][C] == (30, 11, 11)


def test_deltas_keep_burn_address():
    """The zero address's balance is information (how much was burned) — the exception belongs in the ratio calculation."""
    out = evm_layer._deltas_by_token([_log(A, ZERO, 50, 5)])
    assert ZERO in out[TOK]


# ---------------------------------------------------------------------------
# Splitting on truncation
# ---------------------------------------------------------------------------
class _PagingRPC(evm_rpc.EVMRPC):
    """Inherits the real client and replaces only `get_logs`: what gets split is what we test."""

    def __init__(self, limit_above=100, per_block=None):
        self.ranges = []
        self._limit_above = limit_above
        self._per_block = per_block or {}

    async def get_logs(self, network_id, addresses, from_block, to_block, topics=None):
        self.ranges.append((from_block, to_block))
        if to_block - from_block > self._limit_above:
            raise evm_rpc.EVMLogLimit("exceeds limit of 10000")
        out = []
        for blk in range(from_block, to_block + 1):
            out.extend(self._per_block.get(blk, []))
        return out


async def _noop(_seconds):
    pass


async def test_paging_halves_range_until_accepted():
    rpc = _PagingRPC(limit_above=100, per_block={150: [_log(A, B, 5, 150)]})

    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 400, max_calls=50, sleep=_noop,
    )

    assert complete is True
    assert resume == 400
    assert calls == len(rpc.ranges) > 1
    assert len(logs) == 1
    # No gap and no overlap: the split covers the whole range exactly once.
    covered = sorted(r for r in rpc.ranges if r[1] - r[0] <= 100)
    merged = []
    for lo, hi in covered:
        if merged and lo == merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], hi)
        else:
            merged.append((lo, hi))
    assert merged == [(0, 400)]


async def test_paging_uses_configured_range_hint_after_first_rejection(monkeypatch):
    rpc = _PagingRPC(limit_above=100)
    monkeypatch.setitem(config.EVM_LOG_RANGE_HINT, NET, 100)

    _logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 400, max_calls=10, sleep=_noop,
    )

    assert complete is True
    assert resume == 400
    assert calls == 6
    assert rpc.ranges == [
        (0, 400), (0, 99), (100, 199), (200, 299), (300, 399), (400, 400),
    ]


async def test_contract_creation_block_uses_historical_code_binary_search():
    class _HistoricalCodeRPC:
        def __init__(self):
            self.blocks = []

        async def get_code_at(self, network_id, address, block):
            self.blocks.append((network_id, address, block))
            return "0x6000" if block >= 700 else "0x"

        async def historical_block_number(self, _network_id):
            return 1_000

    rpc = _HistoricalCodeRPC()

    block = await evm_rpc.EVMRPC.contract_creation_block(rpc, NET, TOK, 1_000)

    assert block == 700
    assert len(rpc.blocks) < 15


async def test_contract_creation_block_returns_none_for_an_eoa():
    class _NoCodeRPC:
        async def get_code_at(self, _network_id, _address, _block):
            return "0x"

        async def historical_block_number(self, _network_id):
            return 1_000

    assert await evm_rpc.EVMRPC.contract_creation_block(
        _NoCodeRPC(), NET, TOK, 1_000,
    ) is None


async def test_paging_reads_oldest_first():
    """Chronological order is a correctness requirement for the balance when it is pinned at zero."""
    rpc = _PagingRPC(limit_above=100)
    await rpc.get_logs_paged(NET, [TOK], 0, 400, max_calls=50, sleep=_noop)
    accepted = [r for r in rpc.ranges if r[1] - r[0] <= 100]
    assert accepted == sorted(accepted)


async def test_paging_stops_at_call_cap_and_reports_resume():
    """Reaching the cap ⇒ `complete=False` and a resume block, not a lying "done"."""
    rpc = _PagingRPC(limit_above=10)

    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 1000, max_calls=3, sleep=_noop,
    )

    assert calls == 3
    assert complete is False
    # All three were truncated, so nothing was read ⇒ resume from the start
    # of the range: this is honesty, not failure — the split progresses next
    # cycle under a real cap (24 calls).
    assert resume == 0
    assert logs == []


async def test_paging_raises_when_single_block_exceeds_limit():
    """A single block above the cap: no split is possible ⇒ an exception, not an infinite loop."""
    rpc = _PagingRPC(limit_above=-1)
    with pytest.raises(evm_rpc.EVMLogLimit):
        await rpc.get_logs_paged(NET, [TOK], 7, 7, max_calls=5, sleep=_noop)


async def test_paging_stops_at_time_budget_not_only_call_count():
    """The time budget is what protects the period: a single call can hang
    for 25 seconds.

    Measured on the second live cycle: 72 calls consumed 118 seconds against
    a 60-second period — so call count says nothing about time. And the exit
    here is with a resume point, not a loss.
    """
    import time

    rpc = _PagingRPC(limit_above=10)

    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 1000, max_calls=50, sleep=_noop,
        deadline=time.monotonic() - 1,          # the budget is already spent
    )

    # Always one call even with the budget spent: without this the token
    # would spin forever with no progress.
    assert calls == 1
    assert complete is False
    assert resume == 0
    assert logs == []


async def test_paging_ignores_a_deadline_that_never_comes():
    """A generous budget ⇒ behavior unchanged (no hidden shortening)."""
    import time

    rpc = _PagingRPC(limit_above=100, per_block={150: [_log(A, B, 5, 150)]})

    logs, _calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 400, max_calls=50, sleep=_noop,
        deadline=time.monotonic() + 300,
    )

    assert (complete, resume, len(logs)) == (True, 400, 1)


# ---------------------------------------------------------------------------
# Batching: several calls in one HTTP request
#
# The cap is a limit per call, not per request, so batching doubles the range
# read for the same number of requests — and it is in the standard itself
# (JSON-RPC 2.0 §6), not a workaround. Its price is that success and failure
# mix in a single reply, which is what these tests examine.
# ---------------------------------------------------------------------------
class _BatchRPC(_PagingRPC):
    """Adds batching to the splitting client: `batches` is what was packed into a single request."""

    def __init__(self, *args, too_large_once=False, gag_oldest_once=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.batches = []
        self._too_large_once = too_large_once
        self._gag_oldest_once = gag_oldest_once

    async def get_logs_multi(self, network_id, addresses, ranges, topics=None):
        self.batches.append(list(ranges))
        if self._too_large_once:
            self._too_large_once = False
            return [
                evm_rpc.EVMBatchLimit("backend response too large") for _ in ranges
            ]
        out = []
        for index, (lo, hi) in enumerate(ranges):
            if index == 0 and self._gag_oldest_once:
                self._gag_oldest_once = False
                out.append(evm_rpc.EVMRateLimit("HTTP 429"))
                continue
            try:
                out.append(
                    await self.get_logs(network_id, addresses, lo, hi, topics)
                )
            except evm_rpc.EVMRPCError as exc:
                out.append(exc)
        return out


def test_monad_batch_size_stays_below_quicknode_subrequest_limit():
    """QuickNode Monad counts every subrequest inside a JSON-RPC batch against 50/sec."""
    assert 1 <= config.EVM_BATCH_SIZE["143"] <= 50


def test_split_range_uses_the_hint_only_when_it_actually_shrinks():
    """A range whose length equals the hint must not be split by the hint,
    or it returns itself forever.

    This is a survival condition, not an optimization: `range(lo, hi+1,
    hint)` over a range of hint length returns a single range identical to
    itself, which gets pushed onto the stack and rejected again — a loop that
    eats the whole call cap with zero progress, and shows up in the log as a
    token that "works" with no rows.
    """
    assert evm_rpc._split_range(0, 400, 100) == [
        (0, 99), (100, 199), (200, 299), (300, 399), (400, 400),
    ]
    assert evm_rpc._split_range(0, 99, 100) == [(0, 49), (50, 99)]
    assert evm_rpc._split_range(0, 400, 0) == [(0, 200), (201, 400)]


async def test_batch_carries_one_sub_call_per_range_and_maps_replies_by_id():
    """The reply is read by `id`, not by position: the standard does not guarantee the reply array's order."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        # Deliberately reversed: positional reading would attribute one range's logs to another.
        return httpx.Response(200, json=[
            {"jsonrpc": "2.0", "id": 2, "result": [_log(A, B, 5, 250)]},
            {"jsonrpc": "2.0", "id": 0, "result": []},
            {"jsonrpc": "2.0", "id": 1, "result": [_log(A, B, 7, 150)]},
        ])

    rpc = _mock_rpc(handler)
    try:
        out = await rpc.get_logs_multi(
            NET, [TOK], [(0, 99), (100, 199), (200, 299)],
        )
    finally:
        await rpc.aclose()

    assert [item["method"] for item in seen["body"]] == ["eth_getLogs"] * 3
    assert [item["id"] for item in seen["body"]] == [0, 1, 2]
    assert [
        (item["params"][0]["fromBlock"], item["params"][0]["toBlock"])
        for item in seen["body"]
    ] == [("0x0", "0x63"), ("0x64", "0xc7"), ("0xc8", "0x12b")]
    assert [len(result) for result in out] == [0, 1, 1]
    assert out[1][0]["blockNumber"] == hex(150)
    assert out[2][0]["blockNumber"] == hex(250)


async def test_batch_marks_only_the_truncated_sub_call_as_a_range_limit(monkeypatch):
    """One truncated call in a batch does not take its siblings down: each
    gets its own result and its own remedy.

    And this is the practical difference between batching and single calls:
    raising on the first failure throws away successful results already paid
    for, and truncation (10,000 logs) is common in one range out of ten.
    """
    monkeypatch.setattr(config, "EVM_LOG_LIMIT", 2)

    def handler(_request):
        return httpx.Response(200, json=[
            {"jsonrpc": "2.0", "id": 0, "result": [_log(A, B, 1, 10)]},
            {"jsonrpc": "2.0", "id": 1,
             "result": [_log(A, B, 1, 110), _log(A, B, 2, 111)]},
            {"jsonrpc": "2.0", "id": 2,
             "error": {"code": -32020, "message": "backend response too large"}},
        ])

    rpc = _mock_rpc(handler)
    try:
        out = await rpc.get_logs_multi(
            NET, [TOK], [(0, 99), (100, 199), (200, 299)],
        )
    finally:
        await rpc.aclose()

    assert isinstance(out[0], list) and len(out[0]) == 1
    assert isinstance(out[1], evm_rpc.EVMLogLimit)      # hit the cap ⇒ split the range
    assert isinstance(out[2], evm_rpc.EVMBatchLimit)    # heavy response ⇒ shrink the count


async def test_batching_covers_the_same_range_in_fewer_requests(monkeypatch):
    """Exactly the same coverage with a third of the requests — and that is the whole yield."""
    monkeypatch.setitem(config.EVM_LOG_RANGE_HINT, NET, 100)
    monkeypatch.setitem(config.EVM_BATCH_SIZE, NET, 4)
    rpc = _BatchRPC(limit_above=100, per_block={150: [_log(A, B, 5, 150)]})

    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 400, max_calls=10, sleep=_noop,
    )

    assert (complete, resume, len(logs)) == (True, 400, 1)
    # Node calls unchanged (six) — and three requests: a rejection, then a batch of four, then a single.
    assert rpc.ranges == [
        (0, 400), (0, 99), (100, 199), (200, 299), (300, 399), (400, 400),
    ]
    assert calls == 3
    assert rpc.batches == [[(0, 99), (100, 199), (200, 299), (300, 399)]]


async def test_a_too_large_response_shrinks_the_batch_without_splitting_ranges(
    monkeypatch,
):
    """`-32020` is a size limit, not a range limit: halve the count and keep
    the ranges as they are.

    Splitting the range here is an expensive mistake: all the ranges are
    already acceptable, so splitting them wastes the available cap and
    doubles the calls for nothing. And the largest acceptable batch is a
    property of the token, not of the network (measured 10, 5, 3, 2, and 1
    for five Base tokens), so the learning lives in the call, not in the
    file.
    """
    monkeypatch.setitem(config.EVM_LOG_RANGE_HINT, NET, 100)
    monkeypatch.setitem(config.EVM_BATCH_SIZE, NET, 4)
    rpc = _BatchRPC(limit_above=100, too_large_once=True)

    _logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 400, max_calls=10, sleep=_noop,
    )

    assert (complete, resume) == (True, 400)
    assert [len(batch) for batch in rpc.batches] == [4, 2, 2]
    assert rpc.batches[0] == [(0, 99), (100, 199), (200, 299), (300, 399)]
    # And the size does not climb back up: climbing back means a fresh rejection every few requests.
    assert calls == 5


async def test_logs_above_an_older_failed_range_are_discarded_and_reread(monkeypatch):
    """The most dangerous thing batching does: the oldest range fails and
    what follows it succeeds in the same reply.

    Keeping the newer range's logs means either reading them again next cycle
    (doubled balances) or advancing the resume point over an unread range (a
    permanent gap). The contract is that every returned log's block is below
    the resume point — even at the cost of an extra request.
    """
    monkeypatch.setitem(config.EVM_LOG_RANGE_HINT, NET, 100)
    monkeypatch.setitem(config.EVM_BATCH_SIZE, NET, 4)
    monkeypatch.setattr(config, "EVM_RATE_LIMIT_BACKOFF_SECONDS", 0)
    rpc = _BatchRPC(
        limit_above=100, gag_oldest_once=True,
        per_block={150: [_log(A, B, 5, 150)], 250: [_log(B, C, 3, 250)]},
    )

    logs, _calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 400, max_calls=20, sleep=_noop,
    )

    assert (complete, resume) == (True, 400)
    # One log per transfer even if read twice, and in ascending order.
    assert [int(log["blockNumber"], 16) for log in logs] == [150, 250]
    # And the proof it really was read twice: the successful range was re-requested after an older one was throttled.
    assert rpc.ranges.count((100, 199)) == 2
    assert rpc.ranges.count((200, 299)) == 2


async def test_first_mint_block_finds_the_creation_block_in_one_call():
    """A two-topic filter ⇒ mints alone, and their lowest block is for all
    practical purposes the creation block.

    Measured on the live chain: three of four Robinhood tokens were minted
    above 67% of the chain's length, so walking from zero reads 27–37 million
    empty blocks. And the call takes 0.17 seconds.
    """
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1,
            "result": [_log(ZERO, A, 5, 900), _log(ZERO, B, 7, 700)],
        })

    rpc = _mock_rpc(handler)
    try:
        block = await rpc.first_mint_block(NET, TOK, 1_000)
    finally:
        await rpc.aclose()

    assert block == 700
    params = seen["body"]["params"][0]
    assert params["topics"] == [evm_rpc.TRANSFER_TOPIC, evm_rpc.ZERO_TOPIC]
    assert (params["fromBlock"], params["toBlock"]) == ("0x0", "0x3e8")


async def test_first_mint_block_returns_none_when_nothing_was_minted():
    rpc = _mock_rpc(lambda _r: httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "result": []},
    ))
    try:
        assert await rpc.first_mint_block(NET, TOK, 1_000) is None
    finally:
        await rpc.aclose()


async def test_a_truncated_mint_scan_answers_unknown_not_a_higher_floor(monkeypatch):
    """A truncated reply must not be read as a floor: a false floor means a
    token rejected wholesale.

    A continuously minting token hits the 10,000 cap, so the lowest block we
    saw is higher than the truth — and a holder who received before that
    floor shows a negative balance. So `None` is the honest answer.
    """
    monkeypatch.setattr(config, "EVM_LOG_LIMIT", 2)
    rpc = _mock_rpc(lambda _r: httpx.Response(200, json={
        "jsonrpc": "2.0", "id": 1,
        "result": [_log(ZERO, A, 5, 900), _log(ZERO, B, 7, 700)],
    }))
    try:
        assert await rpc.first_mint_block(NET, TOK, 1_000) is None
    finally:
        await rpc.aclose()


async def test_a_gagged_mint_scan_raises_instead_of_answering_unknown():
    """"I could not ask" is not "nothing was minted before this block".

    Swallowing a throttle turns one bad second into a walk from genesis for
    every token on every cycle — which is exactly what makes the cap run out
    before the first snapshot.
    """
    rpc = _mock_rpc(lambda _r: httpx.Response(429, text="rate limited"))
    try:
        with pytest.raises(evm_rpc.EVMRateLimit):
            await rpc.first_mint_block(NET, TOK, 1_000)
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Classifying the node's reply: a timeout gets split, a throttle gets waited out
#
# Both cases measured on the first live cycle (2026-08-13): Robinhood answered
# a fill from block zero with `-32000 log query timed out`, then throttled the
# two calls after it with 429.
# ---------------------------------------------------------------------------
class _Resp:
    """A fake HTTP response with the bare minimum `_call` reads: status, text, and json()."""

    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _ScriptedHTTP:
    """A fake httpx client: the reply is computed from the requested range and the call number."""

    def __init__(self, fn):
        self._fn = fn
        self.ranges = []

    async def post(self, url, json=None):
        params = ((json or {}).get("params") or [{}])[0]
        params = params if isinstance(params, dict) else {}
        lo = int(str(params.get("fromBlock", "0x0")), 16)
        hi = int(str(params.get("toBlock", "0x0")), 16)
        self.ranges.append((lo, hi))
        return self._fn(lo, hi, len(self.ranges))


def _fake_rpc(fn):
    """A real client with a fake HTTP client — bypassing `__init__` so no socket is ever opened."""
    rpc = evm_rpc.EVMRPC.__new__(evm_rpc.EVMRPC)
    rpc._urls = {NET: "http://node.invalid"}
    rpc._timeout = 1.0
    rpc._client = _ScriptedHTTP(fn)
    return rpc


def _err(code, message):
    return _Resp(payload={"jsonrpc": "2.0", "error": {"code": code, "message": message}})


def _ok(result):
    return _Resp(payload={"jsonrpc": "2.0", "result": result})


async def test_query_timeout_is_a_range_to_split_not_a_dead_end():
    """"The range is wider than I can manage", phrased differently ⇒ the same
    remedy: split it.

    Without this classification the token's whole fill dies on the first
    timeout — and it actually happened on the first live cycle: 3 of 58
    tokens dropped out of the ledger.
    """
    def fn(lo, hi, n):
        if hi - lo > 50:
            return _err(-32000, "log query timed out")
        return _ok([_log(A, B, 5, lo)])

    rpc = _fake_rpc(fn)
    logs, calls, complete, resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 200, max_calls=20, sleep=_noop,
    )

    assert complete is True
    assert resume == 200
    assert len(logs) == 4                     # four quarters, each ≤50 blocks
    assert calls > 4                          # and the splitting itself cost calls


async def test_rate_limit_waits_and_repeats_the_same_range():
    """A throttle is waited out, not split: half the range doubles the calls, which invites more throttling."""
    def fn(lo, hi, n):
        if n == 1:
            return _Resp(status=429, text='{"error":{"code":429}}')
        return _ok([_log(A, B, 5, lo)])

    rpc = _fake_rpc(fn)
    logs, _calls, complete, _resume = await rpc.get_logs_paged(
        NET, [TOK], 0, 100, max_calls=5, sleep=_noop,
    )

    assert complete is True
    assert rpc._client.ranges == [(0, 100), (0, 100)]      # the same range, not half of it
    assert len(logs) == 1


async def test_rate_limit_inside_a_200_body_is_also_classified():
    """Some nodes throttle with a 200 and an `error` block — classification is by meaning, not by HTTP status."""
    def fn(lo, hi, n):
        if n == 1:
            return _err(429, "Too Many Requests")
        return _ok([])

    rpc = _fake_rpc(fn)
    _logs, _calls, complete, _ = await rpc.get_logs_paged(
        NET, [TOK], 0, 10, max_calls=5, sleep=_noop,
    )

    assert complete is True
    assert rpc._client.ranges == [(0, 10), (0, 10)]


async def test_revert_is_an_answer_but_rate_limit_is_not():
    """`eth_call` swallows the revert ("no such function") but not the
    throttle.

    Swallowing the throttle would write "this contract has no owner" — false
    information stored as if it were measured.
    """
    reverting = _fake_rpc(lambda lo, hi, n: _err(3, "execution reverted"))
    assert await reverting.eth_call(NET, TOK, "0x8da5cb5b") is None

    muted = _fake_rpc(lambda lo, hi, n: _Resp(status=429, text="Too Many Requests"))
    with pytest.raises(evm_rpc.EVMRateLimit):
        await muted.eth_call(NET, TOK, "0x8da5cb5b")


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------
def test_ledger_applies_signed_deltas_and_ranks_by_value(db):
    db.evm_apply_transfers(NET, TOK, {A: (300, 10), B: (100, 10)}, NOW)
    db.evm_apply_transfers(NET, TOK, {A: (-250, 11), C: (250, 11)}, NOW)

    top = db.evm_top_balances(NET, TOK, 10)
    assert top == [(C, 250), (B, 100), (A, 50)]
    assert db.evm_ledger_stats(NET, TOK) == {"holder_count": 3, "supply": 400}


def test_ledger_chunks_holder_lookup_below_sqlite_variable_limit(db):
    original_limit = db._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
    holders = {
        f"0x{i:040x}": (1, 10)
        for i in range(40)
    }
    try:
        assert db.evm_apply_transfers(NET, TOK, holders, NOW) == 40
    finally:
        db._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original_limit)

    assert db.evm_ledger_stats(NET, TOK) == {"holder_count": 40, "supply": 40}


def test_ledger_ranks_beyond_64_bit(db):
    """Ranking is lexicographic over zero-padded text ⇒ identical to numeric above SQLite's limit."""
    small, huge = 2 ** 63 + 1, 2 ** 200
    db.evm_apply_transfers(NET, TOK, {A: (small, 1), B: (huge, 1)}, NOW)
    assert db.evm_top_balances(NET, TOK, 2) == [(B, huge), (A, small)]


def test_ledger_rejects_negative_regular_holder(db):
    """A negative for an ordinary address is evidence of loss/duplication, not a zero that may be stored."""
    with pytest.raises(Exception, match="negative"):
        db.evm_apply_transfers(NET, TOK, {A: (-500, 9)}, NOW)
    assert db.evm_ledger_stats(NET, TOK)["holder_count"] == 0


def test_ledger_allows_negative_burn_source_without_storing_it(db):
    """The zero address sends on mint, so its negative is expected, but it does not become a holder."""
    db.evm_apply_transfers(
        NET, TOK, {ZERO: (-500, 9), A: (500, 9)}, NOW,
        allow_negative=(ZERO,),
    )
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 500)]


def test_ledger_first_seen_block_never_moves(db):
    """"New holder" = first entry, not last movement."""
    db.evm_apply_transfers(NET, TOK, {A: (10, 100)}, NOW)
    db.evm_apply_transfers(NET, TOK, {A: (10, 500)}, NOW)
    row = db._conn.execute(
        "SELECT first_seen_block, updated_block FROM evm_balances "
        "WHERE holder_address=?", (A,),
    ).fetchone()
    assert row["first_seen_block"] == 100
    assert row["updated_block"] == 500
    assert db.evm_new_holders_since(NET, TOK, 50) == 1
    assert db.evm_new_holders_since(NET, TOK, 200) == 0


def test_first_seen_uses_first_receipt_even_when_net_delta_is_zero(db):
    deltas = evm_layer._deltas_by_token([
        _log(ZERO, A, 100, 10), _log(A, B, 100, 11),
    ])
    db.evm_apply_transfers(
        NET, TOK, deltas[TOK], NOW, allow_negative=evm_rpc.BURN_ADDRESSES,
    )
    row = db._conn.execute(
        "SELECT balance_hex, first_seen_block FROM evm_balances "
        "WHERE holder_address=?", (A,),
    ).fetchone()
    assert int(row["balance_hex"], 16) == 0
    assert row["first_seen_block"] == 10


def test_ledger_excludes_burn_addresses_from_ratios(db):
    db.evm_apply_transfers(NET, TOK, {A: (100, 1), ZERO: (900, 1)}, NOW)
    assert db.evm_ledger_stats(NET, TOK)["supply"] == 1000
    stats = db.evm_ledger_stats(NET, TOK, exclude=evm_rpc.BURN_ADDRESSES)
    assert stats == {"holder_count": 1, "supply": 100}


# ---------------------------------------------------------------------------
# The snapshot row
# ---------------------------------------------------------------------------
def test_concentration_row_computes_tiers_from_live_supply():
    top = [(f"0x{i:040x}", 100) for i in range(20)]
    row = evm_layer.build_evm_concentration_row(
        {"supply": 2_000, "holder_count": 40}, top, TOK, NET, NOW, NOW, "sig-1",
    )
    assert row["top1_pct"] == pytest.approx(5.0)
    assert row["top5_pct"] == pytest.approx(25.0)
    assert row["top20_pct"] == pytest.approx(100.0)
    assert row["holder_count"] == 40
    assert row["top_accounts"] == 20
    assert row["raw_json"]["supply_base"] == "2000"


def test_concentration_row_sorts_before_slicing():
    row = evm_layer.build_evm_concentration_row(
        {"supply": 1_000, "holder_count": 4},
        [(A, 100), (B, 700), (C, 200)], TOK, NET, NOW, NOW, None,
    )
    assert row["top1_pct"] == pytest.approx(70.0)


def test_concentration_row_keeps_precision_at_eighteen_decimals():
    huge = 10 ** 27 + 1
    row = evm_layer.build_evm_concentration_row(
        {"supply": huge, "holder_count": 1}, [(A, huge)], TOK, NET, NOW, NOW,
        None, decimals=18,
    )
    assert row["top1_pct"] == pytest.approx(100.0)


def test_concentration_row_none_when_ledger_empty():
    """An empty ledger ≠ a token with no holders: no row of zeros (FR-007)."""
    assert evm_layer.build_evm_concentration_row(
        {"supply": 0, "holder_count": 0}, [], TOK, NET, NOW, NOW, None,
    ) is None


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------
class _CycleRPC:
    """Fake EVM client: fixed logs filtered by range and address as the node does."""

    def __init__(self, head=1_000, logs=(), fail=(), mint=None):
        self.head = head
        self._logs = list(logs)
        self._fail = set(fail)
        self._mint = mint
        self.ranges = []
        self.heads = []
        self.mint_scans = []

    async def first_mint_block(self, network_id, address, head):
        """`None` is the default here: "unknown" ⇒ a walk from genesis.

        And that is what the remaining tests must stay on, because their data
        describes a small chain starting at zero, and some of them place a
        transfer **before** the mint as a simplification. The scan itself is
        tested by passing an explicit `mint=` where it is the subject.
        """
        self.mint_scans.append((str(network_id), address.lower(), int(head)))
        return self._mint

    async def block_number(self, network_id):
        self.heads.append(str(network_id))
        if str(network_id) in self._fail:
            raise evm_rpc.EVMRPCError(f"eth_blockNumber [{network_id}] HTTP 503")
        return self.head

    async def get_logs_paged(
        self, network_id, addresses, from_block, to_block, topics=None,
        max_calls=None, sleep=None, deadline=None,
    ):
        self.ranges.append((str(network_id), tuple(addresses), from_block, to_block))
        want = {a.lower() for a in addresses}
        out = [
            log for log in self._logs
            if log["address"].lower() in want
            and from_block <= int(log["blockNumber"], 16) <= to_block
        ]
        return out, 1, True, int(to_block)


async def test_first_cycle_seeds_cursor_then_backfills_then_snapshots(db):
    """One cycle on an empty database: a cursor, then a full-history fill, then a snapshot."""
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5), _log(A, B, 200, 6)])

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_cursor_init"] == 1
    assert stats["evm_backfilled"] == 1
    assert stats["evm_snapshots"] == 1
    assert stats["evm_errors"] == 0
    # The cursor sits at head minus confirmations, not at the head.
    assert db.evm_cursor(NET)["last_block"] == 1_000 - config.EVM_CONFIRMATIONS
    # The fill starts at block zero: the balance is cumulative, not averaged.
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 500), (B, 200)]
    row = db._conn.execute(
        "SELECT top1_pct, holder_count, top_accounts, network_id "
        "FROM chain_concentration"
    ).fetchone()
    assert row["network_id"] == NET
    assert row["holder_count"] == 2
    assert row["top1_pct"] == pytest.approx(500 / 700 * 100)
    assert decode_raw(
        db._conn.execute("SELECT raw_json FROM chain_concentration").fetchone()["raw_json"]
    )["source"] == "evm_ledger"


async def test_second_cycle_does_not_reapply_backfilled_range(db):
    """Only what is **after** the cursor is applied: a range applied twice doubles every balance."""
    _watch(db)
    old = _log(ZERO, A, 700, 5)
    rpc = _CycleRPC(head=1_000, logs=[old])
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]

    rpc.head = 2_000                                  # new blocks, and the same old log
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_due"] == 0             # the fill is finished and is not redone
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]
    applied = [r for r in rpc.ranges if r[2] > 5]
    assert applied, "the second cycle must apply what is after the cursor"


async def test_new_token_after_cursor_is_backfilled_without_live_double_apply(db):
    """An unfilled token does not enter the live apply; otherwise the recent range would repeat in backfill."""
    db.set_evm_cursor(NET, 100, NOW, "ok")
    _watch(db)
    rpc = _CycleRPC(head=200, logs=[_log(ZERO, A, 700, 150)])

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfilled"] == 1
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]
    # One call is the fill. The live apply does not ask a token whose ledger is not complete yet.
    assert len(rpc.ranges) == 1


async def test_base_backfill_starts_at_contract_creation_block(db, monkeypatch):
    """A new Base token does not sweep the empty blocks that preceded its contract's creation."""
    net = "8453"
    creation = 850
    _watch(db, network=net)
    db.set_evm_cursor(net, 988, NOW, "ok")
    monkeypatch.setattr(config, "EVM_NETWORKS", (net,))
    monkeypatch.setattr(config, "EVM_CREATION_BLOCK_NETWORKS", (net,))

    class _CreationRPC(_CycleRPC):
        async def contract_creation_block(self, network_id, address, head):
            assert (network_id, address, head) == (net, TOK, 988)
            return creation

    rpc = _CreationRPC(
        head=1_000,
        logs=[_log(ZERO, A, 700, creation, token=TOK)],
    )

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfilled"] == 1
    assert rpc.ranges[0][2:] == (creation, 988)
    assert db.evm_top_balances(net, TOK, 10) == [(A, 700)]


async def test_backfill_starts_at_the_first_mint_where_there_is_no_archive(db):
    """Robinhood keeps no historical state, so the alternative to the binary
    search is a call with a mint filter.

    The saving was measured on the live chain 2026-08-19: three of four
    watched tokens were minted above 67% of the chain's length (67.7%, 88.6%,
    and 93.6%) — that is 27–37 **million** blocks carrying not one transfer
    that used to be walked before the first log. And the call answers in 0.17
    seconds.
    """
    mint = 700
    _watch(db)
    db.set_evm_cursor(NET, 988, NOW, "ok")
    rpc = _CycleRPC(head=1_000, mint=mint, logs=[_log(ZERO, A, 700, mint)])

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfilled"] == 1
    # The scan's upper bound is the cursor itself: what is above it belongs to the live apply.
    assert rpc.mint_scans == [(NET, TOK, 988)]
    assert rpc.ranges[0][2:] == (mint, 988)
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]


async def test_unknown_mint_block_walks_from_genesis_instead_of_guessing(db):
    """`None` means "unknown", not "nothing minted before this block" — and
    the difference is a correct ledger.

    A continuously minting token can get the scan's reply truncated at the
    cap, making the lowest block we saw higher than the truth. A floor above
    creation means a holder who received before it shows a negative balance,
    and a token with a negative balance is rejected wholesale. The long walk
    is an acceptable price; the false floor is not.
    """
    _watch(db)
    db.set_evm_cursor(NET, 988, NOW, "ok")
    rpc = _CycleRPC(head=1_000, mint=None, logs=[_log(ZERO, A, 700, 40)])

    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.mint_scans == [(NET, TOK, 988)]
    assert rpc.ranges[0][2:] == (config.EVM_BACKFILL_FROM_BLOCK, 988)
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]


async def test_empty_partial_backfill_rechecks_contract_creation(db, monkeypatch):
    """An old empty state starts from contract creation even if it saved a small earlier point."""
    net = "8453"
    creation = 850
    _watch(db, network=net)
    db.set_evm_cursor(net, 988, NOW, "ok")
    db.set_evm_backfill_state(
        net, TOK, "partial", NOW, from_block=100, to_block=988,
        transfers=0, calls=24,
    )
    monkeypatch.setattr(config, "EVM_NETWORKS", (net,))
    monkeypatch.setattr(config, "EVM_CREATION_BLOCK_NETWORKS", (net,))

    class _CreationRPC(_CycleRPC):
        async def contract_creation_block(self, network_id, address, head):
            return creation

    rpc = _CreationRPC(
        head=1_000,
        logs=[_log(ZERO, A, 700, creation, token=TOK)],
    )

    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.ranges[0][2:] == (creation, 988)
    assert db.evm_top_balances(net, TOK, 10) == [(A, 700)]


async def test_zero_net_ledger_with_applied_transfers_keeps_saved_resume(db, monkeypatch):
    """A zero net balance does not mean the previous range was empty or may be applied twice."""
    net = "8453"
    _watch(db, network=net)
    db.set_evm_cursor(net, 988, NOW, "ok")
    db.set_evm_backfill_state(
        net, TOK, "partial", NOW, from_block=500, to_block=988,
        transfers=2, calls=24,
    )
    # Applied transfers leave rows even at balance zero (`evm_apply_transfers`
    # pins, never deletes): the ledger's frontier — not the balance — is what
    # proves the saved range was consumed. The zero-net row is that proof.
    db._conn.execute(
        "INSERT INTO evm_balances (network_id, token_address, holder_address,"
        " balance_hex, first_seen_block, updated_block, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (net, TOK, B, "0" * 64, 480, 499, NOW),
    )
    db._conn.commit()
    monkeypatch.setattr(config, "EVM_NETWORKS", (net,))
    monkeypatch.setattr(config, "EVM_CREATION_BLOCK_NETWORKS", (net,))

    class _CreationRPC(_CycleRPC):
        async def contract_creation_block(self, *_args):
            raise AssertionError("the start must not be rediscovered after transfers were applied")

    rpc = _CreationRPC(head=1_000)

    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.ranges[0][2] == 500


async def test_apply_uses_only_common_completed_prefix_across_address_batches(
    db, monkeypatch,
):
    """One batch lagging does not allow writing another batch's future and then replaying it next cycle."""
    tok2 = "0xbbbb000000000000000000000000000000000002"
    _watch(db, TOK)
    _watch(db, tok2)
    db.set_evm_backfill_state(NET, TOK, "done", NOW, from_block=101, to_block=100)
    db.set_evm_backfill_state(NET, tok2, "done", NOW, from_block=101, to_block=100)
    db.set_evm_cursor(NET, 100, NOW, "ok")
    monkeypatch.setitem(config.EVM_ADDRESS_BATCH, NET, 1)

    class _Uneven(_CycleRPC):
        def __init__(self):
            super().__init__(head=200)
            self.round = 0

        async def get_logs_paged(
            self, network_id, addresses, from_block, to_block, topics=None,
            max_calls=None, sleep=None, deadline=None,
        ):
            self.ranges.append((str(network_id), tuple(addresses), from_block, to_block))
            token = addresses[0].lower()
            if self.round == 0 and token == tok2:
                return [], 1, False, 130
            logs = [_log(A, B, 100, 150, token=TOK)] if token == TOK else []
            return logs, 1, True, int(to_block)

    rpc = _Uneven()
    db.evm_apply_transfers(NET, TOK, {A: (100, 100)}, NOW)
    first = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert first["evm_lagging"] == 1
    assert db.evm_cursor(NET)["last_block"] == 129
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 100)]

    rpc.round = 1
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert db.evm_top_balances(NET, TOK, 10) == [(B, 100)]


async def test_apply_rolls_back_balances_when_cursor_write_fails(db, monkeypatch):
    """Balances and cursor are one atomic unit; a cursor failure must not leave transfers that will be replayed."""
    _watch(db)
    db.set_evm_backfill_state(NET, TOK, "done", NOW, from_block=101, to_block=100)
    db.set_evm_cursor(NET, 100, NOW, "ok")
    rpc = _CycleRPC(head=200, logs=[_log(ZERO, A, 700, 150)])
    original = db.set_evm_cursor

    failed = False

    def _fail_cursor(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("cursor write failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "set_evm_cursor", _fail_cursor)
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    monkeypatch.setattr(db, "set_evm_cursor", original)

    assert stats["evm_errors"] == 1
    assert db.evm_ledger_stats(NET, TOK)["supply"] == 0
    assert db.evm_cursor(NET)["last_block"] == 100


async def test_apply_aborts_when_repair_changes_ledger_generation(db):
    _watch(db)
    db.set_evm_backfill_state(NET, TOK, "done", NOW, from_block=101, to_block=100)
    db.set_evm_cursor(NET, 100, NOW, "ok")

    class _ResetDuringFetch(_CycleRPC):
        async def get_logs_paged(self, *args, **kwargs):
            db.bump_evm_ledger_generation()
            return await super().get_logs_paged(*args, **kwargs)

    rpc = _ResetDuringFetch(head=200, logs=[_log(ZERO, A, 700, 150)])
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_errors"] == 0
    assert db.evm_ledger_stats(NET, TOK)["supply"] == 0


async def test_apply_does_not_advance_past_a_backfill_completed_during_head_fetch(db):
    _watch(db)
    db.set_evm_cursor(NET, 100, NOW, "ok")
    db_path = db._conn.execute("PRAGMA database_list").fetchone()["file"]
    other = RecorderDB(db_path, SCHEMA)

    class _BackfillCompletesDuringHead(_CycleRPC):
        async def block_number(self, network_id):
            other.set_evm_backfill_state(
                NET, TOK, "done", NOW, from_block=101, to_block=100,
            )
            return await super().block_number(network_id)

    try:
        stats = await evm_layer.run_evm_cycle(
            _BackfillCompletesDuringHead(head=200), db, NOW, sleep=_noop,
        )
    finally:
        other.close()

    assert stats["evm_errors"] == 0
    assert db.evm_cursor(NET)["last_block"] == 100


async def test_new_transfer_after_cursor_is_applied(db):
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5)])
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    rpc.head = 2_000
    rpc._logs.append(_log(A, B, 300, 1_500))
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert db.evm_top_balances(NET, TOK, 10) == [(A, 400), (B, 300)]


async def test_partial_backfill_blocks_snapshot(db):
    """A snapshot of a half-filled token is a false number, not a partial one."""
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5)])

    async def _partial(network_id, addresses, from_block, to_block, topics=None,
                       max_calls=None, sleep=None, deadline=None):
        # Only half the range was read ⇒ resume from its midpoint.
        mid = (from_block + to_block) // 2
        return [], 1, False, mid

    rpc.get_logs_paged = _partial
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_partial"] == 1
    assert stats["evm_snapshots"] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration"
    ).fetchone()[0] == 0
    state = db.evm_backfill_state(NET, TOK)
    assert state["status"] == "partial"
    assert state["from_block"] > 0                    # resumed, not restarted from zero


def test_snapshot_helper_rejects_partial_backfill(db):
    """The guard inside the writer itself, not only in the coordinator, prevents an incomplete-ledger snapshot."""
    _watch(db)
    db.set_evm_backfill_state(
        NET, TOK, "partial", NOW, from_block=100, to_block=988,
        transfers=1, calls=1,
    )
    watch = db.evm_watched([NET])[0]
    stats = {"evm_snapshots": 0, "evm_snap_empty": 0}

    with pytest.raises(ValueError, match="incomplete"):
        evm_layer._snapshot_token(db, watch, NOW, stats)

    assert db._conn.execute(
        "SELECT COUNT(*) FROM chain_concentration"
    ).fetchone()[0] == 0


async def test_completed_old_backfill_catches_up_before_done(db):
    """A token exempt from the live apply catches up to the cycle's starting cursor before the snapshot."""
    _watch(db)
    db.set_evm_cursor(NET, 200, NOW, "ok")
    db.set_evm_backfill_state(
        NET, TOK, "partial", NOW, from_block=50, to_block=100,
    )
    rpc = _CycleRPC(
        head=300,
        logs=[_log(ZERO, A, 700, 75), _log(A, B, 200, 250)],
    )

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfilled"] == 1
    assert db.evm_backfill_state(NET, TOK)["status"] == "done"
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 500), (B, 200)]


async def test_backfill_commits_when_network_cursor_moves_concurrently(db):
    """Another completed token's cursor advancing does not cancel the partial token's ledger."""
    _watch(db)
    db.set_evm_cursor(NET, 200, NOW, "ok")
    rpc = _CycleRPC(head=300, logs=[_log(ZERO, A, 700, 250)])

    async def _logs(network_id, addresses, from_block, to_block, **kwargs):
        db.set_evm_cursor(NET, 300, NOW, "ok")
        return [_log(ZERO, A, 700, 250)], 1, True, to_block

    rpc.get_logs_paged = _logs
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_partial"] == 1
    state = db.evm_backfill_state(NET, TOK)
    assert (state["status"], state["from_block"], state["to_block"]) == (
        "partial", 289, 300,
    )
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]


async def test_backfill_assist_supplies_its_own_timestamp_when_omitted(db):
    stats = await evm_layer.run_evm_backfill_assist(
        _CycleRPC(), db, networks=[], sleep=_noop,
    )

    assert stats["evm_backfill_due"] == 0


async def test_backfill_done_aborts_if_cursor_moves_after_catchup_check(db, monkeypatch):
    """Cursor movement inside the pinning window must not leave a gap between backfill and the live apply."""
    _watch(db)
    db.set_evm_cursor(NET, 200, NOW, "ok")
    rpc = _CycleRPC(head=300, logs=[_log(ZERO, A, 700, 150)])
    original = db.assert_evm_backfill_state
    moved = False

    def _move_then_assert(*args, **kwargs):
        nonlocal moved
        original(*args, **kwargs)
        if not moved:
            moved = True
            db.set_evm_cursor(NET, 300, NOW, "ok")

    monkeypatch.setattr(db, "assert_evm_backfill_state", _move_then_assert)
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfilled"] == 0
    assert db.evm_backfill_state(NET, TOK) is None
    assert db.evm_ledger_stats(NET, TOK)["supply"] == 0


async def test_incomplete_apply_pulls_cursor_back(db):
    """An incomplete batch ⇒ the cursor stops before the gap; advancing forward loses logs forever."""
    _watch(db)
    rpc = _CycleRPC(head=1_000)
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    seeded = db.evm_cursor(NET)["last_block"]

    async def _incomplete(network_id, addresses, from_block, to_block, topics=None,
                          max_calls=None, sleep=None, deadline=None):
        return [], 1, False, from_block + 10

    rpc.get_logs_paged = _incomplete
    rpc.head = 5_000
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_lagging"] == 1
    # `resume − 1`: the first unread block is (cursor+1)+10, so the cursor stops just before it.
    assert db.evm_cursor(NET)["last_block"] == seeded + 10


async def test_network_error_keeps_valid_cursor_and_records_meta(db):
    _watch(db)
    rpc = _CycleRPC(head=1_000)
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)   # builds the cursor

    rpc._fail = {NET}
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_errors"] == 1
    cursor = db.evm_cursor(NET)
    # The cursor describes the last block actually applied; a head-read
    # failure does not undo that progress, and writing `error` here could
    # downgrade the state of a concurrent worker that succeeded after us.
    assert cursor["last_status"] == "ok"
    assert cursor["last_error"] is None
    assert "503" in (db.get_meta("last_error_evm") or "")


async def test_empty_networks_tuple_touches_nothing(db, monkeypatch):
    """An empty list means "nothing", not "every network"."""
    _watch(db)
    monkeypatch.setattr(config, "EVM_NETWORKS", ())
    rpc = _CycleRPC(head=1_000)

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.heads == []
    assert stats["evm_networks"] == 0


async def test_solana_watch_is_never_touched(db):
    """Networks are separate: a Solana token enters no EVM ledger and is never asked about."""
    _watch(db, "SoLmint111", network=config.SOLANA_NETWORK_ID)
    rpc = _CycleRPC(head=1_000)

    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert all(TOK not in addrs for _, addrs, _, _ in rpc.ranges)
    assert db._conn.execute("SELECT COUNT(*) FROM evm_balances").fetchone()[0] == 0


async def test_backfill_limit_error_is_recorded_and_not_retried_every_cycle(db):
    """A single block above the cap: it is recorded `error` and not retried every minute."""
    _watch(db)
    rpc = _CycleRPC(head=1_000)

    async def _boom(network_id, addresses, from_block, to_block, topics=None,
                    max_calls=None, sleep=None, deadline=None):
        raise evm_rpc.EVMLogLimit("eth_getLogs: 10000 logs ⇒ limit hit")

    rpc.get_logs_paged = _boom
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_errors"] == 1
    state = db.evm_backfill_state(NET, TOK)
    assert state["status"] == "error"
    assert "EVMLogLimit" in state["last_error"]

    # And the next cycle does not retry it even if the node recovered: the break is permanent, not transient.
    del rpc.get_logs_paged
    again = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert again["evm_backfill_due"] == 0


async def test_transient_backfill_failure_is_requeued_not_buried(db):
    """A transient fault ⇒ `retry`, and it comes back next cycle.

    Measured on the first live cycle: a query timeout and two 429s pushed 3
    of 58 tokens out of the ledger **forever** when every failure was written
    `error`. The difference between the two cases is the difference between a
    bad second and an unfixable break.
    """
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5)])

    async def _stumble(network_id, addresses, from_block, to_block, topics=None,
                       max_calls=None, sleep=None, deadline=None):
        raise evm_rpc.EVMRPCError("eth_getLogs [4663] HTTP 503")

    rpc.get_logs_paged = _stumble
    first = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert first["evm_backfill_retry"] == 1
    assert first["evm_backfill_errors"] == 0        # transient ⇒ not in the permanent counter
    assert first["evm_snapshots"] == 0              # no snapshot before a complete ledger
    state = db.evm_backfill_state(NET, TOK)
    assert state["status"] == "retry"
    assert "503" in (state["last_error"] or "")

    del rpc.get_logs_paged                          # the node came back to its senses
    second = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert second["evm_backfill_due"] == 1          # back in the queue
    assert second["evm_backfilled"] == 1
    assert db.evm_backfill_state(NET, TOK)["status"] == "done"
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]


async def test_retry_goes_to_the_tail_of_the_queue(db, monkeypatch):
    """Ordering is by attempt time: whoever was just tried goes last.

    Without this, one stumbling token occupies the whole cycle cap every
    minute and the newcomers are never filled.
    """
    monkeypatch.setattr(config, "EVM_BACKFILL_TOKENS_PER_CYCLE", 1)
    _watch(db)
    rpc = _CycleRPC(head=1_000)

    async def _stumble(network_id, addresses, from_block, to_block, topics=None,
                       max_calls=None, sleep=None, deadline=None):
        raise evm_rpc.EVMRPCError("eth_getLogs [4663] HTTP 503")

    rpc.get_logs_paged = _stumble
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert db.evm_backfill_state(NET, TOK)["status"] == "retry"

    fresh = "0xbbbb000000000000000000000000000000000002"
    _watch(db, fresh)
    del rpc.get_logs_paged
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    # The new one (no attempt time) got ahead of the stumbling one, and the cap is one token.
    assert db.evm_backfill_state(NET, fresh)["status"] == "done"
    assert db.evm_backfill_state(NET, TOK)["status"] == "retry"


async def test_retry_resumes_from_its_saved_block_and_never_doubles(db):
    """`retry` resumes from its point: starting from zero doubles every
    balance already applied.

    The resume point stays saved on a transient failure (`COALESCE`), so
    redoing the whole range is not a slowdown but **false numbers**:
    `evm_apply_transfers` accumulates, it does not replace.
    """
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5)])

    async def _partial(network_id, addresses, from_block, to_block, topics=None,
                       max_calls=None, sleep=None, deadline=None):
        return [_log(ZERO, A, 700, 5)], 1, False, 400

    rpc.get_logs_paged = _partial
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]

    # Then a transient stumble: status `retry`, point 400 saved.
    async def _stumble(network_id, addresses, from_block, to_block, topics=None,
                       max_calls=None, sleep=None, deadline=None):
        raise evm_rpc.EVMRPCError("eth_getLogs [4663] HTTP 429")

    rpc.get_logs_paged = _stumble
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)
    state = db.evm_backfill_state(NET, TOK)
    assert (state["status"], state["from_block"]) == ("retry", 400)

    seen = []

    async def _record(network_id, addresses, from_block, to_block, topics=None,
                      max_calls=None, sleep=None, deadline=None):
        seen.append((from_block, to_block))
        return [], 1, True, to_block

    rpc.get_logs_paged = _record
    await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert seen and seen[-1][0] == 400            # not from zero
    assert db.evm_top_balances(NET, TOK, 10) == [(A, 700)]   # and no doubling


async def test_backfill_rolls_back_balances_when_state_write_fails(db, monkeypatch):
    """The fill state and its balance are one transaction; no checkpoint means no committed balance."""
    _watch(db)
    rpc = _CycleRPC(head=1_000, logs=[_log(ZERO, A, 700, 5)])
    original = db.set_evm_backfill_state
    failed = False

    def _fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("backfill state write failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "set_evm_backfill_state", _fail_once)
    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_retry"] == 1
    assert db.evm_ledger_stats(NET, TOK)["supply"] == 0
    assert db.evm_backfill_state(NET, TOK)["status"] == "retry"


async def test_backfill_stops_starting_tokens_when_the_budget_is_spent(db, monkeypatch):
    """The time budget protects the period: the first token is filled and
    the rest wait for the next cycle.

    The fast layer runs on Solana in the same process, and a 118-second
    cycle (measured) breaks its five-minute rhythm. And the delay here loses
    nothing: the fill resumes from its point.
    """
    _watch(db)
    _watch(db, "0xbbbb000000000000000000000000000000000003")
    rpc = _CycleRPC(head=1_000)
    # A budget already spent ⇒ the first proceeds (one call guaranteed) and the second is deferred.
    monkeypatch.setattr(config, "EVM_BACKFILL_BUDGET_SECONDS", -1.0)

    stats = await evm_layer.run_evm_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["evm_backfill_due"] == 2
    assert stats["evm_backfilled"] == 1
    assert stats["evm_backfill_skipped"] == 1
    assert stats["evm_backfill_errors"] == 0
    assert stats["evm_backfill_retry"] == 0
    # The deferred one has no state row at all ⇒ it heads the next cycle's queue.
    assert db._conn.execute(
        "SELECT COUNT(*) FROM evm_backfill_state"
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# The stale resume point: the ledger frontier is the honest floor
# ---------------------------------------------------------------------------
def test_ledger_frontier_reads_the_applied_high_water(db):
    """`None` for a token with no rows; the highest applied block otherwise."""
    assert db.evm_ledger_frontier(NET, TOK) is None
    for holder, block in ((B, 350), (C, 120)):
        db._conn.execute(
            "INSERT INTO evm_balances (network_id, token_address, holder_address,"
            " balance_hex, first_seen_block, updated_block, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (NET, TOK, holder, f"{10:x}".rjust(64, "0"), block, block, NOW),
        )
    db._conn.commit()
    assert db.evm_ledger_frontier(NET, TOK) == 350


async def test_a_stale_resume_point_is_clamped_to_the_ledger_frontier(db, monkeypatch):
    """Measured live 2026-09-04 on 4663: a retry row saved at from_block
    53,705,748 while the ledger had already been applied through 54,473,722 —
    the re-walk spent balances that were already spent (`negative EVM
    balance`), and the chronic retry held the network's admission gate in
    `active_retry` for a week (the Aug-28 wall of 4663 retries is this exact
    shape). The walk must start at the frontier, not at the stale save."""
    monkeypatch.setattr(config, "EVM_NETWORKS", (NET,))
    _watch(db)
    db.set_evm_cursor(NET, 400, NOW, "ok")
    # The ledger is applied through block 350 (live apply kept it current)…
    db._conn.execute(
        "INSERT INTO evm_balances (network_id, token_address, holder_address,"
        " balance_hex, first_seen_block, updated_block, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (NET, TOK, B, f"{10:x}".rjust(64, "0"), 100, 350, NOW),
    )
    db._conn.commit()
    # …but the backfill row still says "resume at 300" — the stale save.
    db.set_evm_backfill_state(NET, TOK, "partial", NOW, from_block=300, to_block=310)

    asked = []

    class _Hyper:
        def covers(self, network_id):
            return str(network_id) == NET

        async def first_mint_block(self, *_a):
            return None

        async def get_logs_paged(self, network_id, addresses, from_block,
                                 to_block, topics=None, max_calls=None,
                                 sleep=None, deadline=None):
            asked.append(int(from_block))
            return ([], 1, True, int(to_block))

    stats = await evm_layer.run_evm_cycle(
        _CycleRPC(head=400, logs=[]), db, NOW, sleep=_noop, hyper=_Hyper(),
    )

    # The walk began at the ledger's frontier — never at the stale save —
    # and the empty remaining range completed the token instead of retrying.
    assert asked == [351]
    assert stats["evm_backfilled"] == 1
    assert db.evm_backfill_state(NET, TOK)["status"] == "done"


async def test_an_empty_ledger_discards_the_resume_point_and_rewalks_from_the_mint(
    db, monkeypatch,
):
    """Measured live 2026-09-04 on 4663 (tokens 0xa5be0eeb…, 0xb0fea401…): a
    retry row over an **empty** ledger — the resume point outlived a ledger
    rebuild that wiped `evm_balances` — started the walk after holders were
    credited, and every attempt died on `negative EVM balance`. A saved point
    with nothing applied behind it is not a resume: the anchor is re-derived
    and the walk restarts at the mint."""
    monkeypatch.setattr(config, "EVM_NETWORKS", (NET,))
    _watch(db)
    db.set_evm_cursor(NET, 400, NOW, "ok")
    # No `evm_balances` rows at all, and the retry row still points at block
    # 300 — past the true mint at 250, the shape the old walk froze at.
    db.set_evm_backfill_state(NET, TOK, "retry", NOW, from_block=300, to_block=310)

    asked = []
    mint_queries = []

    class _Hyper:
        def covers(self, network_id):
            return str(network_id) == NET

        async def first_mint_block(self, network_id, address, head):
            mint_queries.append((str(network_id), int(head)))
            return 250

        async def get_logs_paged(self, network_id, addresses, from_block,
                                 to_block, topics=None, max_calls=None,
                                 sleep=None, deadline=None):
            asked.append(int(from_block))
            return ([], 1, True, int(to_block))

    stats = await evm_layer.run_evm_cycle(
        _CycleRPC(head=400, logs=[]), db, NOW, sleep=_noop, hyper=_Hyper(),
    )

    # The anchor was re-derived, the walk restarted at the mint — never at
    # the stale save — and the empty range completed the token. (The mint
    # query's head is the cursor the apply step settled on, finality-lagged
    # below the RPC head — its exact value is not this test's subject.)
    assert [net for net, _head in mint_queries] == [NET]
    assert asked == [250]
    assert stats["evm_backfilled"] == 1
    assert db.evm_backfill_state(NET, TOK)["status"] == "done"
