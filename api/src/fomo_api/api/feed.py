from __future__ import annotations

from fastapi import APIRouter, Query

from fomo_api.clients.deps import FomoClientDep
from fomo_api.models.envelope import Envelope, utcnow
from fomo_api.models.feed import FeedPost, TradeComment

router = APIRouter()


@router.get("/feed/social", response_model=Envelope[list[FeedPost]])
async def get_social_feed(
    client: FomoClientDep,
    limit: int = Query(default=50, ge=1, le=200),
    last_id: str | None = Query(default=None, alias="lastId"),
    threshold: int | None = Query(default=None, ge=0),
    trader_id: str | None = Query(default=None, alias="traderId"),
) -> Envelope[list[FeedPost]]:
    """CONFIRMED: /feed/tradingActivity — the social feed with post body,
    likes and views (which the alert view drops)."""
    posts = await client.get_social_feed(
        limit=limit, last_id=last_id, threshold=threshold, trader_id=trader_id
    )
    return Envelope(data=[FeedPost(**p) for p in posts], last_refreshed_at=utcnow())


@router.get("/trades/{trade_id}/comments", response_model=Envelope[list[TradeComment]])
async def get_trade_comments(
    client: FomoClientDep,
    trade_id: str,
) -> Envelope[list[TradeComment]]:
    """CONFIRMED: /trades/{id}/comments — comments on a trade (may be empty)."""
    comments = await client.get_trade_comments(trade_id)
    return Envelope(data=[TradeComment(**c) for c in comments], last_refreshed_at=utcnow())


@router.get("/feed", response_model=Envelope[list[FeedPost]])
async def get_feed(
    client: FomoClientDep,
    limit: int = Query(default=50, ge=1, le=200),
    last_id: str | None = Query(default=None, alias="lastId"),
    feed_types: list[str] | None = Query(default=None, alias="feedTypes"),
) -> Envelope[list[FeedPost]]:
    """CONFIRMED: GET /feed — the global social feed. `feedTypes` defaults to the
    confirmed set (multi_user_buy, multi_user_sell, large_buy) when omitted."""
    posts = await client.get_feed(limit=limit, last_id=last_id, feed_types=feed_types)
    return Envelope(data=[FeedPost(**p) for p in posts], last_refreshed_at=utcnow())


@router.get("/feed/token", response_model=Envelope[list[FeedPost]])
async def get_token_feed(
    client: FomoClientDep,
    token_address: str = Query(alias="tokenAddress"),
    network_id: int = Query(alias="networkId"),
    limit: int = Query(default=50, ge=1, le=200),
    last_id: str | None = Query(default=None, alias="lastId"),
) -> Envelope[list[FeedPost]]:
    """CONFIRMED: GET /feed/token — the per-token social feed."""
    posts = await client.get_token_feed(
        token_address=token_address, network_id=network_id, limit=limit, last_id=last_id
    )
    return Envelope(data=[FeedPost(**p) for p in posts], last_refreshed_at=utcnow())
