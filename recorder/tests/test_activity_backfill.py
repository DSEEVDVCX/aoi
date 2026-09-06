"""Tests for the retro tradingActivity backfill (no network).

Locks in the contracts measured live on 2026-07-28: page unpacking with
hasNextPage, extraction of both shapes (the flat swap_* with usdAmount and the
nested multi_user_* with body), and the walk with an idempotent resume point,
--until, and --dry-run.
"""
import os

import backfill_activity as ba
import extract
import pytest
from db import RecorderDB

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-07-28T12:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


# --- raw shapes confirmed live ---
SWAP_BUY = {
    "type": "swap_buy", "id": "sw_1", "tradeId": "tr_1",
    "createdAt": "2026-07-28T12:07:32.857Z", "userId": "u_1",
    "displayName": "Jack", "userHandle": "boosteryting",
    "usdAmount": 26.77, "marketCap": "3531.86", "fdv": "3531.86",
    "price": "3.53e-06", "ticker": "PEPE", "tokenAddress": "0xTok",
    "networkId": 4663, "equity": 120.5,
}

MULTI_BUY = {
    "type": "multi_user_buy", "id": "mu_1", "createdAt": "2026-07-27T04:10:13.035Z",
    "tokenAddress": "0xTokB", "networkId": 1399811149, "userId": "u_9",
    "likes": 3, "pinned": False, "views": 10,
    "body": {
        "areTopTraders": True, "fdv": "42000.5", "marketCap": "41000.0",
        "minutes": 7, "numTrades": 9, "price": 0.00041, "priceChangePercent": 12.5,
        "ticker": "DOGE2", "totalVolume": 9000.0, "uniqueTraders": 4,
        "topTraders": [{"id": "tt_1"}, {"id": "tt_2"}],
    },
}

THESIS_EV = {
    "type": "thesis", "id": "th_1", "createdAt": "2026-07-27T16:42:43.788Z",
    "tokenAddress": "0xTok", "networkId": 4663, "userId": "u_2",
    "userHandle": "writer", "equity": 0, "numReplies": 2,
    "comment": {"comment": "text", "numLikes": 5},
}


def _envelope(items, has_next=True):
    return {"success": True, "responseObject": {"items": items, "hasNextPage": has_next}}


# --- page unpacking ---
def test_activity_page_items_and_has_next():
    items, has_next = extract.activity_page(_envelope([SWAP_BUY], has_next=True))
    assert len(items) == 1 and has_next is True


def test_activity_page_end_of_history():
    items, has_next = extract.activity_page(_envelope([], has_next=False))
    assert items == [] and has_next is False


def test_activity_page_garbage_is_empty_stop():
    assert extract.activity_page(None) == ([], False)
    assert extract.activity_page({"responseObject": {}}) == ([], False)


def test_activity_page_bare_list_envelope():
    items, has_next = extract.activity_page({"responseObject": [SWAP_BUY]})
    assert len(items) == 1 and has_next is False   # no keys ⇒ no pagination available


# --- extraction ---
def test_extract_swap_buy_flat_shape():
    row = extract.extract_activity_event(SWAP_BUY, NOW)
    assert row["event_type"] == "swap_buy"
    assert row["usd_amount"] == 26.77
    assert row["user_id"] == "u_1"
    assert row["market_cap"] == 3531.86          # numeric string → float
    assert row["price_usd"] == 3.53e-06
    assert row["equity"] == 120.5
    assert row["unique_traders"] is None          # body fields absent → None (FR-007)


def test_extract_multi_user_nested_body():
    row = extract.extract_activity_event(MULTI_BUY, NOW)
    assert row["event_type"] == "multi_user_buy"
    assert row["unique_traders"] == 4
    assert row["num_trades"] == 9
    assert row["minutes"] == 7
    assert row["total_volume"] == 9000.0
    assert row["are_top_traders"] == 1
    assert row["market_cap"] == 41000.0
    assert row["ticker"] == "DOGE2"
    import json
    assert json.loads(row["top_trader_ids_json"]) == ["tt_1", "tt_2"]
    assert row["usd_amount"] is None              # flat field absent in this shape


def test_extract_thesis_event_tolerated():
    row = extract.extract_activity_event(THESIS_EV, NOW)
    assert row["event_type"] == "thesis"
    assert row["usd_amount"] is None


def test_extract_missing_id_dropped():
    assert extract.extract_activity_event({"type": "swap_buy"}, NOW) is None
    assert extract.extract_activity_event(None, NOW) is None


# --- the walk ---
class _PagerClient:
    """Fake client returning prepared pages in order, then an empty page."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.params_seen = []

    async def _get(self, path, params=None):
        self.params_seen.append(dict(params or {}))
        if not self._pages:
            return _envelope([], has_next=False)
        return self._pages.pop(0)


async def _noop(_s):
    pass


async def test_walk_inserts_pages_and_checkpoints(db):
    client = _PagerClient([
        _envelope([SWAP_BUY, MULTI_BUY], has_next=True),
        _envelope([THESIS_EV], has_next=False),
    ])
    stats = await ba.walk(client, db, max_pages=10, sleep=_noop)

    assert stats["pages"] == 2
    assert stats["added"] == 3
    assert stats["stopped"] == "has_next_page_false"
    assert db.activity_count() == 3
    assert db.get_meta(ba.META_LAST_ID) == "th_1"
    assert db.get_meta(ba.META_OLDEST) == MULTI_BUY["createdAt"]
    # the second page was requested with the lastId from the first
    assert client.params_seen[1]["lastId"] == "mu_1"


async def test_walk_is_idempotent_on_rerun(db):
    pages = [_envelope([SWAP_BUY], has_next=True)]
    client = _PagerClient([*pages, _envelope([SWAP_BUY], has_next=True)])
    await ba.walk(client, db, max_pages=2, sleep=_noop)
    assert db.activity_count() == 1
    # resume from the checkpoint: the next page returns the same event — OR IGNORE swallows it
    client2 = _PagerClient([_envelope([SWAP_BUY, MULTI_BUY], has_next=False)])
    stats = await ba.walk(client2, db, max_pages=5, sleep=_noop)
    assert stats["added"] == 1                     # only mu_1 is new
    assert db.activity_count() == 2
    assert client2.params_seen[0]["lastId"] == "sw_1"   # resumed from the position


async def test_walk_stops_at_until(db):
    client = _PagerClient([
        _envelope([SWAP_BUY], has_next=True),                    # 2026-07-28
        _envelope([MULTI_BUY], has_next=True),                   # 2026-07-27
        _envelope([THESIS_EV], has_next=True),                   # 2026-07-27
    ])
    stats = await ba.walk(client, db, max_pages=10, until="2026-07-27T05:00:00Z", sleep=_noop)
    assert stats["stopped"].startswith("until:")
    assert stats["pages"] == 2                                   # stopped after the second


async def test_walk_dry_run_writes_nothing_and_no_checkpoint(db):
    client = _PagerClient([_envelope([SWAP_BUY], has_next=False)])
    stats = await ba.walk(client, db, max_pages=5, dry_run=True, sleep=_noop)
    assert stats["events"] == 1
    assert db.activity_count() == 0
    assert db.get_meta(ba.META_LAST_ID) is None


async def test_walk_empty_first_page_stops(db):
    client = _PagerClient([_envelope([], has_next=False)])
    stats = await ba.walk(client, db, max_pages=5, sleep=_noop)
    assert stats["stopped"] == "empty_page"
    assert stats["events"] == 0


async def test_walk_head_repairs_gap_without_replacing_history_cursor(db):
    """Sweeping from the head adds the new until the first overlap and never touches the history cursor."""
    existing = dict(SWAP_BUY)
    db.insert_activity_events([extract.extract_activity_event(existing, NOW)])
    db.set_meta(ba.META_LAST_ID, "old-history-cursor")

    newer = dict(SWAP_BUY, id="new-1", createdAt="2026-08-23T12:00:00.000Z")
    client = _PagerClient([
        _envelope([newer], has_next=True),
        _envelope([existing, MULTI_BUY], has_next=True),
    ])

    stats = await ba.walk_head(client, db, max_pages=5, sleep=_noop)

    assert stats["stopped"] == "overlap"
    assert stats["added"] == 2
    assert db.activity_count() == 3
    assert db.get_meta(ba.META_LAST_ID) == "old-history-cursor"


async def test_walk_head_follows_last_id_across_pages(db):
    """The second page is requested with the `lastId` from the first — no NameError, no head restart."""
    client = _PagerClient([
        _envelope([dict(SWAP_BUY, id="h-1")], has_next=True),
        _envelope([dict(SWAP_BUY, id="h-2")], has_next=False),
    ])

    stats = await ba.walk_head(client, db, max_pages=5, sleep=_noop)

    assert stats["pages"] == 2
    assert stats["added"] == 2
    assert client.params_seen[1]["lastId"] == "h-1"
