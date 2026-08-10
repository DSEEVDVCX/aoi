"""اختبارات دورة الحائزين ومستخرجيها (بلا شبكة)."""
import os

import pytest

import config
import extract
import recorder
from db import RecorderDB, decode_raw

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-08-09T12:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _watch(db, token, *, control=False, network="56"):
    db.upsert_watch(token, network, "large_buy", f"sig-{token}", 48, NOW)
    if control:
        db._conn.execute(
            "UPDATE watchlist SET is_control=1, entry_signal_id=NULL WHERE token_address=?",
            (token,),
        )
        db._conn.commit()


def _token_details_envelope(*, top10=None, holders=None):
    """يحاكي POST /proxy/tokenDetails — responseObject مباشر بلا قائمة."""
    ro = {}
    if top10 is not None:
        ro["top10HoldersPercent"] = top10
    if holders is not None:
        ro["holders"] = holders
    return {"responseObject": ro}


def _hodlers_top_envelope(*, total=None, positions=None):
    """يحاكي GET /hodlers/top — responseObject قائمة عناصر لكل عملة."""
    if positions is None:
        positions = []
    entry = {"totalHolders": total, "topHolders": positions}
    return {"responseObject": [entry]}


# ---------------------------------------------------------------------------
# مستخرِج tokenDetails
# ---------------------------------------------------------------------------
def test_extract_token_details_preserves_both_metrics():
    row = extract.extract_token_details_holders(
        _token_details_envelope(top10=83.24, holders=947),
        "tok", "56", NOW, NOW, "sig-1", is_control=0,
    )
    assert row is not None
    assert row["source"] == "token_details"
    assert row["top10_pct"] == 83.24
    assert row["holder_count"] == 947
    assert row["platform_holders"] is None
    assert row["platform_value_usd"] is None


def test_extract_token_details_rejects_empty_envelope():
    assert extract.extract_token_details_holders(
        {}, "tok", "56", NOW, NOW, None
    ) is None
    assert extract.extract_token_details_holders(
        {"responseObject": {}}, "tok", "56", NOW, NOW, None
    ) is None


def test_extract_token_details_accepts_partial_data():
    """واحد من الاثنين موجود → صفّ صالح (FR-007: الغائب يبقى NULL)."""
    row_top10_only = extract.extract_token_details_holders(
        _token_details_envelope(top10=21.9),
        "tok", "56", NOW, NOW, None,
    )
    assert row_top10_only is not None
    assert row_top10_only["top10_pct"] == 21.9
    assert row_top10_only["holder_count"] is None

    row_count_only = extract.extract_token_details_holders(
        _token_details_envelope(holders=14371),
        "tok", "56", NOW, NOW, None,
    )
    assert row_count_only is not None
    assert row_count_only["top10_pct"] is None
    assert row_count_only["holder_count"] == 14371


# ---------------------------------------------------------------------------
# مستخرِج hodlers/top
# ---------------------------------------------------------------------------
def test_extract_platform_holders_computes_aggregates():
    positions = [
        {"value": 100.0, "costBasis": 120.0, "unrealizedPnl": -20.0,
         "averageHoldTimeSeconds": 24000, "isDev": False,
         "user": {"id": "u1", "userHandle": "alice"}},
        {"value": 50.0, "costBasis": 40.0, "unrealizedPnl": 10.0,
         "averageHoldTimeSeconds": 31000, "isDev": False,
         "user": {"id": "u2", "userHandle": "bob"}},
        {"value": 80.0, "costBasis": 85.0, "unrealizedPnl": -5.0,
         "averageHoldTimeSeconds": 18000, "isDev": True,
         "user": {"id": "u3", "userHandle": "dev"}},
    ]
    row = extract.extract_platform_holders(
        _hodlers_top_envelope(total=274, positions=positions),
        "tok", "56", NOW, NOW, "sig-1",
    )
    assert row is not None
    assert row["source"] == "hodlers_top"
    assert row["top10_pct"] is None
    assert row["holder_count"] is None
    assert row["platform_holders"] == 274
    assert row["platform_holders_listed"] == 3
    assert row["platform_value_usd"] == 230.0
    assert row["platform_underwater"] == 2
    assert row["platform_median_hold_seconds"] == 24000
    assert row["platform_dev_holding"] == 1


def test_extract_platform_holders_handles_missing_unrealized():
    """الربح غير المحقّق غائب في بعض المراكز → التقييم على القائمة فقط."""
    positions = [
        {"value": 50.0, "unrealizedPnl": -10.0},
        {"value": 60.0},
        {"value": 70.0, "unrealizedPnl": 5.0},
    ]
    row = extract.extract_platform_holders(
        _hodlers_top_envelope(total=100, positions=positions),
        "tok", "56", NOW, NOW, None,
    )
    assert row["platform_underwater"] == 1
    assert row["platform_holders_listed"] == 3


def test_extract_platform_holders_rejects_empty_list():
    assert extract.extract_platform_holders(
        {"responseObject": []}, "tok", "56", NOW, NOW, None
    ) is None


def test_extract_platform_holders_accepts_total_only():
    """حشد موجود بلا تفصيل → صفّ بعدد كلّي (FR-007)."""
    row = extract.extract_platform_holders(
        _hodlers_top_envelope(total=118),
        "tok", "56", NOW, NOW, None,
    )
    assert row is not None
    assert row["platform_holders"] == 118
    assert row["platform_holders_listed"] is None
    assert row["platform_value_usd"] is None


# ---------------------------------------------------------------------------
# دورة run_holders_cycle
# ---------------------------------------------------------------------------
class _HoldersClient:
    """عميل مزيّف. الفشل **لكل مصدر** لأنّ عزل المصدرين هو ما نختبره."""

    def __init__(self, details_replies=None, top_replies=None,
                 fail_details=None, fail_top=None):
        self.calls = []
        self._details = details_replies or {}
        self._top = top_replies or {}
        self._fail_details = fail_details or set()
        self._fail_top = fail_top or set()

    async def _post(self, path, body):
        self.calls.append(("POST", path, body))
        addr = body.get("tokenId", "").split(":")[0]
        if addr in self._fail_details:
            raise RuntimeError("upstream boom")
        return self._details.get(addr, _token_details_envelope())

    async def _get(self, path, params):
        self.calls.append(("GET", path, params))
        import json
        tokens = json.loads(params["tokens"])
        addr = tokens[0]["address"] if tokens else ""
        if addr in self._fail_top:
            raise RuntimeError("upstream boom")
        return self._top.get(addr, _hodlers_top_envelope())


async def _noop(_seconds):
    pass


async def test_holders_cycle_writes_two_rows_per_token(db):
    _watch(db, "rich")
    _watch(db, "crowd", network="4663")
    client = _HoldersClient(
        details_replies={
            "rich": _token_details_envelope(top10=83.24, holders=947),
            "crowd": _token_details_envelope(top10=21.9, holders=14371),
        },
        top_replies={
            "rich": _hodlers_top_envelope(total=276, positions=[
                {"value": 100.0, "unrealizedPnl": -20.0, "averageHoldTimeSeconds": 24000}
            ]),
            "crowd": _hodlers_top_envelope(total=118),
        },
    )

    stats = await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    assert stats == {
        "holders_tokens": 2, "holders_details": 2,
        "holders_top": 2, "holders_errors": 0,
    }
    rows = db._conn.execute(
        "SELECT token_address, source, top10_pct, platform_holders "
        "FROM token_holders ORDER BY token_address, source"
    ).fetchall()
    assert len(rows) == 4
    assert rows[0]["token_address"] == "crowd"
    assert rows[0]["source"] == "hodlers_top"
    assert rows[0]["platform_holders"] == 118
    assert rows[1]["source"] == "token_details"
    assert rows[1]["top10_pct"] == 21.9


async def test_details_failure_does_not_block_platform_source(db):
    """سقوط مصدر لا يُسقط الثاني — التركّز يغيب والحشد يُسجّل."""
    _watch(db, "partial")
    client = _HoldersClient(
        top_replies={"partial": _hodlers_top_envelope(total=61)},
        fail_details={"partial"},
    )

    stats = await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    assert stats["holders_errors"] == 1
    assert stats["holders_details"] == 0
    assert stats["holders_top"] == 1
    assert stats["holders_tokens"] == 1
    rows = db._conn.execute("SELECT source FROM token_holders").fetchall()
    assert [r["source"] for r in rows] == ["hodlers_top"]
    err = db.get_meta("last_error_holders")
    assert err is not None and "token_details" in err


async def test_both_sources_failing_marks_state_error(db):
    _watch(db, "dead")
    client = _HoldersClient(fail_details={"dead"}, fail_top={"dead"})

    stats = await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    assert stats["holders_errors"] == 2
    assert stats["holders_tokens"] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM token_holders").fetchone()[0] == 0
    state = db._conn.execute(
        "SELECT last_status FROM holders_fetch_state WHERE token_address='dead'"
    ).fetchone()
    assert state["last_status"] == "error"


async def test_holders_cycle_archives_raw_envelopes(db):
    """الخام مضغوط في العمود — الأرشيف يسمح بإعادة الاستخراج بلا شبكة."""
    _watch(db, "tok")
    client = _HoldersClient(
        details_replies={"tok": _token_details_envelope(top10=24.29, holders=500)},
        top_replies={"tok": _hodlers_top_envelope(total=42, positions=[
            {"value": 10.0, "unrealizedPnl": -1.0, "isDev": True}
        ])},
    )

    await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    rows = {
        r["source"]: decode_raw(r["raw_json"])
        for r in db._conn.execute("SELECT source, raw_json FROM token_holders")
    }
    assert rows["token_details"]["responseObject"]["top10HoldersPercent"] == 24.29
    assert rows["hodlers_top"]["responseObject"][0]["totalHolders"] == 42


async def test_holders_refresh_and_error_retry_windows(db, monkeypatch):
    from datetime import datetime, timedelta

    _watch(db, "tok")
    client = _HoldersClient()
    await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)
    calls_after_first = len(client.calls)
    assert calls_after_first == 2               # مصدران لكل عملة

    soon = (datetime.fromisoformat(NOW) + timedelta(seconds=60)).isoformat()
    await recorder.run_holders_cycle(client, db, soon, sleep=_noop)
    assert len(client.calls) == calls_after_first    # ما زال طازجاً

    later = (
        datetime.fromisoformat(NOW)
        + timedelta(seconds=config.HOLDERS_REFRESH_SECONDS + 1)
    ).isoformat()
    await recorder.run_holders_cycle(client, db, later, sleep=_noop)
    assert len(client.calls) == calls_after_first + 2


async def test_signal_token_precedes_control_when_capped(db, monkeypatch):
    _watch(db, "control", control=True)
    _watch(db, "signal")
    monkeypatch.setattr(config, "HOLDERS_PER_CYCLE", 1)
    client = _HoldersClient()

    await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    assert client.calls[0][2]["tokenId"].startswith("signal:")


async def test_token_id_carries_network_suffix(db):
    """`tokenId` بلا ":networkId" يجعل الخادم يرمي 502 مضلّلاً — نثبّت الشكل."""
    _watch(db, "tok", network="8453")
    client = _HoldersClient()

    await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    post = next(c for c in client.calls if c[0] == "POST")
    assert post[2]["tokenId"] == "tok:8453"
    get = next(c for c in client.calls if c[0] == "GET")
    import json
    assert json.loads(get[2]["tokens"]) == [{"address": "tok", "networkId": 8453}]


async def test_holders_state_tracks_top10_from_token_details(db):
    _watch(db, "tok")
    client = _HoldersClient(
        details_replies={"tok": _token_details_envelope(top10=38.42)},
    )

    await recorder.run_holders_cycle(client, db, NOW, sleep=_noop)

    state = db._conn.execute(
        "SELECT last_status, top10_pct FROM holders_fetch_state"
    ).fetchone()
    assert state["last_status"] == "ok"
    assert state["top10_pct"] == 38.42
