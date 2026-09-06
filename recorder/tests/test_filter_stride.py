"""Tests for filterTokens request slicing by stride (FILTER_TOKENS_STRIDE)."""

import pytest
from db import RecorderDB

import recorder


class _FilterClient:
    """Answers filterTokens with one item per requested address, and counts the calls."""

    def __init__(self):
        self.calls: list[list[str]] = []

    async def _post(self, path, body):
        self.calls.append(list(body))
        items = []
        for symbol in body:
            addr, _, net = str(symbol).partition(":")
            items.append({"token": {"address": addr, "networkId": net,
                                    "createdAt": 1787000000}})
        return {"responseObject": items}

    async def aclose(self):
        pass


@pytest.fixture()
def db(tmp_path):
    recorder._FILTER_STRIDE_TURN = -1
    value = RecorderDB(str(tmp_path / "t.db"), "schema.sql")
    yield value
    value.close()
    recorder._FILTER_STRIDE_TURN = -1


def _watch(db: RecorderDB, n: int) -> set[tuple[str, str]]:
    watched = set()
    for i in range(n):
        addr = f"tok{i:03d}"
        db.upsert_watch(addr, "1399811149", "large_buy", "s1", 48,
                        "2026-08-25T00:00:00+00:00")
        watched.add((addr, "1399811149"))
    return watched


async def test_stride_two_halves_requests(db, monkeypatch):
    """stride=2: each cycle requests only half the addresses; a token is
    measured every other cycle."""
    monkeypatch.setattr(recorder.config, "FILTER_TOKENS_STRIDE", 2)
    watched = _watch(db, 10)
    client = _FilterClient()

    s1 = await recorder.run_filter_tokens_cycle(
        client, db, "2026-08-25T01:00:00+00:00", watched, set())
    s2 = await recorder.run_filter_tokens_cycle(
        client, db, "2026-08-25T01:01:00+00:00", watched, set())

    total1 = sum(len(c) for c in client.calls)
    assert s1["filter_requested"] <= 5            # half of ten or fewer
    assert s2["filter_requested"] <= 5
    # the union across both cycles covers every address — no token dropped
    asked = {sym.split(":")[0] for c in client.calls for sym in c}
    assert asked == {f"tok{i:03d}" for i in range(10)}
    assert total1 + sum(len(c) for c in client.calls) >= 10


async def test_stride_one_is_legacy_behavior(db, monkeypatch):
    """stride=1: every cycle requests all addresses, as before."""
    monkeypatch.setattr(recorder.config, "FILTER_TOKENS_STRIDE", 1)
    watched = _watch(db, 6)
    client = _FilterClient()
    s = await recorder.run_filter_tokens_cycle(
        client, db, "2026-08-25T01:00:00+00:00", watched, set())
    assert s["filter_requested"] == 6


async def test_empty_slice_costs_no_call(db, monkeypatch):
    """An empty slice (nothing in its turn) costs no call at all."""
    monkeypatch.setattr(recorder.config, "FILTER_TOKENS_STRIDE", 3)
    watched = _watch(db, 3)
    client = _FilterClient()
    # three consecutive cycles: each takes a third — 3 calls total, not 9
    for minute in range(3):
        await recorder.run_filter_tokens_cycle(
            client, db, f"2026-08-25T01:0{minute}:00+00:00", watched, set())
    assert sum(len(c) for c in client.calls) == 3
