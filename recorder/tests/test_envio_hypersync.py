"""Tests for the Envio HyperSync adapter: pagination, redaction, fallback.

No network call in any test: responses are handmade HyperSync pages, and the
key pool is a fixed list. What is verified is what silence corrupts — the
GoldRush trap (#29): a dropped page is a false balance ledger that looks
sound. So `next_block` must be honored on every page, and the "no more pages"
answer must be told apart from "one page and done".
"""
import json
import os

import envio_hypersync
import httpx
import pytest
from db import RecorderDB
from tests.test_evm_layer import _CycleRPC, _log, _watch

NOW = "2026-08-13T12:00:00+00:00"

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)

KEY = "test-key-123456"
BASE = "8453"
TOKEN = "0xaaaa000000000000000000000000000000000001"
A = "0x1111111111111111111111111111111111111111"
B = "0x2222222222222222222222222222222222222222"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _page(logs, next_block=None):
    """A HyperSync response page in the **measured live shape** (2026-09-04):
    `data` is a list of block groups each carrying `logs`; an empty range
    answers `data: []`. `next_block` absent ⇒ range complete."""
    data = [{"logs": logs}] if logs else []
    body = {"data": data}
    if next_block is not None:
        body["next_block"] = next_block
    return httpx.Response(200, json=body)


def _entry(frm, to, value, block):
    """A HyperSync log row: decimal block numbers, unprefixed hex topics/data."""
    return {
        "block_number": block,
        "log_index": 0,
        "transaction_index": 0,
        "address": TOKEN,
        "topic0": envio_hypersync.TRANSFER_TOPIC,
        "topic1": "0x" + "0" * 24 + frm[2:],
        "topic2": "0x" + "0" * 24 + to[2:],
        "data": hex(value),
    }


def _client(handler, keys=(KEY,)):
    rpc = envio_hypersync.EnvioHyperSync(
        urls={BASE: "https://base.test/query"}, keys=list(keys),
    )
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return rpc


async def _noop(_seconds):
    pass


# ---------------------------------------------------------------------------
# Routing: availability is a fact about the config and the pool, not a hope
# ---------------------------------------------------------------------------
async def test_covers_requires_successful_network_probe_and_key():
    rpc = envio_hypersync.EnvioHyperSync(urls={BASE: "u"}, keys=[KEY])
    assert rpc.covers(BASE) is False               # config alone is not evidence
    assert rpc.covers("143") is False             # no route for this network
    keyless = envio_hypersync.EnvioHyperSync(urls={BASE: "u"}, keys=[])
    assert keyless.covers(BASE) is False           # a route without a key is not a route


async def test_the_default_map_covers_all_measured_networks_after_probe():
    """All configured networks start untrusted and become covered only after a query."""
    rpc = envio_hypersync.EnvioHyperSync(keys=[KEY])
    try:
        for network in ("143", "56", "8453", "4663"):
            assert rpc.covers(network) is False
        assert rpc.covers("999999") is False
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# The GoldRush trap: pagination must be honored page by page
# ---------------------------------------------------------------------------
async def test_the_measured_live_response_shape_parses():
    """The exact body shape the service returned to a live query on
    2026-09-04: `data` is a **list of block groups**, not a dict. The first
    deployment read it as `data.logs` and every page failed with
    "unrecognized response shape" — this test exists so that shape can never
    silently drift again."""
    first = httpx.Response(200, json={
        "archive_height": 50_877_944,
        "data": [{"logs": [_entry(A, B, 10, 5), _entry(A, B, 11, 7)]}],
        "next_block": 100,
        "rollback_guard": None,
        "total_execution_time": 12,
    })
    second = _page([], next_block=None)

    def handler(request):
        return first if json.loads(request.content)["from_block"] == 0 else second

    rpc = _client(handler)
    try:
        logs, requests, complete, _resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 250, sleep=_noop,
        )
    finally:
        await rpc.aclose()
    assert (complete, requests, len(logs)) == (True, 2, 2)


async def test_walk_follows_next_block_through_every_page():
    pages = {
        0: _page([_entry(A, B, 10, 5), _entry(A, B, 11, 7)], next_block=100),
        100: _page([_entry(A, B, 12, 140)], next_block=200),
        200: _page([], next_block=None),           # absent next_block ⇒ complete
    }
    asked = []

    def handler(request):
        body = json.loads(request.content)
        asked.append(body["from_block"])
        return pages[body["from_block"]]

    rpc = _client(handler)
    try:
        logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 250, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert asked == [0, 100, 200]                  # every page was asked, in order
    assert complete is True
    assert resume == 250
    assert requests == 3
    assert len(logs) == 3
    # And the translation to JSON-RPC shape happened: decode_transfer reads these.
    import evm_rpc

    assert evm_rpc.decode_transfer(logs[0]) == {
        "token_address": TOKEN, "from": A, "to": B, "value": 10, "block": 5,
    }


async def test_max_calls_exits_short_with_the_honest_resume_point():
    def handler(request):
        body = json.loads(request.content)
        return _page([_entry(A, B, 1, body["from_block"])], next_block=body["from_block"] + 100)

    rpc = _client(handler)
    try:
        _logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 10_000, max_calls=2, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert complete is False
    assert requests == 2
    assert resume == 200                            # exactly where the third page would start


# ---------------------------------------------------------------------------
# The second key: a quota answer rides the other account, it does not wait
# ---------------------------------------------------------------------------
async def test_a_429_rides_the_second_key_and_succeeds():
    """A 429 is a quota answer, not a broken service: with two keys on two
    accounts (added 2026-09-05 for the Robinhood drain) the cooled key rotates
    out and the same request is answered by the other account — the pool's
    whole reason for existing. The walk must not even notice."""
    second = "test-key-654321"
    asked = []

    def handler(request):
        auth = request.headers["authorization"]
        asked.append(auth)
        if auth.endswith(KEY):
            return httpx.Response(429, text="rate limit")
        return _page([_entry(A, B, 10, 5)], next_block=None)

    rpc = _client(handler, keys=(KEY, second))
    try:
        logs, requests, complete, _resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 100, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert [a.endswith(KEY) for a in asked] == [True, False]   # key 1, then key 2
    assert (complete, requests, len(logs)) == (True, 1, 1)     # one logical page, retried
    assert rpc.key_stats()["keys"] == 2
    assert rpc.covers(BASE) is True


async def test_failed_network_loses_only_its_coverage():
    second = "test-key-654321"
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["from_block"])
        if len(calls) == 1:
            return _page([], next_block=None)
        return httpx.Response(503, text="temporarily unavailable")

    rpc = envio_hypersync.EnvioHyperSync(
        urls={BASE: "https://base.test/query", "4663": "https://rh.test/query"},
        keys=[KEY, second],
    )
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await rpc.get_logs_paged(BASE, [TOKEN], 0, 10, sleep=_noop)
        assert rpc.covers(BASE) is True
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRateLimit):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 10, sleep=_noop)
        assert rpc.covers(BASE) is False
        assert rpc.covers("4663") is False
    finally:
        await rpc.aclose()


async def test_a_429_on_every_key_raises_instead_of_rotating_forever():
    """Each key is tried at most once per request: when both accounts have said
    429, the raise is the honest answer and the caller's backoff does the
    waiting — never an unbounded rotation over cooled keys."""
    second = "test-key-654321"
    asked = []

    def handler(request):
        asked.append(request.headers["authorization"])
        return httpx.Response(429, text="rate limit")

    rpc = _client(handler, keys=(KEY, second))
    try:
        with pytest.raises(Exception) as caught:
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()

    assert type(caught.value).__name__ == "EVMRateLimit"
    assert len(asked) == 2                                     # each key exactly once


async def test_a_401_on_every_key_raises_instead_of_rotating_forever():
    """401/403 rotation is bounded exactly like the 429 branch: each key at
    most once per request. Two keys that both say 401 must raise
    `EnvioUnavailable` after exactly two calls — the old unbounded recursion
    spun forever (one key, two keys, any count)."""
    second = "test-key-654321"
    asked = []

    def handler(request):
        asked.append(request.headers["authorization"])
        return httpx.Response(401, text="unauthorized")

    rpc = _client(handler, keys=(KEY, second))
    try:
        with pytest.raises(envio_hypersync.EnvioUnavailable):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()

    assert len(asked) == 2                             # each key exactly once, then the raise


async def test_a_non_advancing_next_block_is_a_loud_error():
    """The GoldRush failure mode is silence; spinning forever is its cousin — both are refused."""
    def handler(_request):
        return _page([_entry(A, B, 1, 1)], next_block=5)

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(
                BASE, [TOKEN], 5, 100, max_calls=3, sleep=_noop,
            )
    finally:
        await rpc.aclose()


async def test_the_exhausted_tail_pins_next_block_at_from_block():
    """Measured live 2026-09-04 (token 0xc52aedec…): the walk's last page lands
    on the final single-block range and the service answers an empty page with
    `next_block` pinned at `from_block` instead of omitting it. That is
    "complete", not a spin — but only when the page is empty and the range is
    exhausted; the row-bearing variant below stays a loud error.

    Re-verified 2026-09-07 for the exclusive wire range: the request now asks
    [b, b+1) on the wire for the caller's [b, b], so the pinned-cursor answer
    still describes "the single block was queried and held nothing" — the
    condition `lo >= hi and not rows` fires only on that exact final-page
    shape, and nothing about the +1 changes which page the walk ends on. The
    pre-+1 live capture pinned next_block at from_block on the **last** page
    of a walk; an early page pinning the cursor is still a loud error."""
    def handler(_request):
        return _page([], next_block=5_018_120)

    rpc = _client(handler)
    try:
        logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 5_018_120, 5_018_120, sleep=_noop,
        )
    finally:
        await rpc.aclose()
    assert (complete, requests, resume, logs) == (True, 1, 5_018_120, [])

    def handler_with_rows(_request):
        return _page([_entry(A, B, 1, 5_018_120)], next_block=5_018_120)

    rpc = _client(handler_with_rows)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(
                BASE, [TOKEN], 5_018_120, 5_018_120, sleep=_noop,
            )
    finally:
        await rpc.aclose()

    # And a pinned cursor on a **mid-range** page (range still remaining) is
    # a loud error, not completion — the tail shape is final-page-only.
    def handler_mid_range(_request):
        return _page([], next_block=50)

    rpc = _client(handler_mid_range)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(
                BASE, [TOKEN], 50, 100, sleep=_noop,
            )
    finally:
        await rpc.aclose()


async def test_single_address_is_enforced_not_assumed():
    """The live apply's multi-address filter must never land on the HyperSync path."""
    rpc = envio_hypersync.EnvioHyperSync(urls={BASE: "u"}, keys=[KEY])
    try:
        with pytest.raises(envio_hypersync.EnvioUnavailable):
            await rpc.get_logs_paged(BASE, [TOKEN, B], 0, 10, sleep=_noop)
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# The exclusive wire boundary: the last block must actually be read
# ---------------------------------------------------------------------------
async def test_the_final_block_is_on_the_wire_and_its_logs_return():
    """Measured live 2026-09-07 (net 4663): HyperSync's `to_block` is
    **exclusive** — `[b, b]` answers 0 events, `[b, b+1]` answers the events
    at block b. Sending the caller's inclusive `hi` as-is silently skipped the
    final block of every backfill range. The wire body must carry `hi + 1`,
    and the log at `hi` itself must come back."""
    seen_body = {}

    def handler(request):
        body = json.loads(request.content)
        seen_body.update(body)
        return _page([_entry(A, B, 10, 250)], next_block=None)

    rpc = _client(handler)
    try:
        logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 250, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert seen_body["to_block"] == 251                 # hi + 1, exclusive wire
    assert seen_body["from_block"] == 0
    assert (complete, requests, resume) == (True, 1, 250)  # caller semantics unchanged
    assert len(logs) == 1 and int(logs[0]["blockNumber"], 16) == 250


async def test_a_one_block_range_reads_that_block():
    """`[b, b]` is a legal inclusive request: one block, queried once, done."""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _page([_entry(A, B, 10, 5_018_120)], next_block=None)

    rpc = _client(handler)
    try:
        logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 5_018_120, 5_018_120, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert bodies[0]["from_block"] == 5_018_120
    assert bodies[0]["to_block"] == 5_018_121
    assert (complete, requests, resume, len(logs)) == (True, 1, 5_018_120, 1)


async def test_adjacent_ranges_neither_overlap_nor_gap():
    """`[0, 100]` then `[101, 200]` (inclusive caller semantics) must not
    double-read block 100 or skip block 101 — the pagination seam the old
    exclusive-wire bug sat on."""
    def make_handler(pages):
        def handler(request):
            body = json.loads(request.content)
            return pages[(body["from_block"], body["to_block"])]
        return handler

    # First walk [0,100]: page returns the log at block 100 itself and done.
    first = make_handler({(0, 101): _page([_entry(A, B, 10, 100)], next_block=None)})
    # Second walk [101,200]: page returns the log at block 101 and done.
    second = make_handler({(101, 201): _page([_entry(A, B, 11, 101)], next_block=None)})

    for handler, from_b, to_b, expected_block in (
        (first, 0, 100, 100), (second, 101, 200, 101),
    ):
        rpc = _client(handler)
        try:
            logs, requests, complete, _resume = await rpc.get_logs_paged(
                BASE, [TOKEN], from_b, to_b, sleep=_noop,
            )
        finally:
            await rpc.aclose()
        assert complete is True
        assert requests == 1
        assert len(logs) == 1
        assert int(logs[0]["blockNumber"], 16) == expected_block


# ---------------------------------------------------------------------------
# Key handling: rejected key ⇒ unavailable (fallback), never a leaked value
# ---------------------------------------------------------------------------
async def test_rejected_single_key_is_unavailability_not_error():
    rpc = _client(lambda _r: httpx.Response(401, json={"error": "unauthorized"}))
    try:
        with pytest.raises(envio_hypersync.EnvioUnavailable):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 10, sleep=_noop)
    finally:
        await rpc.aclose()


async def test_rejected_key_rotates_to_the_second_key():
    calls = []

    def handler(request):
        calls.append(request.headers.get("authorization"))
        if len(calls) == 1:
            return httpx.Response(401, json={"error": "unauthorized"})
        return _page([], next_block=None)

    rpc = _client(handler, keys=("first-key-123456", "second-key-123456"))
    try:
        _logs, requests, complete, _resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 10, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert complete is True
    assert requests == 1                           # the successful page counts; 401 rotated without counting
    assert calls[0].endswith("first-key-123456")
    assert calls[1].endswith("second-key-123456")


async def test_error_messages_never_contain_the_key():
    def handler(_request):
        return httpx.Response(400, text=f"bad request from {KEY} account")

    rpc = _client(handler)
    try:
        with pytest.raises(Exception) as excinfo:
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 10, sleep=_noop)
    finally:
        await rpc.aclose()
    assert KEY not in str(excinfo.value)


async def test_rate_limit_shape_is_raised_for_the_callers_backoff():
    def handler(_request):
        return httpx.Response(429, text="slow down")

    rpc = _client(handler)
    try:
        with pytest.raises(Exception) as excinfo:
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 10, sleep=_noop)
    finally:
        await rpc.aclose()
    import evm_rpc

    assert isinstance(excinfo.value, evm_rpc.EVMRateLimit)


# ---------------------------------------------------------------------------
# The mint scan
# ---------------------------------------------------------------------------
async def test_first_mint_block_is_the_lowest_mint_page_block():
    def handler(request):
        body = json.loads(request.content)
        assert body["logs"][0]["topics"] == [
            [envio_hypersync.TRANSFER_TOPIC], ["0x" + "0" * 64],
        ]
        return _page([{"block_number": 5_006_972}], next_block=None)

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 10_000_000) == 5_006_972
    finally:
        await rpc.aclose()


async def test_first_mint_block_follows_pagination_until_the_mint():
    """A first page without a mint is not proof none exists later (plan 4.5):
    the scan follows `next_block` and answers with the later page's mint —
    where the old single-page read returned None and silently started the
    walk at a wrong (too-recent) block."""
    pages = {
        0: _page([], next_block=400_000),       # empty early history, more to read
        400_000: _page([{"block_number": 512_345}], next_block=None),
    }

    def handler(request):
        return pages[json.loads(request.content)["from_block"]]

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 600_000) == 512_345
    finally:
        await rpc.aclose()


async def test_first_mint_block_none_only_when_the_range_is_exhausted():
    """None is the honest "no mint found" only after the walk reached the
    range's end — an exhausted page without next_block, not an empty first page."""
    pages = {
        0: _page([], next_block=300),
        300: _page([], next_block=None),
    }

    def handler(request):
        return pages[json.loads(request.content)["from_block"]]

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 1_000) is None
    finally:
        await rpc.aclose()


async def test_first_mint_block_budget_stop_is_unknown_not_no_mint():
    """The request cap stopped the scan mid-range: the honest answer is None
    (unknown ⇒ the caller keeps its conservative walk), and no further page
    is asked for."""
    asked = []

    def handler(request):
        asked.append(json.loads(request.content)["from_block"])
        return _page([], next_block=body_next(asked))

    def body_next(asked):
        return (asked[-1] + 100) if asked else 100

    rpc = _client(handler)
    try:
        result = await rpc.first_mint_block(BASE, TOKEN, 10_000, max_calls=2)
    finally:
        await rpc.aclose()
    assert result is None
    assert len(asked) == 2


async def test_first_mint_block_non_advancing_cursor_is_a_loud_error():
    def handler(_request):
        return _page([], next_block=5)

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.first_mint_block(BASE, TOKEN, 10_000)
    finally:
        await rpc.aclose()


async def test_first_mint_block_none_when_no_rows():
    rpc = _client(lambda _r: _page([], next_block=None))
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 10_000_000) is None
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# Fail-closed pagination (plan 4.2): an unreadable row quarantines the range
# ---------------------------------------------------------------------------
async def test_an_undecodable_row_raises_instead_of_vanishing():
    """A log row that cannot be decoded might carry a balance change; the old
    code skipped it and still declared the range complete — a silent hole in
    the ledger. It must be a loud error the caller can retry, never a gap."""

    def handler(_request):
        return _page([{"block_number": "not-a-number"}], next_block=None)

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


async def test_a_non_object_row_raises_instead_of_vanishing():
    def handler(_request):
        return httpx.Response(200, json={"data": [{"logs": ["raw-string"]}]})

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# The deadline: a time cap is a short exit, not a failure
# ---------------------------------------------------------------------------
async def test_deadline_exits_short_after_the_first_page():
    """The budget gates the **first** request too (round 3, review item 2B):
    a walk whose budget was already consumed by the mint scan must not start
    one more page read — the deadline is checked before every request, and an
    expired one means zero requests plus an honest resume point."""
    def handler(request):
        body = json.loads(request.content)
        return _page([], next_block=body["from_block"] + 100)

    rpc = _client(handler)
    try:
        _logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 10_000,
            sleep=_noop, deadline=time_deadline_in_the_past(),
        )
    finally:
        await rpc.aclose()
    assert requests == 0                           # expired ⇒ zero, not a courtesy call
    assert complete is False
    assert resume == 0                             # nothing read ⇒ resume at the start


def time_deadline_in_the_past():
    import time

    return time.monotonic() - 1


# ---------------------------------------------------------------------------
# The cycle-level routing: the backfill goes to HyperSync, the head does not
# ---------------------------------------------------------------------------
async def test_backfill_uses_hypersync_when_covered(db, monkeypatch):
    """The whole point: a covered network's history read leaves the public node."""
    import config
    import evm_layer

    NET = "8453"
    monkeypatch.setattr(config, "EVM_NETWORKS", (NET,))
    _watch(db, token=TOKEN, network=NET)
    db.set_evm_cursor(NET, 300, NOW, "ok")

    ZERO = "0x" + "0" * 40

    rpc = _CycleRPC(head=400, logs=[_log(ZERO, A, 700, 150)])

    class _Hyper:
        def covers(self, network_id):
            return str(network_id) == NET

        async def first_mint_block(self, network_id, address, head,
                                   max_calls=None, deadline=None):
            return 100

        async def get_logs_paged(self, network_id, addresses, from_block,
                                 to_block, topics=None, max_calls=None,
                                 sleep=None, deadline=None):
            # ZERO→B: a mint, so the balance ledger stays non-negative.
            return ([_log(ZERO, B, 500, 150)], 1, True, int(to_block))

    async def _noop_sleep(_s):
        pass

    stats = await evm_layer.run_evm_cycle(
        rpc, db, NOW, sleep=_noop_sleep, hyper=_Hyper(),
    )

    assert stats["evm_backfilled"] == 1
    assert db.evm_top_balances(NET, TOKEN, 10) == [(B, 500)]
    # The head anchor stayed on the public RPC, not HyperSync:
    assert rpc.heads == [NET]


async def test_backfill_falls_back_to_public_rpc_when_not_covered(db, monkeypatch):
    """An uncovered network (or a missing key) must behave exactly as before."""
    import config
    import evm_layer

    NET = "4663"
    monkeypatch.setattr(config, "EVM_NETWORKS", (NET,))
    _watch(db, token=TOKEN, network=NET)
    db.set_evm_cursor(NET, 300, NOW, "ok")

    rpc = _CycleRPC(head=400, mint=100, logs=[_log("0x" + "0" * 40, B, 500, 150)])

    class _Hyper:
        def covers(self, network_id):
            return False                           # no key / no route

        async def first_mint_block(self, *_a):
            raise AssertionError("an uncovered network must not reach HyperSync")

    async def _noop_sleep(_s):
        pass

    stats = await evm_layer.run_evm_cycle(
        rpc, db, NOW, sleep=_noop_sleep, hyper=_Hyper(),
    )

    assert stats["evm_backfilled"] == 1
    assert db.evm_top_balances(NET, TOKEN, 10) == [(B, 500)]
    assert rpc.mint_scans == [(NET, TOKEN, 388)]   # the public node's mint scan ran


# ---------------------------------------------------------------------------
# The fast lane: the paid route gets the paid route's caps
# ---------------------------------------------------------------------------
async def test_the_fast_lane_call_cap_reaches_the_hypersync_path(db, monkeypatch):
    """24 calls/coin is a public-node ration; the covered path gets its own."""
    import config
    import evm_layer

    NET = "8453"
    monkeypatch.setattr(config, "EVM_NETWORKS", (NET,))
    _watch(db, token=TOKEN, network=NET)
    db.set_evm_cursor(NET, 300, NOW, "ok")

    ZERO = "0x" + "0" * 40
    seen = {}

    class _Hyper:
        def covers(self, network_id):
            return str(network_id) == NET

        async def first_mint_block(self, *_a):
            return 100

        async def get_logs_paged(self, network_id, addresses, from_block,
                                 to_block, topics=None, max_calls=None,
                                 sleep=None, deadline=None):
            seen["max_calls"] = max_calls
            return ([_log(ZERO, B, 500, 150)], 1, True, int(to_block))

    await evm_layer.run_evm_cycle(
        _CycleRPC(head=400, logs=[_log(ZERO, A, 700, 150)]),
        db, NOW, sleep=_noop, hyper=_Hyper(),
    )
    assert seen["max_calls"] == config.EVM_HYPERSYNC_MAX_CALLS


async def test_pages_wait_the_fast_lane_pacing_not_the_public_one(monkeypatch):
    """The 0.4s public-node courtesy (a measured 429) must not tax the paid route."""
    import config

    monkeypatch.setattr(config, "EVM_HYPERSYNC_PACING_SECONDS", 0.123)
    waited = []

    async def _recording_sleep(seconds):
        waited.append(seconds)

    pages = {
        0: _page([], next_block=100),
        100: _page([], next_block=200),
        200: _page([], next_block=None),
    }

    def handler(request):
        return pages[json.loads(request.content)["from_block"]]

    rpc = _client(handler)
    try:
        await rpc.get_logs_paged(BASE, [TOKEN], 0, 250, sleep=_recording_sleep)
    finally:
        await rpc.aclose()
    assert waited == [0.123, 0.123]          # between pages only, never before the first


# ---------------------------------------------------------------------------
# Route loss mid-backfill: the public node answers once, never twice
# ---------------------------------------------------------------------------
async def test_backfill_falls_back_when_the_route_goes_away(db, monkeypatch):
    """Plan 4.4: an `EnvioUnavailable` from the history read must not burn the
    token's error/retry budget — the public node reads the same range in the
    same call and the backfill still completes, counted in its own stat."""
    import config
    import evm_layer
    from envio_hypersync import EnvioUnavailable

    NET = "8453"
    monkeypatch.setattr(config, "EVM_NETWORKS", (NET,))
    _watch(db, token=TOKEN, network=NET)
    db.set_evm_cursor(NET, 300, NOW, "ok")

    ZERO = "0x" + "0" * 40
    rpc = _CycleRPC(head=400, logs=[_log(ZERO, A, 700, 150)])

    class _Hyper:
        def can_attempt(self, network_id):
            return str(network_id) == NET

        def covers(self, network_id):
            return str(network_id) == NET

        async def first_mint_block(self, *_a):
            return None                     # unknown ⇒ from-zero walk

        async def get_logs_paged(self, *_a, **_k):
            raise EnvioUnavailable("hypersync [8453] HTTP 401")

    stats = await evm_layer.run_evm_cycle(
        rpc, db, NOW, sleep=_noop, hyper=_Hyper(),
    )

    assert stats["evm_hyper_fallbacks"] == 1        # the route event is its own counter
    assert stats["evm_backfill_errors"] == 0        # the token did nothing wrong
    assert stats["evm_backfill_retry"] == 0
    assert stats["evm_backfilled"] == 1             # and it still completed, once
    assert db.evm_top_balances(NET, TOKEN, 10) == [(A, 700)]   # applied exactly once
    assert [net for net, _a, _f, _t in rpc.ranges] == [NET]    # by the public node


# ---------------------------------------------------------------------------
# Corrupt pagination pointers: presence without validity is a contract break
# ---------------------------------------------------------------------------
async def test_a_corrupt_next_block_raises_not_completes():
    """A present-but-unparseable next_block ("invalid") must raise, not be
    silently equated with "range complete" — the old code mapped a failed
    int() onto None onto completion, moving the progress cursor with zero
    evidence the range was served."""
    def handler(_request):
        return httpx.Response(200, json={"data": [], "next_block": "invalid"})

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


async def test_a_next_block_past_the_requested_ceiling_raises():
    """The service's cursor claims coverage beyond the blocks asked for —
    adopting it would mark unserved blocks as read. Refused."""
    def handler(_request):
        return _page([], next_block=999_999)

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# Full transfer validation: a convertible-but-undecodable row quarantines too
# ---------------------------------------------------------------------------
async def test_a_transfer_row_that_fails_decode_raises():
    """A row converts to JSON-RPC shape but is not a decodable Transfer (no
    topics, no value — e.g. {"block_number": 10}): the old chain dropped it
    silently in `_deltas_by_token` while the range was declared complete. The
    filter asked for Transfers, so a row answering it that cannot decode is a
    contract break — quarantine, never a silent hole."""
    def handler(_request):
        # Well-formed page carrying a topic-less, data-less row.
        return httpx.Response(200, json={
            "data": [{"logs": [{
                "block_number": 10, "log_index": 0, "transaction_index": 0,
                "address": TOKEN, "data": "", "topic0": None,
            }]}],
            "next_block": None,
        })

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# The mint scan's budget must reach the production call, not exist in theory
# ---------------------------------------------------------------------------
async def test_the_mint_scan_deadline_stops_at_unknown(db, monkeypatch):
    """The wall-clock deadline that bounds the transfer walk must bound the
    mint scan too — the production wrapper (_first_mint_block) passes it, and
    expiry means unknown (None), never a guessed block."""
    rpc = _client(lambda _r: _page([], next_block=5_000))
    try:
        import time

        assert await rpc.first_mint_block(
            BASE, TOKEN, 10_000, deadline=time.monotonic() - 1,
        ) is None
    finally:
        await rpc.aclose()


async def test_the_layer_passes_the_budget_into_the_mint_scan(db, monkeypatch):
    """Plan 4.5's production path: `_backfill_token` must call HyperSync's
    mint scan with the real request cap and deadline, not the adapter's
    effectively-infinite defaults."""
    import config
    import evm_layer

    NET = "8453"
    monkeypatch.setattr(config, "EVM_NETWORKS", (NET,))
    _watch(db, token=TOKEN, network=NET)
    db.set_evm_cursor(NET, 300, NOW, "ok")

    ZERO = "0x" + "0" * 40
    seen = {}

    class _Hyper:
        def can_attempt(self, network_id):
            return str(network_id) == NET

        def covers(self, network_id):
            return str(network_id) == NET

        async def first_mint_block(self, network_id, address, head,
                                   max_calls=None, deadline=None):
            seen["max_calls"] = max_calls
            seen["deadline"] = deadline
            return None

        async def get_logs_paged(self, *_a, **_k):
            return ([_log(ZERO, B, 500, 150)], 1, True, 388)

    await evm_layer.run_evm_cycle(
        _CycleRPC(head=400, logs=[_log(ZERO, A, 700, 150)]),
        db, NOW, sleep=_noop, hyper=_Hyper(),
    )
    assert seen["max_calls"] == config.EVM_HYPERSYNC_MAX_CALLS
    assert seen["deadline"] is not None


# ---------------------------------------------------------------------------
# The fallback read fails too: one shared verdict, not a silent escape
# ---------------------------------------------------------------------------
async def test_a_fallback_that_also_raises_evmloglimit_is_terminal(db, monkeypatch):
    """The public node takes over after the paid route dies, and its read
    raises EVMLogLimit: the verdict must be the terminal `error` state (the
    same as a primary-source EVMLogLimit), not an escape to the generic
    handler that would mark the token `retry` and repeat the impossible
    range every minute."""
    import config
    import evm_layer
    import evm_rpc
    from envio_hypersync import EnvioUnavailable

    NET = "4663"
    monkeypatch.setattr(config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(config, "EVM_MINT_SCAN_NETWORKS", ())
    _watch(db, token=TOKEN, network=NET)
    db.set_evm_cursor(NET, 300, NOW, "ok")

    class _Hyper:
        def can_attempt(self, network_id):
            return str(network_id) == NET

        def covers(self, network_id):
            return str(network_id) == NET

        async def first_mint_block(self, *_a, **_k):
            return None

        async def get_logs_paged(self, *_a, **_k):
            raise EnvioUnavailable("hypersync [4663] HTTP 401")

    class _LimitRPC(_CycleRPC):
        async def get_logs_paged(self, *_a, **_k):
            raise evm_rpc.EVMLogLimit("backend response too large")

    stats = await evm_layer.run_evm_cycle(
        _LimitRPC(head=400, logs=[]), db, NOW, sleep=_noop, hyper=_Hyper(),
    )
    assert stats["evm_hyper_fallbacks"] == 1      # the route change happened
    assert stats["evm_backfill_errors"] == 1     # and the terminal verdict won
    assert db.evm_backfill_state(NET, TOKEN)["status"] == "error"
    assert "EVMLogLimit" in (db.evm_backfill_state(NET, TOKEN).get("last_error") or "")
async def test_cycle_stamps_what_hypersync_will_route(db, monkeypatch):
    """The stamp is routing intent (can_attempt), not proven coverage — the
    bootstrap fix: after a restart a routed network must not sit paused under
    the public-node admission math while the worker is able to use HyperSync."""
    import config
    import evm_layer

    NET = "8453"
    monkeypatch.setattr(config, "EVM_NETWORKS", (NET,))
    _watch(db, token=TOKEN, network=NET)
    db.set_evm_cursor(NET, 300, NOW, "ok")

    ZERO = "0x" + "0" * 40

    class _Hyper:
        def can_attempt(self, network_id):
            return str(network_id) == NET

        def covers(self, network_id):
            return False                       # nothing proven yet this cycle

        async def first_mint_block(self, *_a):
            return 100

        async def get_logs_paged(self, *_a, **_k):
            return ([_log(ZERO, B, 500, 150)], 1, True, 388)

    await evm_layer.run_evm_cycle(
        _CycleRPC(head=400, logs=[_log(ZERO, A, 700, 150)]),
        db, NOW, sleep=_noop, hyper=_Hyper(),
    )
    assert json.loads(db.get_meta("evm_hypersync_covered")) == {
        "at": NOW, "networks": [NET],
    }


async def test_queue_priority_uses_proven_coverage_not_routing(db, monkeypatch):
    """A routed-but-failing network must not jump the backfill queue ahead of
    working public-path tokens — priority stays on proven `covers()` only."""
    import config
    import evm_layer

    ZERO = "0x" + "0" * 40
    SLOW = "0xbbbb000000000000000000000000000000000002"      # public path
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453", "4663"))
    monkeypatch.setattr(config, "EVM_BACKFILL_TOKENS_PER_CYCLE", 1)
    for net, token in (("4663", SLOW), ("8453", TOKEN)):
        _watch(db, token=token, network=net)
        db.set_evm_cursor(net, 300, NOW, "ok")
        db.set_evm_backfill_state(
            net, token, "partial", NOW, from_block=100, to_block=300,
        )

    picked = []

    class _Hyper:
        def can_attempt(self, network_id):
            return True                         # routed everywhere...

        def covers(self, network_id):
            return str(network_id) == "4663"    # ...but proven only on 4663

        async def first_mint_block(self, *_a):
            return None

        async def get_logs_paged(self, network_id, addresses, *_a, **_k):
            picked.append((str(network_id), addresses[0]))
            return ([_log(ZERO, B, 500, 150)], 1, True, 388)

    await evm_layer.run_evm_cycle(
        _CycleRPC(head=400, logs=[_log(ZERO, A, 700, 150)]),
        db, NOW, sleep=_noop, hyper=_Hyper(),
    )

    # The proven-covered 4663 token went first despite being second in the
    # watchlist; the 8453 token (routed but unproven) waited its normal turn.
    assert picked[0] == ("4663", SLOW)


async def test_a_cycle_without_the_adapter_writes_no_stamp(db, monkeypatch):
    """`hyper=None` is the keyless state: the stamp must not exist, so the
    admission gate's freshness check keeps the public-node math."""
    import config
    import evm_layer

    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453",))
    _watch(db, token=TOKEN, network="8453")
    db.set_evm_cursor("8453", 300, NOW, "ok")

    await evm_layer.run_evm_cycle(
        _CycleRPC(head=400, logs=[]), db, NOW, sleep=_noop,
    )
    assert db.get_meta("evm_hypersync_covered") is None


# ---------------------------------------------------------------------------
# Queue priority: the cheap lane clears first, the wall waits
# ---------------------------------------------------------------------------
async def test_a_covered_partial_beats_an_older_public_one(db, monkeypatch):
    """Measured live 2026-09-04: "oldest first" let three Base partials starve
    for hours behind a wall of three-week-old public-path partials — each of
    which eats a whole cycle's budget alone — while the admission gate stayed
    shut. The fast lane sorts to the front because its tokens *leave* the
    queue instead of competing in it."""
    import config
    import evm_layer

    ZERO = "0x" + "0" * 40
    SLOW = "0xbbbb000000000000000000000000000000000002"      # Robinhood
    monkeypatch.setattr(config, "EVM_NETWORKS", ("8453", "4663"))
    monkeypatch.setattr(config, "EVM_BACKFILL_TOKENS_PER_CYCLE", 1)
    for net, token, tried in (
        ("4663", SLOW, "2026-08-14T17:40:13+00:00"),         # three weeks older
        ("8453", TOKEN, "2026-09-04T16:44:28+00:00"),
    ):
        _watch(db, token=token, network=net)
        db.set_evm_cursor(net, 300, NOW, "ok")
        db.set_evm_backfill_state(
            net, token, "partial", tried, from_block=100, to_block=300,
        )

    picked = []

    class _Hyper:
        def covers(self, network_id):
            return str(network_id) == "8453"

        async def first_mint_block(self, *_a):
            return None

        async def get_logs_paged(self, network_id, addresses, *_a, **_k):
            picked.append((str(network_id), addresses[0]))
            return ([_log(ZERO, B, 500, 150)], 1, True, 388)

    rpc = _CycleRPC(head=400, logs=[_log(ZERO, A, 700, 150)])
    stats = await evm_layer.run_evm_cycle(
        rpc, db, NOW, sleep=_noop, hyper=_Hyper(),
    )

    # The Base token took the cycle's only slot despite being the newer
    # attempt, and finished; the older public-path token waits its turn.
    assert picked == [("8453", TOKEN)]
    assert stats["evm_backfilled"] == 1
    assert db.evm_backfill_state("8453", TOKEN)["status"] == "done"


async def test_an_uncovered_network_gets_one_reserved_slot(db, monkeypatch):
    """A covered network with enough partials must not take every slot.

    Measured live 2026-09-06: Robinhood's 60+ covered partials monopolized
    all of `EVM_BACKFILL_TOKENS_PER_CYCLE`, leaving Base's 17 partials
    untouched for 74+ minutes. The last slot is reserved for the oldest
    uncovered token, so the lanes drain in parallel."""
    import config
    import evm_layer

    ZERO = "0x" + "0" * 40
    monkeypatch.setattr(config, "EVM_NETWORKS", ("4663", "8453"))
    monkeypatch.setattr(config, "EVM_BACKFILL_TOKENS_PER_CYCLE", 3)
    covered_tokens = [f"0x{index + 100:040x}" for index in range(4)]
    for token in covered_tokens:                           # all on covered 4663
        _watch(db, token=token, network="4663")
        db.set_evm_backfill_state(
            "4663", token, "partial", NOW, from_block=100, to_block=300,
        )
    base_token = "0xcccc000000000000000000000000000000000003"
    _watch(db, token=base_token, network="8453")           # uncovered 8453
    db.set_evm_backfill_state(
        "8453", base_token, "partial", NOW, from_block=100, to_block=300,
    )
    for net in ("4663", "8453"):
        db.set_evm_cursor(net, 300, NOW, "ok")

    picked = []

    class _Hyper:
        def can_attempt(self, network_id):
            return str(network_id) == "4663"

        def covers(self, network_id):
            return str(network_id) == "4663"

        async def first_mint_block(self, *_a):
            return None

        async def get_logs_paged(self, network_id, addresses, *_a, **_k):
            picked.append((str(network_id), addresses[0]))
            return ([_log(ZERO, B, 500, 150)], 1, True, 388)

    class _BaseRPC(_CycleRPC):
        # 8453 is a creation-scan network: the public path asks for the
        # contract's birth block before walking. `None` = unknown ⇒ the walk
        # keeps the partial's saved resume point, as at genesis.
        async def contract_creation_block(self, *_args):
            return None

    rpc = _BaseRPC(head=400, logs=[_log(ZERO, A, 700, 150)])
    await evm_layer.run_evm_cycle(
        rpc, db, NOW, sleep=_noop, hyper=_Hyper(),
    )

    nets = [net for net, _ in picked]
    # Three slots: two covered through HyperSync, and the reserved third
    # running through the public RPC — 8453 is not covered, so its pick
    # shows up in the RPC's backfill ranges, not in the HyperSync log.
    assert nets.count("4663") == 2
    assert [net for net, _a, _f, _t in rpc.ranges] == ["8453"]


async def test_no_reservation_when_every_pending_token_is_covered(
    db, monkeypatch,
):
    """No uncovered work ⇒ the reservation changes nothing: all slots stay
    covered-first, and a full covered queue keeps its pace."""
    import config
    import evm_layer

    ZERO = "0x" + "0" * 40
    monkeypatch.setattr(config, "EVM_NETWORKS", ("4663",))
    monkeypatch.setattr(config, "EVM_BACKFILL_TOKENS_PER_CYCLE", 2)
    for index in range(3):
        token = f"0x{index + 200:040x}"
        _watch(db, token=token, network="4663")
        db.set_evm_backfill_state(
            "4663", token, "partial", NOW, from_block=100, to_block=300,
        )
    db.set_evm_cursor("4663", 300, NOW, "ok")

    picked = []

    class _Hyper:
        def can_attempt(self, network_id):
            return True

        def covers(self, network_id):
            return True

        async def first_mint_block(self, *_a):
            return None

        async def get_logs_paged(self, network_id, addresses, *_a, **_k):
            picked.append((str(network_id), addresses[0]))
            return ([_log(ZERO, B, 500, 150)], 1, True, 388)

    await evm_layer.run_evm_cycle(
        _CycleRPC(head=400, logs=[_log(ZERO, A, 700, 150)]),
        db, NOW, sleep=_noop, hyper=_Hyper(),
    )

    assert len(picked) == 2
    assert all(net == "4663" for net, _ in picked)


# ---------------------------------------------------------------------------
# Round 2: the pointer must never go backward, and never be almost a number
# ---------------------------------------------------------------------------
async def test_a_backward_pointer_is_an_error_even_on_a_single_block_range():
    """[50, 50] answered with next_block=49 used to fall into the exhausted-tail
    shape (nxt <= lo, empty page, lo >= hi) and be blessed as complete — a
    pointer *behind* from_block is a contract break in every shape; the
    measured tail pins the cursor at from_block, never behind it."""
    def handler(_request):
        return _page([], next_block=49)

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 50, 50, sleep=_noop)
    finally:
        await rpc.aclose()


async def test_a_fractional_or_boolean_pointer_is_outright_rejected():
    """int() is not a type check: int(101.9) is 101 and int(True) is 1, so a
    float or a bool in next_block would be adopted as a plausible cursor.
    Both are contract breaks, refused before any comparison."""
    for bad in (101.9, True):
        def handler(_request, _bad=bad):
            return httpx.Response(
                200, json={"data": [], "next_block": _bad},
            )

        rpc = _client(handler)
        try:
            import evm_rpc

            with pytest.raises(evm_rpc.EVMRPCError):
                await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
        finally:
            await rpc.aclose()


async def test_a_null_next_block_is_not_the_measured_absence():
    """The measured completion signal is the field's **absence**; a JSON null
    is a present field with no value — indistinguishable from a service bug,
    so it is refused rather than mapped onto completion."""
    def handler(_request):
        return httpx.Response(
            200, json={"data": [], "next_block": None},
        )

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# Round 2: the event must belong to the page it arrived on
# ---------------------------------------------------------------------------
async def test_a_transfer_for_another_token_is_a_page_rejection():
    """A perfectly valid Transfer — for a token we did not ask about. The
    filter is single-address, so the response is untrustworthy; adopting the
    row would write another token's movement into this walk."""
    other = "0x" + "d" * 40

    def handler(_request):
        entry = _entry(A, B, 10, 50)
        entry["address"] = other
        return _page([entry], next_block=None)

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


async def test_a_transfer_outside_the_requested_range_is_a_page_rejection():
    """The pointing-back double-apply: request [0, 200], receive a valid
    Transfer at block 150 with next_block=100 — the row sits above the page
    the pointer claims, and under a request cap the walk would return the
    event AND a resume point of 100, so block 150 gets applied now and read
    again after the resume ⇒ a doubled balance. Rows outside the page's own
    coverage quarantine the page instead."""
    def handler(_request):
        return _page([_entry(A, B, 10, 150)], next_block=100)

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(
                BASE, [TOKEN], 0, 200, max_calls=1, sleep=_noop,
            )
    finally:
        await rpc.aclose()


async def test_a_non_object_block_group_is_a_page_rejection():
    """`data` is a list of block groups; a non-object group used to be
    silently skipped while its rows (if any) could never be read."""
    def handler(_request):
        return httpx.Response(200, json={"data": ["raw"], "next_block": None})

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# Round 2: the mint scan's budget is real before the first request
# ---------------------------------------------------------------------------
async def test_an_expired_mint_deadline_makes_zero_http_requests():
    """The deadline is checked before the **first** request, not only between
    pages: an already-expired budget must mean zero HTTP calls, never one
    courtesy request past the deadline."""
    asked = []

    def handler(request):
        asked.append(json.loads(request.content)["from_block"])
        return _page([], next_block=None)

    rpc = _client(handler)
    try:
        import time

        result = await rpc.first_mint_block(
            BASE, TOKEN, 10_000, deadline=time.monotonic() - 1,
        )
    finally:
        await rpc.aclose()
    assert result is None
    assert asked == []                          # zero requests, not one


async def test_a_corrupt_mint_row_next_to_a_valid_one_is_unknown():
    """A page carrying a valid mint block and an unreadable one: min() over
    the rows that happened to parse would confirm a start the page does not
    actually establish. The honest answer is unknown (None)."""
    def handler(_request):
        return httpx.Response(200, json={
            "data": [{"logs": [
                {"block_number": 5_000},
                {"block_number": "corrupt"},
            ]}],
            "next_block": None,
        })

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 10_000) is None
    finally:
        await rpc.aclose()


async def test_the_mint_scan_has_no_unlimited_legacy_path(db, monkeypatch):
    """A _Hyper double with the old 3-arg signature (no max_calls/deadline)
    must surface as unknown, not silently re-route to an unbudgeted call —
    the production wrapper no longer has a TypeError escape hatch. The
    TypeError fires on the budgeted call before the double's body runs, the
    broad except turns it into unknown, and exactly one call to the double
    happens — a legacy re-route would make it two."""
    import config
    import evm_layer

    NET = "8453"
    monkeypatch.setattr(config, "EVM_NETWORKS", (NET,))
    _watch(db, token=TOKEN, network=NET)
    db.set_evm_cursor(NET, 300, NOW, "ok")

    ZERO = "0x" + "0" * 40
    called = {"count": 0}

    class _LegacyHyper:
        def can_attempt(self, network_id):
            return str(network_id) == NET

        def covers(self, network_id):
            return str(network_id) == NET

        async def first_mint_block(self, network_id, address, head):
            # Old signature: the budgeted call raises TypeError before this
            # body can run — which is exactly what the test pins down.
            called["count"] += 1
            return 100

        async def get_logs_paged(self, *_a, **_k):
            return ([_log(ZERO, B, 500, 150)], 1, True, 388)

    stats = await evm_layer.run_evm_cycle(
        _CycleRPC(head=400, logs=[_log(ZERO, A, 700, 150)]),
        db, NOW, sleep=_noop, hyper=_LegacyHyper(),
    )
    # One budgeted call, TypeError → unknown (from-zero walk): no retry, no
    # unbudgeted second attempt, and the cycle still completed.
    assert called["count"] == 0
    assert stats["evm_backfilled"] == 1
    assert db.evm_backfill_state(NET, TOKEN)["status"] == "done"


# ---------------------------------------------------------------------------
# Round 3: the mint page's own pointer gates the blocks it hands back
# ---------------------------------------------------------------------------
async def test_a_mint_block_above_the_head_is_not_a_confirmed_start():
    """The exact gap the review named: a mint row with block_number 500 against
    head=100 — a valid-looking integer that the old code returned before ever
    reading the pointer. A start above the walk's end is unverified data from
    an unreliable page, so the answer is unknown (None), never 500."""
    def handler(_request):
        return httpx.Response(200, json={
            "data": [{"logs": [{"block_number": 500}]}],
            "next_block": 101,
        })

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 100) is None
    finally:
        await rpc.aclose()


async def test_a_mint_block_outside_the_pages_own_coverage_is_unknown():
    """Same never-broken contract as the transfer walk, applied to the scan: a
    mint row at 150 on a page whose pointer claims coverage only to 100 is a
    page that contradicts itself — unknown, not a confirmed start."""
    def handler(_request):
        return httpx.Response(200, json={
            "data": [{"logs": [{"block_number": 150}]}],
            "next_block": 100,
        })

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 1_000) is None
    finally:
        await rpc.aclose()


async def test_a_negative_mint_block_is_unknown():
    """Blocks are unsigned; a negative block_number is a contract break, and a
    confirmed start cannot be built from it."""
    def handler(_request):
        return httpx.Response(200, json={
            "data": [{"logs": [{"block_number": -5}]}],
            "next_block": None,
        })

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 1_000) is None
    finally:
        await rpc.aclose()


async def test_a_valid_mint_still_passes_through_the_pointer_gate():
    """The gate must not refuse honest pages: a mint inside the page's own
    coverage, on a page with a sane pointer, still returns its block."""
    def handler(_request):
        return httpx.Response(200, json={
            "data": [{"logs": [{"block_number": 80}]}],
            "next_block": 101,
        })

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 100) == 80
    finally:
        await rpc.aclose()


async def test_a_corrupt_pointer_on_a_mint_page_with_rows_is_unknown():
    """The pointer is parsed before any block is trusted: a page with mints
    present and a pointer that contradicts itself cannot establish coverage,
    so no block it returns may be confirmed. Unknown, never min(rows)."""
    for bad in (101.9, True):
        def handler(_request, _bad=bad):
            return httpx.Response(200, json={
                "data": [{"logs": [{"block_number": 80}]}],
                "next_block": _bad,
            })

        rpc = _client(handler)
        try:
            assert await rpc.first_mint_block(BASE, TOKEN, 100) is None
        finally:
            await rpc.aclose()


# ---------------------------------------------------------------------------
# Round 3: false is not an empty page, and int() is not a type check
# ---------------------------------------------------------------------------
async def test_a_falsy_logs_value_is_not_an_empty_page():
    """`entry.get("logs") or []` converts `false` (and any corrupt falsy
    value) into "no events here" — blessing an event-free read of a page that
    could not be read. A non-list logs value quarantines the page instead."""
    def handler(_request):
        return httpx.Response(200, json={
            "data": [{"logs": False}], "next_block": 101,
        })

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


async def test_a_falsy_logs_value_in_a_mint_page_is_not_an_empty_page():
    """The same `or []` conversion in the mint scan: a page whose logs are
    `false` cannot be read, and a mint-free verdict on it would be invented.
    Unknown (None), not a completed mint-free scan."""
    def handler(_request):
        return httpx.Response(200, json={
            "data": [{"logs": False}], "next_block": 101,
        })

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 1_000) is None
    finally:
        await rpc.aclose()


async def test_a_fractional_block_number_is_not_a_plausible_number():
    """int(101.9) is 101: a fractional block_number used to be silently
    truncated into a real-looking block. The row is undecodable instead —
    and an undecodable row in a declared-complete page is a raise."""
    def handler(_request):
        entry = _entry(A, B, 10, 101.9)
        return _page([entry], next_block=None)

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 200, sleep=_noop)
    finally:
        await rpc.aclose()


async def test_a_boolean_log_index_is_not_a_plausible_number():
    """int(True) is 1: the same coercion on the row's own indexes. A boolean
    log_index is a corrupt row, refused before conversion."""
    def handler(_request):
        entry = _entry(A, B, 10, 50)
        entry["log_index"] = True
        return _page([entry], next_block=None)

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 200, sleep=_noop)
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# Round 3: the deadline gates the request, not only the page loop
# ---------------------------------------------------------------------------
async def test_an_expired_get_logs_deadline_makes_zero_http_requests():
    """The walk's own guard used to demand `requests > 0`, letting a walk
    whose budget was already consumed by a mint scan start one more page read.
    An expired deadline at entry means zero requests and an honest resume
    point, exactly like the mint scan."""
    asked = []

    def handler(request):
        asked.append(json.loads(request.content)["from_block"])
        return _page([], next_block=None)

    rpc = _client(handler)
    try:
        import time

        logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 100, sleep=_noop,
            deadline=time.monotonic() - 1,
        )
    finally:
        await rpc.aclose()
    assert logs == []
    assert requests == 0
    assert complete is False
    assert resume == 0                     # nothing read ⇒ resume at the start
    assert asked == []                     # zero requests, not one


async def test_a_key_rotation_is_bounded_by_the_deadline():
    """Rotation inherits the deadline: a 401 answer that arrives with the
    budget expired must not rotate and re-ask on the next key — the request
    is refused, not retried outside the budget."""
    import time

    from envio_hypersync import EnvioUnavailable

    asked = []

    def handler(_request):
        asked.append(1)
        return httpx.Response(401)

    rpc = _client(handler, keys=(KEY, "second-key-654321"))
    try:
        with pytest.raises(EnvioUnavailable):
            await rpc.get_logs_paged(
                BASE, [TOKEN], 0, 100, sleep=_noop,
                deadline=time.monotonic() + 60,
            )
        assert len(asked) == 2              # both keys, bounded rotation
        # The expired budget is caught by the walk's own pre-request gate:
        # zero further requests and an honest resume point — the rotation
        # never gets the chance to re-ask outside the budget.
        _logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 100, sleep=_noop,
            deadline=time.monotonic() - 1,
        )
        assert len(asked) == 2              # expired ⇒ no request at all
        assert requests == 0
        assert complete is False
        assert resume == 0
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# Round 4: expiry mid-walk preserves the pages already paid for
# ---------------------------------------------------------------------------
async def test_expiry_before_the_next_page_returns_partial_progress():
    """Page success + timeout before the next request: the completed, verified
    pages come back with complete=False and the first unread block — the token
    resumes from the saved point next cycle instead of re-reading from the old
    start. The pacing sleep between pages is where the budget dies (the walk
    re-checks the deadline before each request), so page one stays saved."""
    import asyncio
    import time

    asked = []

    def handler(request):
        asked.append(json.loads(request.content)["from_block"])
        return _page([_entry(A, B, 10, 50)], next_block=60)

    # The budget dies **during** the pacing sleep between pages: page one
    # answers instantly (0.25s of budget is ample for an in-process mock),
    # the real 0.5s pacing sleep outruns the budget, and the deadline gate
    # after the sleep must refuse the second request — the "sequential parts,
    # small scheduling margin" shape from the review.
    deadline = time.monotonic() + 0.25

    async def slow_pacing(_seconds):
        await asyncio.sleep(0.5)

    rpc = _client(handler)
    try:
        logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 100, sleep=slow_pacing, deadline=deadline,
        )
    finally:
        await rpc.aclose()
    assert requests == 1                     # the interrupted page is not counted
    assert complete is False
    assert resume == 60                      # first unread block, not 0
    assert len(logs) == 1 and logs[0]["blockNumber"] == hex(50)
    assert len(asked) == 1


async def test_expiry_during_the_next_request_cancels_and_keeps_prior_pages():
    """Page success + timeout during the next request: httpx's read timeout is
    per-chunk, so a slow trickle can outlive the budget — asyncio.wait_for
    cancels the request at the deadline and the walk returns the previous
    pages only, with no truncated page."""
    import asyncio
    import time

    asked = []

    async def slow(_request):
        asked.append(1)
        if len(asked) == 1:
            return _page([_entry(A, B, 10, 50)], next_block=60)
        await asyncio.sleep(5)               # a trickle that outlives any budget
        return _page([_entry(A, B, 20, 60)], next_block=70)

    rpc = _client(slow)
    try:
        deadline = time.monotonic() + 0.3
        logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 100, sleep=_noop, deadline=deadline,
        )
    finally:
        await rpc.aclose()
    assert requests == 1                     # the cancelled page is not counted
    assert complete is False
    assert resume == 60
    assert len(logs) == 1 and logs[0]["blockNumber"] == hex(50)
    assert len(asked) == 2                   # the second request started (and was cancelled)


async def test_a_401_with_the_budget_spent_makes_no_second_key_request():
    """The review's named gap: a 401 answered on key 1 with the budget already
    spent — no request with the second key and no RPC rollback: a spent budget
    is not a retry reason. The expired deadline is caught before the first
    request, so the rotation never even begins."""
    import time

    from envio_hypersync import EnvioUnavailable

    asked = []

    def handler(_request):
        asked.append(1)
        return httpx.Response(401)

    rpc = _client(handler, keys=(KEY, "second-key-654321"))
    try:
        with pytest.raises(EnvioUnavailable):
            await rpc.get_logs_paged(
                BASE, [TOKEN], 0, 100, sleep=_noop,
                deadline=time.monotonic() + 60,
            )
        assert len(asked) == 2               # rotation itself is intact
        # Now the same 401 with a budget that is already spent: the deadline
        # must gate the rotation's re-ask, exactly as it gates a page request.
        asked.clear()
        _logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 100, sleep=_noop,
            deadline=time.monotonic() - 1,
        )
        assert asked == []                   # no request, no second key
        assert requests == 0
        assert complete is False
        assert resume == 0
    finally:
        await rpc.aclose()


# ---------------------------------------------------------------------------
# Round 4: one row-extraction policy for both branches
# ---------------------------------------------------------------------------
async def test_a_non_list_logs_in_the_dict_branch_is_never_a_complete_page():
    """`{"data": {"logs": {}}}` used to pass the dict branch as a complete
    empty page — a page that could not be read was blessed as event-free. Both
    branches now share one extraction policy: non-list logs is a rejection."""
    def handler(_request):
        return httpx.Response(200, json={"data": {"logs": {}}, "next_block": 101})

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


async def test_a_null_logs_in_a_block_group_is_an_empty_group_not_an_error():
    """`{"data": [{"logs": null}, ...]}`: the list-branch policy treats a
    missing field and an explicit null identically (an empty block group), so
    the page still hands back its real rows — an explicit null is not the
    measured corruption shape (false, {}) and does not quarantine the page."""
    def handler(_request):
        return httpx.Response(200, json={
            "data": [{"logs": None}, {"logs": [_entry(A, B, 10, 50)]}],
            "next_block": 101,
        })

    rpc = _client(handler)
    try:
        logs, requests, complete, resume = await rpc.get_logs_paged(
            BASE, [TOKEN], 0, 100, sleep=_noop,
        )
        assert requests == 1
        assert complete is True              # next_block 101 = hi+1 ⇒ range served
        assert resume == 100
        assert len(logs) == 1 and logs[0]["blockNumber"] == hex(50)
    finally:
        await rpc.aclose()


async def test_a_dict_branch_without_logs_is_a_rejection_not_a_complete_page():
    """`{"data": {...}}` with no logs key at all: the dict branch's missing
    field stays an explicit refusal (round 3, kept in round 4) — the two
    branches agree on *present but non-list*, and stay deliberately strict
    about *missing* in the dict shape, which never carries a keyless empty
    page in the measured wire format."""
    def handler(_request):
        return httpx.Response(200, json={"data": {"other": 1}, "next_block": 101})

    rpc = _client(handler)
    try:
        import evm_rpc

        with pytest.raises(evm_rpc.EVMRPCError):
            await rpc.get_logs_paged(BASE, [TOKEN], 0, 100, sleep=_noop)
    finally:
        await rpc.aclose()


async def test_a_corrupt_mint_dict_branch_is_unknown_not_an_empty_scan():
    """The unified extractor in the mint scan: `{"data": {"logs": {}}}` cannot
    be read, so no mint-free verdict may be built from it — unknown (None)."""
    def handler(_request):
        return httpx.Response(200, json={"data": {"logs": {}}, "next_block": 101})

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 1_000) is None
    finally:
        await rpc.aclose()


async def test_a_corrupt_mint_list_branch_row_is_unknown_not_an_empty_scan():
    """`{"data": [{"logs": {}}]}` in the mint scan: a non-list logs group is a
    page that cannot be read; the scan converts the unified extractor's
    rejection into unknown, never into "mint-free, walk from zero"."""
    def handler(_request):
        return httpx.Response(200, json={"data": [{"logs": {}}], "next_block": 101})

    rpc = _client(handler)
    try:
        assert await rpc.first_mint_block(BASE, TOKEN, 1_000) is None
    finally:
        await rpc.aclose()
