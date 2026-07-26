"""End-to-end coverage for the alert ingestion pipeline.

Regression context: every piece of this pipeline existed but nothing connected
them. AlertPoller was never instantiated, so no upstream poll ever ran and
/v1/alerts/stream emitted heartbeats forever. FakeRedis — which the app falls
back to whenever Redis is unreachable, i.e. the actual deployment — had no
`set`, no `scan_iter` and a pub/sub that dropped every message, so subscriptions
resolved to [] and published alerts went nowhere. These tests pin each link.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from fomo_api.realtime.poller import AlertPoller
from fomo_api.realtime.pubsub import ALERT_CHANNEL, AlertPubSub
from fomo_api.redis_state import get_redis
from fomo_api.services.alert_subscription_service import AlertSubscriptionService

_FEED_PATH = "/feed/tradingActivity"


def _feed(*items) -> dict:
    return {"success": True, "responseObject": {"feed": list(items)}, "statusCode": 200}


def _event(eid: str, trader: str, ts: str) -> dict:
    return {
        "id": eid,
        "userId": trader,
        "createdAt": ts,
        "networkId": 56,
        "tokenAddress": "0xtok",
        "type": "large_buy",
    }


# --- FakeRedis must cover everything the app calls ------------------------


async def test_fake_redis_supports_set_and_scan_iter(fake_redis):
    await fake_redis.set("fomo:sub:consumer:ck:s1", "1")
    await fake_redis.setex("fomo:sub:consumer:ck:s2", 60, "1")
    await fake_redis.setex("unrelated", 60, "1")

    found = [k async for k in fake_redis.scan_iter(match="fomo:sub:consumer:ck:*")]
    assert sorted(found) == ["fomo:sub:consumer:ck:s1", "fomo:sub:consumer:ck:s2"]


async def test_fake_redis_pubsub_delivers_published_messages(fake_redis):
    conn = fake_redis.pubsub()
    await conn.subscribe(ALERT_CHANNEL)

    assert await fake_redis.publish(ALERT_CHANNEL, json.dumps({"id": "a1"})) == 1
    msg = await conn.get_message(timeout=1)
    assert msg is not None
    assert json.loads(msg["data"]) == {"id": "a1"}

    # Nothing pending -> None within the timeout (mirrors redis-py).
    assert await conn.get_message(timeout=0.05) is None
    await conn.close()


async def test_fake_redis_pubsub_ignores_other_channels(fake_redis):
    conn = fake_redis.pubsub()
    await conn.subscribe(ALERT_CHANNEL)
    assert await fake_redis.publish("some:other:channel", "{}") == 0
    assert await conn.get_message(timeout=0.05) is None
    await conn.close()


# --- subscriptions resolve back to trader ids -----------------------------


async def test_list_for_consumer_returns_created_subscriptions(fake_redis):
    svc = AlertSubscriptionService(fake_redis)
    created = await svc.create("consumer-1", ["t_1", "t_2"])

    ids = await svc.list_for_consumer("consumer-1")
    assert ids == [created["subscription_id"]]
    assert await svc.list_for_consumer("someone-else") == []


async def test_all_tracked_trader_ids_unions_across_consumers(fake_redis):
    svc = AlertSubscriptionService(fake_redis)
    await svc.create("consumer-1", ["t_1", "t_2"])
    await svc.create("consumer-2", ["t_2", "t_3"])

    assert await svc.all_tracked_trader_ids() == {"t_1", "t_2", "t_3"}


# --- the poller actually fetches, filters and publishes -------------------


async def test_poller_publishes_new_alerts_and_advances_watermark(
    fake_redis, respx_mock, fomo_json
):
    respx_mock.get(_FEED_PATH).mock(
        return_value=fomo_json(
            _feed(
                _event("a1", "t_1", "2026-07-26T10:00:00Z"),
                _event("a2", "t_2", "2026-07-26T10:05:00Z"),  # not ours
            )
        )
    )
    poller = AlertPoller(AlertPubSub(fake_redis), lambda _tid: _token())

    published = await poller.poll_once("t_1")

    assert [a["id"] for a in published] == ["a1"]
    assert await fake_redis.get("fomo:watermark:t_1") == "2026-07-26T10:00:00Z"


async def test_poller_does_not_republish_already_seen_alerts(
    fake_redis, respx_mock, fomo_json
):
    respx_mock.get(_FEED_PATH).mock(
        return_value=fomo_json(_feed(_event("a1", "t_1", "2026-07-26T10:00:00Z")))
    )
    poller = AlertPoller(AlertPubSub(fake_redis), lambda _tid: _token())

    assert len(await poller.poll_once("t_1")) == 1
    assert await poller.poll_once("t_1") == []  # watermark suppresses the repeat


async def test_poller_refreshes_tracked_traders_from_subscriptions(fake_redis):
    svc = AlertSubscriptionService(fake_redis)
    poller = AlertPoller(
        AlertPubSub(fake_redis),
        lambda _tid: _token(),
        tracked_source=svc.all_tracked_trader_ids,
    )
    assert poller.tracked == set()

    await svc.create("consumer-1", ["t_7"])
    await poller._refresh_tracked()
    assert poller.tracked == {"t_7"}


async def test_poller_keeps_previous_tracked_set_when_source_fails(fake_redis):
    async def _boom() -> set[str]:
        raise RuntimeError("redis down")

    poller = AlertPoller(AlertPubSub(fake_redis), lambda _tid: _token(), tracked_source=_boom)
    poller.track("t_9")
    await poller._refresh_tracked()
    assert poller.tracked == {"t_9"}


# --- published alerts reach an SSE consumer -------------------------------


async def test_published_alert_reaches_the_sse_stream(fake_redis):
    from fomo_api.realtime.sse import SSEManager

    pubsub = AlertPubSub(fake_redis)
    stream = SSEManager(pubsub).stream("sub-1", ["t_1"])

    assert (await stream.__anext__())["event"] == "subscribed"
    # Give the manager a beat to finish subscribing before publishing.
    await asyncio.sleep(0)
    await pubsub.publish({"id": "a1", "trader_id": "t_1"})

    event = await asyncio.wait_for(stream.__anext__(), timeout=2)
    assert event["event"] == "alert"
    assert json.loads(event["data"])["id"] == "a1"
    await stream.aclose()


async def _token() -> str:
    return "fomo-session-token"


@pytest.fixture(autouse=True)
def _redis_is_fake(fake_redis):
    """Guard: these tests are meaningless if get_redis() isn't the fake."""
    assert get_redis() is fake_redis
