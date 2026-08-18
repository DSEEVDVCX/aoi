"""اختبارات حلقة الطبقة الاجتماعية (بلا شبكة)."""
import os
from datetime import datetime, timedelta

import config
import pytest
from db import RecorderDB

import recorder

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-07-27T12:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


class _SocialClient:
    def __init__(self, replies=None, fail_on=None):
        self.calls = []
        self._replies = replies or {}
        self._fail_on = fail_on or set()

    async def _get(self, path, params=None):
        self.calls.append(params)
        addr = (params or {}).get("tokenAddress")
        if addr in self._fail_on:
            raise RuntimeError("upstream boom")
        return self._replies.get(addr, _envelope(2))


def _envelope(n, likes=5):
    return {"responseObject": {"items": [
        {"id": f"t{i}", "userHandle": f"u{i}", "numReplies": 1, "equity": 10.0,
         "createdAt": "2026-07-27T11:00:00Z",
         "comment": {"comment": "gm", "numLikes": likes}}
        for i in range(n)
    ]}}


async def _noop(_s):
    pass


def _watch(db, tok):
    db.upsert_watch(tok, "56", "large_buy", "s1", 48, NOW)


async def test_social_cycle_writes_a_snapshot_per_token(db):
    _watch(db, "tokA")
    client = _SocialClient()

    stats = await recorder.run_social_cycle(client, db, NOW, sleep=_noop)

    assert stats["social_tokens"] == 1
    assert stats["social_items"] == 2
    row = db._conn.execute("SELECT * FROM token_social").fetchone()
    assert row["thesis_count"] == 2
    assert row["thesis_likes"] == 10
    assert row["thesis_authors"] == 2
    assert db.social_count("tokA", "56") == 1


async def test_social_request_carries_token_and_network(db):
    _watch(db, "tokA")
    client = _SocialClient()
    await recorder.run_social_cycle(client, db, NOW, sleep=_noop)
    assert client.calls[0]["tokenAddress"] == "tokA"
    assert client.calls[0]["networkId"] == 56          # رقميّ لا نصّيّ


async def test_silent_token_is_recorded_as_empty_not_skipped(db):
    """الصمت إشارة — نسجّل الصفر ولا نتخطّى العملة."""
    _watch(db, "tokA")
    client = _SocialClient(replies={"tokA": {"responseObject": {"items": []}}})

    await recorder.run_social_cycle(client, db, NOW, sleep=_noop)

    assert db.social_count("tokA", "56") == 1
    row = db._conn.execute("SELECT thesis_count FROM token_social").fetchone()
    assert row["thesis_count"] == 0
    st = db._conn.execute("SELECT last_status FROM social_fetch_state").fetchone()
    assert st["last_status"] == "empty"


async def test_silent_token_is_still_refetched_later(db):
    """بخلاف الشموع، الصمت المتكرّر لا يُستبعد: تغيّره هو المطلوب."""
    _watch(db, "tokA")
    client = _SocialClient(replies={"tokA": {"responseObject": {"items": []}}})
    for k in range(3):
        t = (datetime.fromisoformat(NOW)
             + timedelta(seconds=k * (config.SOCIAL_REFRESH_SECONDS + 60))).isoformat()
        await recorder.run_social_cycle(client, db, t, sleep=_noop)
    assert len(client.calls) == 3


async def test_one_failing_token_does_not_stop_the_slice(db):
    _watch(db, "tokA")
    _watch(db, "tokB")
    client = _SocialClient(fail_on={"tokA"})

    stats = await recorder.run_social_cycle(client, db, NOW, sleep=_noop)

    assert stats["social_errors"] == 1
    assert stats["social_tokens"] == 1
    states = {r["token_address"]: r["last_status"]
              for r in db._conn.execute("SELECT * FROM social_fetch_state")}
    assert states["tokA"] == "error" and states["tokB"] == "ok"


async def test_social_slice_is_capped_and_respects_refresh_window(db):
    for i in range(config.SOCIAL_PER_CYCLE + 3):
        _watch(db, f"tok{i}")
    client = _SocialClient()

    await recorder.run_social_cycle(client, db, NOW, sleep=_noop)
    assert len(client.calls) == config.SOCIAL_PER_CYCLE

    soon = (datetime.fromisoformat(NOW) + timedelta(seconds=60)).isoformat()
    await recorder.run_social_cycle(client, db, soon, sleep=_noop)
    # الأربعة الأولى ما تزال طازجة → تُختار الباقية فقط
    assert len(client.calls) == config.SOCIAL_PER_CYCLE + 3


def test_social_error_retry_window_is_shorter_than_success_window(db):
    _watch(db, "error-token")
    db.set_social_state("error-token", "56", "error", 0, "2026-08-17T11:54:00+00:00")

    due = db.social_fetch_due(
        limit=10,
        stale_before_iso="2026-08-17T11:30:00+00:00",
        error_stale_before_iso="2026-08-17T11:55:00+00:00",
    )

    assert [row["token_address"] for row in due] == ["error-token"]

async def test_social_builds_a_time_series_per_token(db):
    """سلسلة زمنية: الفرق بين لقطتين يعطي تسارع الزخم لا مستواه فقط."""
    _watch(db, "tokA")
    client = _SocialClient(replies={"tokA": _envelope(2)})
    await recorder.run_social_cycle(client, db, NOW, sleep=_noop)
    later = (datetime.fromisoformat(NOW)
             + timedelta(seconds=config.SOCIAL_REFRESH_SECONDS + 60)).isoformat()
    client._replies["tokA"] = _envelope(9)
    await recorder.run_social_cycle(client, db, later, sleep=_noop)

    counts = [r["thesis_count"] for r in db._conn.execute(
        "SELECT thesis_count FROM token_social ORDER BY recorded_at")]
    assert counts == [2, 9]
