"""Tests for the added-data collection: flow · gap filling · traders · source merging.

All offline (fake clients), never touching the live database (tmp_path).
"""
import json
import os
import re
from datetime import datetime

import config
import extract
import features
import pytest
from db import RecorderDB, decode_raw

import recorder

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql"
)
NOW = "2026-08-10T12:00:00+00:00"
# Derived, not hardcoded: a constant that silently disagrees with NOW would make
# the "future row" a past one, so the point-in-time test would pass while
# testing nothing.
NOW_TS = int(datetime.fromisoformat(NOW).timestamp())


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _legacy_filter_stride(monkeypatch):
    """Tests in this unit examine the mapping, not the cadence — restore the
    uniform behavior (every address, every cycle) so stride does not split
    their elements."""
    monkeypatch.setattr(recorder.config, "FILTER_TOKENS_STRIDE", 1)


def _watch(db, token, *, network="56"):
    db.upsert_watch(token, network, "large_buy", f"sig-{token}", 48, NOW)


def _details_envelope(**over):
    """A tokenDetails reply with the **live-measured** keys (25 flow keys,
    100% presence)."""
    ro = {
        "top10HoldersPercent": 42.0, "holders": 900,
        "buyCount5m": 57, "buyCount1": 300, "buyCount4": 900, "buyCount24": 4000,
        "sellCount5m": 20, "sellCount1": 150, "sellCount4": 600, "sellCount24": 3000,
        # The values arrive as **text** from upstream — measured.
        "buyVolume5m": "458", "buyVolume1": "9000", "buyVolume4": "40000",
        "buyVolume24": "117918",
        "sellVolume5m": "229", "sellVolume1": "6000", "sellVolume4": "30000",
        "sellVolume24": "90135",
        "uniqueBuys5m": 30, "uniqueBuys1": 200, "uniqueBuys4": 500,
        "uniqueBuys24": 691,
        "uniqueSells5m": 12, "uniqueSells1": 100, "uniqueSells4": 300,
        "uniqueSells24": 400,
        "isLowFees": False,
    }
    ro.update(over)
    return {"responseObject": ro}


# ---------------------------------------------------------------------------
# The flow extractor
# ---------------------------------------------------------------------------
def test_flow_extractor_converts_string_volumes():
    """Upstream sends volume as text; a REAL column needs a number."""
    row = extract.extract_token_flow(
        _details_envelope(), "tok", "56", NOW, NOW, "sig-1", 0
    )
    assert row is not None
    assert row["buy_volume_24h"] == 117918.0
    assert row["sell_volume_24h"] == 90135.0
    assert row["buy_count_5m"] == 57
    assert row["unique_buys_24h"] == 691


def test_flow_extractor_has_no_12h_tier():
    """The source does not provide 12h — so there is no column for it at all
    (no dead column)."""
    row = extract.extract_token_flow(
        _details_envelope(), "tok", "56", NOW, NOW, None, 0
    )
    assert not [k for k in row if "12h" in k]


def test_flow_extractor_keeps_false_distinct_from_missing():
    """False ≠ absent: the first is a measurement, the second is not (FR-007)."""
    has = extract.extract_token_flow(
        _details_envelope(isLowFees=False), "t", "56", NOW, NOW, None, 0
    )
    assert has["is_low_fees"] == 0

    env = _details_envelope()
    del env["responseObject"]["isLowFees"]
    absent = extract.extract_token_flow(env, "t", "56", NOW, NOW, None, 0)
    assert absent["is_low_fees"] is None


def test_flow_extractor_rejects_response_without_flow():
    """A holders reply without flow must not fabricate an empty row."""
    assert extract.extract_token_flow(
        {"responseObject": {"top10HoldersPercent": 42.0, "holders": 900}},
        "t", "56", NOW, NOW, None, 0,
    ) is None
    assert extract.extract_token_flow(None, "t", "56", NOW, NOW, None, 0) is None
    assert extract.extract_token_flow(
        {"responseObject": []}, "t", "56", NOW, NOW, None, 0
    ) is None


def test_flow_extractor_survives_partial_tiers():
    """One tier present is enough; the rest stay NULL, not zero."""
    row = extract.extract_token_flow(
        {"responseObject": {"buyCount5m": 3}}, "t", "56", NOW, NOW, None, 0
    )
    assert row is not None
    assert row["buy_count_5m"] == 3
    assert row["buy_count_24h"] is None
    assert row["sell_volume_5m"] is None


# ---------------------------------------------------------------------------
# The holders cycle writes flow from the **same** reply
# ---------------------------------------------------------------------------
class _DetailsClient:
    def __init__(self, replies=None, fail=None, top_fail=None):
        self.calls = []
        self._replies = replies or {}
        self._fail = fail or set()
        self._top_fail = top_fail or set()

    async def _post(self, path, body):
        self.calls.append(("POST", path, body))
        addr = body.get("tokenId", "").split(":")[0]
        if addr in self._fail:
            raise RuntimeError("upstream boom")
        return self._replies.get(addr, _details_envelope())

    async def _get(self, path, params):
        self.calls.append(("GET", path, params))
        tokens = json.loads(params["tokens"])
        addr = tokens[0]["address"] if tokens else ""
        if addr in self._top_fail:
            raise RuntimeError("upstream boom")
        return {"responseObject": [{"totalHolders": 42, "topHolders": []}]}


async def _noop(_seconds):
    pass


async def test_holders_cycle_writes_flow_without_extra_call(db):
    """Flow comes from the tokenDetails reply itself — **zero extra calls**."""
    _watch(db, "tok")
    client = _DetailsClient()

    stats = await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    assert stats["flow_rows"] == 1
    assert stats["holders_details"] == 1
    # Two calls, not three: tokenDetails and hodlers/top only.
    assert len(client.calls) == 2

    row = db._conn.execute("SELECT * FROM token_flow").fetchone()
    assert row["buy_volume_24h"] == 117918.0
    assert row["buy_count_5m"] == 57
    assert row["is_low_fees"] == 0
    assert decode_raw(row["raw_json"])["responseObject"]["buyCount5m"] == 57


async def test_flow_written_even_when_holding_data_absent(db):
    """The holders extractor returns None without ratios — and must not
    swallow the flow with it.

    This is why the two tables are separate: if flow were tied to the success
    of holders it would be lost, while it is present 100% and the holders
    ratios are not.
    """
    _watch(db, "noholders")
    env = _details_envelope()
    del env["responseObject"]["top10HoldersPercent"]
    del env["responseObject"]["holders"]
    client = _DetailsClient(replies={"noholders": env})

    stats = await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    assert stats["holders_details"] == 0   # no holders row
    assert stats["flow_rows"] == 1         # but the flow survived
    assert db._conn.execute("SELECT COUNT(*) FROM token_flow").fetchone()[0] == 1


async def test_fetch_failure_writes_no_flow_row(db):
    """A fetch failure must not leave a stale `raw` from which a false flow row
    would be written."""
    _watch(db, "dead")
    client = _DetailsClient(fail={"dead"})

    stats = await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    assert stats["flow_rows"] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM token_flow").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Filling the measurement gap (filterTokens)
# ---------------------------------------------------------------------------
def _filter_item(addr, *, net="56", price=1.0, protocol=None, created=None):
    item = {
        "token": {"address": addr, "networkId": net, "symbol": "AAA"},
        "priceUSD": price, "liquidity": 5000.0, "volume24": 1000.0,
    }
    if protocol is not None:
        item["pair"] = {"protocol": protocol}
    if created is not None:
        item["token"]["createdAt"] = created
        item["createdAt"] = created
    return item


def test_static_extractor_preserves_pair_protocol():
    """A protocol present in the raw data must not be lost from token_static."""
    row = extract.extract_token_static(
        _filter_item("tok", protocol="PumpAmm", created=1700000000), NOW
    )

    assert row is not None
    assert row["dex_protocol"] == "PumpAmm"


class _FilterClient:
    def __init__(self, items=None, fail=False):
        self.calls = []
        self._items = items
        self._fail = fail

    async def _post(self, path, body):
        self.calls.append(body)
        if self._fail:
            raise RuntimeError("upstream boom")
        if self._items is not None:
            return {"responseObject": self._items}
        addrs = [s.split(":")[0] for s in body]
        return {"responseObject": [_filter_item(a) for a in addrs]}


async def test_filter_cycle_only_requests_the_gap(db):
    """What trending already captured is not requested again — gap filling,
    not a full sweep."""
    _watch(db, "seen")
    _watch(db, "missed")
    client = _FilterClient()
    watched = {("seen", "56"), ("missed", "56")}
    captured = {("seen", "56")}

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, watched, captured, sleep=_noop
    )

    assert stats["filter_requested"] == 1
    assert client.calls == [["missed:56"]]
    assert stats["filter_ticks"] == 1
    rows = db._conn.execute(
        "SELECT token_address, source FROM market_ticks"
    ).fetchall()
    assert [(r["token_address"], r["source"]) for r in rows] == [("missed", "filter")]


async def test_filter_cycle_no_gap_makes_no_call(db):
    _watch(db, "seen")
    client = _FilterClient()

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("seen", "56")}, {("seen", "56")}, sleep=_noop
    )

    assert client.calls == []
    assert stats == {"filter_requested": 0, "filter_ticks": 0, "filter_errors": 0,
                     "filter_static": 0}


async def test_filter_cycle_maps_by_address_not_position(db):
    """Upstream **silently drops the dead**, so positions slip — bind by address
    alone."""
    for t in ("a", "b", "c"):
        _watch(db, t)
    watched = {("a", "56"), ("b", "56"), ("c", "56")}
    # 'b' is dropped: it returns 'c' then 'a', in reversed order too.
    client = _FilterClient(items=[_filter_item("c", price=3.0),
                                  _filter_item("a", price=1.0)])

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, watched, set(), sleep=_noop
    )

    assert stats["filter_requested"] == 3
    assert stats["filter_ticks"] == 2       # a dead one does not break the batch
    prices = {
        r["token_address"]: r["price_usd"]
        for r in db._conn.execute("SELECT token_address, price_usd FROM market_ticks")
    }
    assert prices == {"a": 1.0, "c": 3.0}   # index binding would have flipped the values


async def test_filter_cycle_maps_by_address_and_network(db):
    """The same address can exist on two networks; never mix one network's
    history/price with another's."""
    _watch(db, "same", network="56")
    _watch(db, "same", network="8453")
    client = _FilterClient(items=[
        _filter_item("same", net="8453", price=8.0, created=1700000008),
        _filter_item("same", net="56", price=5.0, created=1700000005),
    ])

    await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("same", "56"), ("same", "8453")}, set(), sleep=_noop
    )

    rows = db._conn.execute(
        "SELECT network_id, price_usd FROM market_ticks "
        "WHERE token_address='same' ORDER BY network_id"
    ).fetchall()
    assert [(row["network_id"], row["price_usd"]) for row in rows] == [
        ("56", 5.0), ("8453", 8.0),
    ]


async def test_filter_cycle_fills_dex_protocol(db):
    """`dex_protocol` is absent from trending (0 of 3,000) — this is its only
    source."""
    _watch(db, "tok")
    db.upsert_static({
        **{k: None for k in (
            "symbol", "name", "created_at", "first_seen_at", "decimals",
            "chain", "is_verified", "dex_protocol")},
        "token_address": "tok", "network_id": "56", "recorded_at": NOW,
        "raw_json": "{}",
    })
    client = _FilterClient(items=[_filter_item("tok", protocol="PumpAmm")])

    await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("tok", "56")}, set(), sleep=_noop
    )

    proto = db._conn.execute(
        "SELECT dex_protocol FROM token_static WHERE token_address='tok'"
    ).fetchone()["dex_protocol"]
    assert proto == "PumpAmm"


async def test_filter_cycle_fills_missing_static_row(db):
    """The age hole: the global watchlist gates on popularity, not age, so 177
    coins never appeared in it even once — no static row, no creation date.
    And a filterTokens item has the same shape as a trending item, so it
    carries `token.createdAt` — and the item is in our hands in the very same
    cycle."""
    _watch(db, "orphan")
    client = _FilterClient(items=[_filter_item("orphan", created=1700000000)])

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("orphan", "56")}, set(), sleep=_noop
    )

    assert stats["filter_static"] == 1
    row = db._conn.execute(
        "SELECT token_created_at, symbol FROM token_static WHERE token_address='orphan'"
    ).fetchone()
    assert row["token_created_at"] == "1700000000"
    assert row["symbol"] == "AAA"          # a full row, not a single column


async def test_filter_cycle_fills_empty_age_on_existing_row(db):
    """The row exists and the age is missing (upstream omitted it: 3 of 406) —
    and `upsert_static` is INSERT OR IGNORE, so it will not fix it; a narrow
    update path is required."""
    _watch(db, "tok")
    db.upsert_static({
        "token_address": "tok", "network_id": "56", "recorded_at": NOW,
        "symbol": "OLD", "token_created_at": None, "raw_json": "{}",
    })
    client = _FilterClient(items=[_filter_item("tok", created=1700000000)])

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("tok", "56")}, set(), sleep=_noop
    )

    assert stats["filter_static"] == 1
    row = db._conn.execute(
        "SELECT token_created_at, symbol FROM token_static WHERE token_address='tok'"
    ).fetchone()
    assert row["token_created_at"] == "1700000000"
    assert row["symbol"] == "OLD"          # the row was not replaced; only its column was filled


async def test_filter_cycle_does_not_persist_future_creation_date(db):
    _watch(db, "future")
    client = _FilterClient(items=[
        _filter_item("future", created="2099-01-01T00:00:00Z"),
    ])

    await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("future", "56")}, set(), sleep=_noop
    )

    row = db._conn.execute(
        "SELECT token_created_at FROM token_static WHERE token_address='future'"
    ).fetchone()
    assert row["token_created_at"] is None


async def test_filter_cycle_never_overwrites_a_recorded_age(db):
    """The upstream's own value **changes** (53 of 216 disagree with the stored
    one, one by a full year). If we followed its changes, the age gate's
    verdict would flip under a coin already accepted — so we freeze the first
    value we saw."""
    _watch(db, "tok")
    db.upsert_static({
        "token_address": "tok", "network_id": "56", "recorded_at": NOW,
        "token_created_at": "1600000000", "raw_json": "{}",
    })
    client = _FilterClient(items=[_filter_item("tok", created=1700000000)])

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("tok", "56")}, set(), sleep=_noop
    )

    assert stats["filter_static"] == 0
    age = db._conn.execute(
        "SELECT token_created_at FROM token_static WHERE token_address='tok'"
    ).fetchone()["token_created_at"]
    assert age == "1600000000"


async def test_market_list_stamps_an_unobserved_age_without_waiting_for_admission(db):
    _watch(db, "tok")
    db.upsert_static({
        "token_address": "tok", "network_id": "56", "recorded_at": NOW,
        "token_created_at": "1600000000", "raw_json": "{}",
    })
    db._conn.execute(
        "UPDATE token_static SET token_created_at_observed_at=NULL "
        "WHERE token_address='tok'"
    )
    db._conn.commit()

    recorder._write_filter_static(
        db, _filter_item("tok", created=1600000000), "tok", "56", NOW,
        replace_invalid=True,
    )

    row = db._conn.execute(
        "SELECT token_created_at_observed_at FROM token_static "
        "WHERE token_address='tok'"
    ).fetchone()
    assert row["token_created_at_observed_at"] == NOW


async def test_filter_cycle_error_does_not_raise(db):
    _watch(db, "tok")
    client = _FilterClient(fail=True)

    stats = await recorder.run_filter_tokens_cycle(
        client, db, NOW, {("tok", "56")}, set(), sleep=_noop
    )

    assert stats["filter_errors"] == 1
    assert stats["filter_ticks"] == 0
    assert "last_error_filter" in (db.get_meta("last_error_filter") or "") or \
        db.get_meta("last_error_filter") is not None


# ---------------------------------------------------------------------------
# The traders cycle
# ---------------------------------------------------------------------------
def _trader_envelope(tid="u1", **over):
    """The fields **live-measured** on /v2/users/{id} (26 keys)."""
    ro = {
        "id": tid, "userHandle": "Thepennyflippe", "displayName": "Max",
        "followers": 2143, "following": 68, "swapCount": 5450,
        "numTrades": 518, "totalVolume": 12990492.90762,
        "averageHoldTimeSeconds": 38304, "isRestricted": False,
        "private": False, "address": "GW8Pf", "evmAddress": "0x47ae",
        "twitter": "https://x.com/Thepennyflippe",
        "createdAt": "2025-09-07T15:30:29.242Z",
    }
    ro.update(over)
    return {"responseObject": ro}


def test_trader_extractor_maps_measured_fields():
    row = extract.extract_trader(_trader_envelope(), "u1", NOW)
    assert row["trader_id"] == "u1"
    assert row["followers_count"] == 2143
    assert row["swap_count"] == 5450
    assert row["num_trades"] == 518
    assert row["total_volume_usd"] == pytest.approx(12990492.90762)
    assert row["avg_hold_seconds"] == 38304
    assert row["is_restricted"] == 0
    assert row["handle"] == "Thepennyflippe"


def test_trader_extractor_rejects_bad_envelope():
    assert extract.extract_trader({"success": True}, "u1", NOW) is None
    assert extract.extract_trader(None, "u1", NOW) is None


def test_batch_extractor_keys_rows_by_the_id_the_source_returned():
    """The key is what the source returns in `id`, not the order we asked for.

    And the key must not be corrupted by ordering: the source drops unknowns
    from `users`, so a reply of three to a request of four shifts every row
    after the missing one if bound by index.
    """
    envelope = {"responseObject": {"users": [
        _trader_envelope("aaa", followers=1)["responseObject"],
        _trader_envelope("bbb", followers=2)["responseObject"],
    ]}}

    rows = extract.extract_traders(envelope, NOW)

    assert sorted(rows) == ["aaa", "bbb"]
    assert rows["bbb"]["followers_count"] == 2
    assert rows["aaa"]["swap_count"] == 5450


def test_batch_extractor_stores_only_the_user_object_as_raw():
    """`raw_json` is the user object alone: a hundred rows carrying the reply
    of the hundred = a hundredfold."""
    envelope = {"responseObject": {"users": [
        _trader_envelope("aaa")["responseObject"],
        _trader_envelope("bbb")["responseObject"],
    ]}}

    raw = decode_raw(extract.extract_traders(envelope, NOW)["aaa"]["raw_json"])

    assert raw["id"] == "aaa"
    assert "users" not in raw and "responseObject" not in raw


def test_batch_extractor_rejects_bad_envelopes_without_raising():
    """A malformed reply = zero rows, not an exception that takes down the
    whole traders cycle."""
    assert extract.extract_traders(None, NOW) == {}
    assert extract.extract_traders({"success": True}, NOW) == {}
    assert extract.extract_traders({"responseObject": {}}, NOW) == {}
    assert extract.extract_traders({"responseObject": {"users": "nope"}}, NOW) == {}
    # And a user without `id` has no key, so it is dropped — without dropping
    # its neighbors with it.
    rows = extract.extract_traders(
        {"responseObject": {"users": [
            {"userHandle": "no-id"},
            _trader_envelope("aaa")["responseObject"],
        ]}},
        NOW,
    )
    assert list(rows) == ["aaa"]


def _signal(db, sid, buyer, token="tok"):
    db._conn.execute(
        "INSERT INTO signal_events (id, token_address, network_id, ts, "
        "recorded_at, signal_type, buyer_id, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (sid, token, "56", NOW, NOW, "large_buy", buyer, b"{}"),
    )
    db._conn.commit()


def _uuid(tag: str) -> str:
    """A well-formed identifier from a readable name.

    The source requires a uuid: a malformed identifier returns 400 for the
    whole batch, and all 8,026 production identifiers are uuids without
    exception (measured 2026-08-20). So a test with short identifiers like
    "u1" was measuring a path that does not exist in reality.
    """
    body = re.sub(r"[^0-9a-f]", "0", tag.lower().ljust(8, "0"))[:8]
    return f"{body}-0000-5000-8000-000000000000"


class _TraderClient:
    """Mimics `/v2/users?userIds=…&userIds=…`: one batch returning `users[]`.

    Whoever is absent from the list simply has no user with their id — the
    source silently drops the unknown without an error (measured: 6
    identifiers requested, 4 came back). `calls` is the list of batches, not
    identifiers, because the number of calls is what changed: 50 traders in a
    single call.
    """

    def __init__(self, replies=None, fail=None, missing=None):
        self.calls = []
        self._replies = replies or {}
        self._fail = fail or set()
        self._missing = missing or set()

    async def _get(self, path, params=None):
        ids = list((params or {}).get("userIds") or [])
        self.calls.append(ids)
        if self._fail.intersection(ids):
            raise RuntimeError("upstream boom")
        users = [
            (self._replies.get(tid) or _trader_envelope(tid))["responseObject"]
            for tid in ids
            if tid not in self._missing
        ]
        return {"responseObject": {"users": users}}


async def test_traders_cycle_fetches_only_repeat_buyers(db, monkeypatch):
    """Whoever appears once has no behavior for us to learn ⇒ not fetched."""
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    for i in range(3):
        _signal(db, f"s{i}", _uuid("repeat"))
    _signal(db, "once", _uuid("oneshot"))
    client = _TraderClient()

    stats = await recorder.run_traders_cycle(client, db, NOW, sleep=_noop)

    assert client.calls == [[_uuid("repeat")]]  # one call, with the repeat buyer only
    assert stats["traders_rows"] == 1
    row = db._conn.execute("SELECT * FROM traders").fetchone()
    assert row["trader_id"] == _uuid("repeat")
    assert row["followers_count"] == 2143


async def test_traders_cycle_upserts_changing_profile(db, monkeypatch):
    """The profile **changes** (followers, hold time) ⇒ REPLACE, not IGNORE."""
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    monkeypatch.setattr(config, "TRADERS_REFRESH_SECONDS", 0)
    tid = _uuid("u1")
    for i in range(3):
        _signal(db, f"s{i}", tid)

    await recorder.run_traders_cycle(
        _TraderClient(replies={tid: _trader_envelope(tid, followers=100)}),
        db, NOW, sleep=_noop,
    )
    later = "2026-08-11T12:00:00+00:00"
    await recorder.run_traders_cycle(
        _TraderClient(replies={tid: _trader_envelope(tid, followers=999)}),
        db, later, sleep=_noop,
    )

    rows = db._conn.execute("SELECT * FROM traders").fetchall()
    assert len(rows) == 1                       # one row, not two
    assert rows[0]["followers_count"] == 999    # and the newer value won
    assert rows[0]["recorded_at"] == later


async def test_traders_absent_from_the_batch_is_empty_not_error(db, monkeypatch):
    """Absence from `users` is an answer, not a failure ⇒ `empty`, not `error`.

    And this is the difference that hid a 21-hour outage: when /v2/users/{id}
    died, a 404 and "deleted account" were one path, so `empty` was written
    for every trader without a single error.
    """
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    gone = _uuid("gone")
    for i in range(3):
        _signal(db, f"s{i}", gone)
    client = _TraderClient(missing={gone})

    stats = await recorder.run_traders_cycle(client, db, NOW, sleep=_noop)

    assert stats["traders_errors"] == 0
    assert stats["traders_rows"] == 0
    assert stats["traders_missing"] == 1
    state = db._conn.execute(
        "SELECT last_status FROM traders_fetch_state WHERE trader_id=?", (gone,)
    ).fetchone()
    assert state["last_status"] == "empty"


async def test_a_malformed_id_is_dropped_before_it_can_kill_the_batch(db, monkeypatch):
    """A malformed identifier returns 400 for the whole batch, so it is never
    sent at all.

    And `unsupported`, not `error`: a retry will not fix a malformed shape,
    and the eligibility query excludes `unsupported` for good while retrying
    on `error`.
    """
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    good = _uuid("good")
    for i in range(3):
        _signal(db, f"a{i}", "not-a-uuid")
    for i in range(3):
        _signal(db, f"b{i}", good, token="tok2")
    client = _TraderClient()

    stats = await recorder.run_traders_cycle(client, db, NOW, sleep=_noop)

    assert client.calls == [[good]]             # the malformed one was never sent
    assert stats["traders_malformed"] == 1
    assert stats["traders_errors"] == 0
    assert stats["traders_rows"] == 1
    assert db._conn.execute(
        "SELECT last_status FROM traders_fetch_state WHERE trader_id='not-a-uuid'"
    ).fetchone()["last_status"] == "unsupported"


async def test_a_failed_batch_does_not_take_the_next_one_with_it(db, monkeypatch):
    """The batch became the unit of failure instead of the trader, so the
    isolation is measured between two batches.

    And no status is written for one whose batch fell: a status means "we
    asked and this is the answer", and writing `error` would push it into an
    hour-long cooldown for the network's fault, not its own.
    """
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    monkeypatch.setattr(config, "TRADERS_BATCH_MAX", 1)   # one batch per trader
    bad, good = _uuid("bad"), _uuid("good")
    for i in range(3):
        _signal(db, f"a{i}", bad)
    for i in range(3):
        _signal(db, f"b{i}", good, token="tok2")
    client = _TraderClient(fail={bad})

    stats = await recorder.run_traders_cycle(client, db, NOW, sleep=_noop)

    assert stats["traders_errors"] == 1
    assert stats["traders_rows"] == 1           # the second one survived
    assert db._conn.execute(
        "SELECT trader_id FROM traders"
    ).fetchone()["trader_id"] == good
    assert db._conn.execute(
        "SELECT COUNT(*) n FROM traders_fetch_state WHERE trader_id=?", (bad,)
    ).fetchone()["n"] == 0


async def test_traders_are_chunked_at_the_upstream_batch_cap(db, monkeypatch):
    """The cap of 100 is declared by the source itself, so what exceeds it is
    chunked, not truncated."""
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    monkeypatch.setattr(config, "TRADERS_BATCH_MAX", 2)
    for t in range(5):
        for i in range(3):
            _signal(db, f"s{t}-{i}", _uuid(f"t{t}"), token=f"tok{t}")
    client = _TraderClient()

    stats = await recorder.run_traders_cycle(client, db, NOW, sleep=_noop)

    assert [len(c) for c in client.calls] == [2, 2, 1]   # 5 over batches of 2
    assert sorted(i for c in client.calls for i in c) == sorted(
        _uuid(f"t{t}") for t in range(5)
    )
    assert stats["traders_rows"] == 5


async def test_traders_not_refetched_before_refresh(db, monkeypatch):
    """The profile changes over days — we do not waste a call every minute."""
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    for i in range(3):
        _signal(db, f"s{i}", _uuid("u1"))

    await recorder.run_traders_cycle(_TraderClient(), db, NOW, sleep=_noop)
    client2 = _TraderClient()
    await recorder.run_traders_cycle(client2, db, NOW, sleep=_noop)

    assert client2.calls == []


# ---------------------------------------------------------------------------
# Flow features (the point-in-time law)
# ---------------------------------------------------------------------------
def _insert_flow(db, ts, **over):
    row = {c: None for c in __import__("db")._FLOW_COLUMNS}
    row.update({
        "token_address": "tok", "network_id": "56", "recorded_at": ts,
        "watch_first_seen_at": NOW, "entry_signal_id": None, "is_control": 0,
        "buy_volume_5m": 300.0, "sell_volume_5m": 100.0,
        "buy_count_5m": 10, "sell_count_5m": 5,
        "unique_buys_5m": 4, "unique_sells_5m": 3,
        "raw_json": "{}",
    })
    row.update(over)
    db.insert_flow(row)


def test_flow_features_derive_ratios(db):
    _insert_flow(db, "2026-08-10T11:55:00+00:00")
    f = features.flow_features(db, "tok", "56", NOW_TS)

    assert f["flow_net_volume_5m"] == 200.0
    assert f["flow_buy_sell_volume_ratio_5m"] == 3.0
    assert f["flow_buy_sell_count_ratio_5m"] == 2.0
    assert f["flow_unique_ratio_5m"] == 0.4      # 4 uniques ÷ 10 trades
    assert f["flow_trade_size_5m"] == 30.0       # $300 ÷ 10 trades
    assert f["flow_age_min"] == pytest.approx(5.0)


def test_flow_features_absent_stays_null(db):
    """No row: everything None — "not measured", not "zero" (FR-007)."""
    f = features.flow_features(db, "tok", "56", NOW_TS)
    assert set(f.values()) == {None}
    assert "flow_net_volume_5m" in f


def test_flow_features_missing_side_is_not_zero(db):
    """An unknown sell is not a zero sell — the net stays None instead of
    equaling the buy."""
    _insert_flow(db, "2026-08-10T11:55:00+00:00", sell_volume_5m=None)
    f = features.flow_features(db, "tok", "56", NOW_TS)

    assert f["flow_buy_volume_5m"] == 300.0
    assert f["flow_net_volume_5m"] is None
    assert f["flow_buy_sell_volume_ratio_5m"] is None


def test_flow_features_ignore_future_rows(db):
    """The point-in-time law: what comes after t0 is not seen no matter how
    fresh it is."""
    _insert_flow(db, "2026-08-10T11:55:00+00:00", buy_volume_5m=300.0)
    before = features.flow_features(db, "tok", "56", NOW_TS)
    _insert_flow(db, "2026-08-10T12:30:00+00:00", buy_volume_5m=999.0)
    after = features.flow_features(db, "tok", "56", NOW_TS)

    assert before == after
    assert after["flow_buy_volume_5m"] == 300.0


def test_flow_features_zero_denominator_is_null_not_inf(db):
    """Division by zero ⇒ None, not inf — and a zero in the numerator stays a
    measurement."""
    _insert_flow(db, "2026-08-10T11:55:00+00:00",
                 buy_volume_5m=0.0, sell_volume_5m=0.0, buy_count_5m=0)
    f = features.flow_features(db, "tok", "56", NOW_TS)

    assert f["flow_buy_volume_5m"] == 0.0         # a measured zero ≠ absent
    assert f["flow_net_volume_5m"] == 0.0
    assert f["flow_buy_sell_volume_ratio_5m"] is None
    assert f["flow_trade_size_5m"] is None


def test_flow_columns_registered_in_feature_columns():
    """A column computed but never registered = work silently lost."""
    empty = RecorderDB(":memory:", SCHEMA)
    try:
        f_keys = set(features.flow_features(empty, "x", "1", NOW_TS))
        h_keys = set(features.holders_features(empty, "x", "1", NOW_TS))
    finally:
        empty.close()
    assert f_keys <= set(features.FEATURE_COLUMNS)
    assert h_keys <= set(features.FEATURE_COLUMNS)
    assert "tick_rich_age_min" in features.FEATURE_COLUMNS


# ---------------------------------------------------------------------------
# On-chain holder change over an hour — from two snapshots of ours, because
# the source gives no delta
# ---------------------------------------------------------------------------
def _holders(db, at, count, *, source="token_details", top10=42.0):
    db.insert_holders({
        "token_address": "tok", "network_id": "56", "recorded_at": at,
        "watch_first_seen_at": NOW, "entry_signal_id": "sig", "is_control": 0,
        "source": source, "top10_pct": top10, "holder_count": count,
        "raw_json": "{}",
    })


def test_holders_delta_measures_chain_not_platform(db):
    """The cadence is 25m ⇒ a prior at 1h+ sits 75m away, and the span is an
    explicit column, not an assumption."""
    _holders(db, "2026-08-10T10:45:00+00:00", 900)
    _holders(db, "2026-08-10T12:00:00+00:00", 1000)      # +100 over 75m
    f = features.holders_features(db, "tok", "56", NOW_TS)

    assert f["chain_holder_count"] == 1000
    assert f["chain_holders_delta_1h"] == 100
    assert f["chain_holders_growth_1h"] == pytest.approx(100 / 900)
    assert f["chain_holders_span_min"] == pytest.approx(75.0)


def test_holders_delta_absent_prior_stays_null(db):
    """One snapshot: the delta is None, not zero — "not measured" is not
    "did not change" (FR-007)."""
    _holders(db, "2026-08-10T12:00:00+00:00", 1000)
    f = features.holders_features(db, "tok", "56", NOW_TS)

    assert f["chain_holder_count"] == 1000
    assert f["chain_holders_delta_1h"] is None
    assert f["chain_holders_growth_1h"] is None
    assert f["chain_holders_span_min"] is None


def test_holders_delta_rejects_stale_prior(db):
    """Beyond 100m the snapshot is from another era: the measured tail
    stretches to 4,834m."""
    _holders(db, "2026-08-10T06:00:00+00:00", 500)        # 360m earlier
    _holders(db, "2026-08-10T12:00:00+00:00", 1000)
    f = features.holders_features(db, "tok", "56", NOW_TS)

    assert f["chain_holders_span_min"] is None
    assert f["chain_holders_delta_1h"] is None


def test_holders_delta_zero_change_is_measured(db):
    """A measured zero ≠ absent: in 15.1% of pairs the count truly does not
    change."""
    _holders(db, "2026-08-10T10:45:00+00:00", 900)
    _holders(db, "2026-08-10T12:00:00+00:00", 900)
    f = features.holders_features(db, "tok", "56", NOW_TS)

    assert f["chain_holders_delta_1h"] == 0
    assert f["chain_holders_growth_1h"] == 0.0


def test_holders_delta_ignores_future_and_platform_rows(db):
    """The point-in-time law + the source: hodlers_top must not pollute the
    chain count."""
    _holders(db, "2026-08-10T10:45:00+00:00", 900)
    _holders(db, "2026-08-10T12:00:00+00:00", 1000)
    before = features.holders_features(db, "tok", "56", NOW_TS)
    _holders(db, "2026-08-10T12:30:00+00:00", 9999)                    # after t0
    _holders(db, "2026-08-10T11:50:00+00:00", 7, source="hodlers_top")  # platform
    after = features.holders_features(db, "tok", "56", NOW_TS)

    assert before["chain_holders_delta_1h"] == after["chain_holders_delta_1h"] == 100
    assert after["chain_holder_count"] == 1000


# ---------------------------------------------------------------------------
# The shutout guard: a repeated collective zero can only be an outage
# ---------------------------------------------------------------------------
async def test_a_whole_source_shutout_stops_being_silent(db, monkeypatch):
    """Asked about 3, no row came down, three cycles in a row ⇒ an error line
    the dashboard can see.

    This is the lesson of 21 hours without a word: `traders_rows: 0` and
    `errors: 0` on every log line, because "no user with this id" is a
    legitimate answer for a single item.
    """
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    monkeypatch.setattr(config, "TRADERS_REFRESH_SECONDS", 0)
    monkeypatch.setattr(config, "SHUTOUT_STREAK_ALERT", 3)
    for t in range(3):
        for i in range(3):
            _signal(db, f"s{t}-{i}", _uuid(f"t{t}"), token=f"tok{t}")
    gone = {_uuid(f"t{t}") for t in range(3)}

    for cycle in range(2):
        at = f"2026-08-11T1{cycle}:00:00+00:00"
        await recorder.run_traders_cycle(_TraderClient(missing=gone), db, at, sleep=_noop)
        assert db.get_meta("shutout_traders") == str(cycle + 1)
        assert db.get_meta("last_error_traders") is None   # once and twice: silence

    await recorder.run_traders_cycle(
        _TraderClient(missing=gone), db, "2026-08-11T12:00:00+00:00", sleep=_noop
    )

    note = db.get_meta("last_error_traders")
    assert note is not None and "ShutoutSuspected" in note
    assert "3" in note
    # And no success stamp: otherwise the dashboard would declare the error
    # recovered a minute later.
    assert db.get_meta("traders_last_ok_at") is None


async def test_one_row_ends_the_shutout_streak(db, monkeypatch):
    """A single zero is ordinary: the first row that comes down resets the
    counter and stamps the success."""
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    monkeypatch.setattr(config, "TRADERS_REFRESH_SECONDS", 0)
    tid = _uuid("t0")
    for i in range(3):
        _signal(db, f"s{i}", tid)

    await recorder.run_traders_cycle(
        _TraderClient(missing={tid}), db, "2026-08-11T10:00:00+00:00", sleep=_noop
    )
    assert db.get_meta("shutout_traders") == "1"

    later = "2026-08-11T11:00:00+00:00"
    await recorder.run_traders_cycle(_TraderClient(), db, later, sleep=_noop)

    assert db.get_meta("shutout_traders") == "0"
    assert db.get_meta("traders_last_ok_at") == later


async def test_the_shutout_streak_survives_a_restart(db, monkeypatch):
    """The counter lives in `meta`, not memory: a restart does not bring back
    the silence.

    Were it in memory it would start from zero at every boot — and the
    recorder gets restarted.
    """
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    monkeypatch.setattr(config, "TRADERS_REFRESH_SECONDS", 0)
    monkeypatch.setattr(config, "SHUTOUT_STREAK_ALERT", 2)
    tid = _uuid("t0")
    for i in range(3):
        _signal(db, f"s{i}", tid)
    db.set_meta("shutout_traders", "1")          # a streak inherited from a previous process

    await recorder.run_traders_cycle(
        _TraderClient(missing={tid}), db, "2026-08-11T10:00:00+00:00", sleep=_noop
    )

    assert db.get_meta("shutout_traders") == "2"
    assert "ShutoutSuspected" in (db.get_meta("last_error_traders") or "")


async def test_a_quiet_queue_is_stamped_ok_not_accused(db, monkeypatch):
    """Nobody deserved fetching ⇒ success, not a shutout: nothing was asked,
    so nothing failed.

    Without a stamp here, a quiet queue stays red forever on an error long
    healed — the very defect that turned the chain badges red on 2026-08-17.
    """
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    at = "2026-08-11T10:00:00+00:00"

    stats = await recorder.run_traders_cycle(_TraderClient(), db, at, sleep=_noop)

    assert stats["traders_fetched"] == 0
    assert db.get_meta("shutout_traders") in (None, "0")
    assert db.get_meta("traders_last_ok_at") == at


async def test_a_failing_batch_is_not_counted_as_a_shutout(db, monkeypatch):
    """An error writes its own line; the guard is for silence alone, so it is
    not charged for errors."""
    monkeypatch.setattr(config, "TRADERS_PER_CYCLE", 10)
    monkeypatch.setattr(config, "SHUTOUT_STREAK_ALERT", 1)
    tid = _uuid("bad")
    for i in range(3):
        _signal(db, f"s{i}", tid)

    await recorder.run_traders_cycle(
        _TraderClient(fail={tid}), db, "2026-08-11T10:00:00+00:00", sleep=_noop
    )

    assert db.get_meta("shutout_traders") is None
    assert db.get_meta("traders_last_ok_at") is None
    assert "ShutoutSuspected" not in (db.get_meta("last_error_traders") or "")
    assert "upstream boom" in (db.get_meta("last_error_traders") or "")
