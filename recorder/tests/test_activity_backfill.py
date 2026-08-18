"""اختبارات استرجاع tradingActivity الرجعيّ (بلا شبكة).

تثبت العقود المقيسة حيّاً 2026-07-28: فكّ الصفحة مع hasNextPage، استخراج
الشكلين (المسطّح swap_* بـusdAmount والمتداخٍ multi_user_* بـbody)، والمشيّاط
بنقطة استئناف idempotent و--until و--dry-run.
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


# --- أشكال خام مؤكَّدة حيّاً ---
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
    "comment": {"comment": "نصّ", "numLikes": 5},
}


def _envelope(items, has_next=True):
    return {"success": True, "responseObject": {"items": items, "hasNextPage": has_next}}


# --- فكّ الصفحة ---
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
    assert len(items) == 1 and has_next is False   # بلا مفاتيح ⇒ لا ترقيم متاح


# --- الاستخراج ---
def test_extract_swap_buy_flat_shape():
    row = extract.extract_activity_event(SWAP_BUY, NOW)
    assert row["event_type"] == "swap_buy"
    assert row["usd_amount"] == 26.77
    assert row["user_id"] == "u_1"
    assert row["market_cap"] == 3531.86          # نصّ رقميّ → float
    assert row["price_usd"] == 3.53e-06
    assert row["equity"] == 120.5
    assert row["unique_traders"] is None          # حقول body غائبة → None (FR-007)


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
    assert row["usd_amount"] is None              # مسطّح غائب في هذا الشكل


def test_extract_thesis_event_tolerated():
    row = extract.extract_activity_event(THESIS_EV, NOW)
    assert row["event_type"] == "thesis"
    assert row["usd_amount"] is None


def test_extract_missing_id_dropped():
    assert extract.extract_activity_event({"type": "swap_buy"}, NOW) is None
    assert extract.extract_activity_event(None, NOW) is None


# --- المشيّاط ---
class _PagerClient:
    """عميل وهمي يعيد صفحات مُعدّة بالتسلسل ثمّ صفحة فارغة."""

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
    # الصفحة الثانية طُلبت بـ lastId من أولى
    assert client.params_seen[1]["lastId"] == "mu_1"


async def test_walk_is_idempotent_on_rerun(db):
    pages = [_envelope([SWAP_BUY], has_next=True)]
    client = _PagerClient([*pages, _envelope([SWAP_BUY], has_next=True)])
    await ba.walk(client, db, max_pages=2, sleep=_noop)
    assert db.activity_count() == 1
    # إعادة من نقطة الاستئناف: الصفحة التالية تعيد الحدث نفسه — OR IGNORE يبتلعه
    client2 = _PagerClient([_envelope([SWAP_BUY, MULTI_BUY], has_next=False)])
    stats = await ba.walk(client2, db, max_pages=5, sleep=_noop)
    assert stats["added"] == 1                     # mu_1 فقط جديد
    assert db.activity_count() == 2
    assert client2.params_seen[0]["lastId"] == "sw_1"   # استأنف من الموضع


async def test_walk_stops_at_until(db):
    client = _PagerClient([
        _envelope([SWAP_BUY], has_next=True),                    # 2026-07-28
        _envelope([MULTI_BUY], has_next=True),                   # 2026-07-27
        _envelope([THESIS_EV], has_next=True),                   # 2026-07-27
    ])
    stats = await ba.walk(client, db, max_pages=10, until="2026-07-27T05:00:00Z", sleep=_noop)
    assert stats["stopped"].startswith("until:")
    assert stats["pages"] == 2                                   # توقّف بعد الثانية


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
