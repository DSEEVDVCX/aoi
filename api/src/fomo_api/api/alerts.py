from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from fomo_api.auth.deps import require_consumer_key
from fomo_api.realtime.pubsub import AlertPubSub
from fomo_api.realtime.sse import SSEManager
from fomo_api.services.alert_subscription_service import AlertSubscriptionService

router = APIRouter()

_TRADER_ID_PATTERN = r"^[A-Za-z0-9_-]+$"


class SubscriptionRequest(BaseModel):
    trader_ids: list[str] = Field(default_factory=list)


class SubscriptionResponse(BaseModel):
    subscription_id: str
    trader_ids: list[str]


def _subscription_service() -> AlertSubscriptionService:
    return AlertSubscriptionService()


def _validate_trader_ids(trader_ids: list[str]) -> list[str]:
    import re

    for tid in trader_ids:
        if not re.match(_TRADER_ID_PATTERN, tid):
            from fomo_api.api.errors import VALIDATION_ERROR, ApiError

            raise ApiError(VALIDATION_ERROR, f"Invalid trader_id: {tid}", {"trader_id": tid})
    return trader_ids


@router.post("/alerts/subscriptions", response_model=SubscriptionResponse)
async def create_subscription(
    req: SubscriptionRequest,
    consumer_key: str = Depends(require_consumer_key),
) -> SubscriptionResponse:
    trader_ids = _validate_trader_ids(req.trader_ids)
    svc = _subscription_service()
    result = await svc.create(consumer_key, trader_ids)
    return SubscriptionResponse(**result)


@router.delete("/alerts/subscriptions/{subscription_id}", status_code=204, response_class=Response)
async def delete_subscription(
    subscription_id: str,
    consumer_key: str = Depends(require_consumer_key),
) -> Response:
    svc = _subscription_service()
    existed = await svc.delete(subscription_id, consumer_key)
    if not existed:
        from fomo_api.api.errors import NotFoundError

        raise NotFoundError("Subscription", subscription_id)
    return Response(status_code=204)


@router.get("/alerts/stream")
async def alert_stream(
    consumer_key: str = Depends(require_consumer_key),
) -> EventSourceResponse:
    svc = _subscription_service()
    subscription_ids = await svc.list_for_consumer(consumer_key)
    tracked: list[str] = []
    for sid in subscription_ids:
        sub = await svc.get(sid)
        if sub:
            tracked.extend(sub.get("trader_ids", []))
    tracked = list(dict.fromkeys(tracked))
    pubsub = AlertPubSub()
    manager = SSEManager(pubsub)
    return EventSourceResponse(manager.stream(consumer_key, tracked))
