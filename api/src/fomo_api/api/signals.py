from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from fomo_api.clients.deps import FomoClientDep
from fomo_api.models.envelope import Envelope, utcnow

router = APIRouter()


@router.get("/signals/top-tokens")
async def get_top_tokens(
    client: FomoClientDep,
    chain: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> Envelope[list[dict]]:
    """CONFIRMED: POST /proxy/trendingTokens — trending tokens (chain filtered
    client-side). Replaces the former phantom /tokens/trending path."""
    tokens = await client.get_top_tokens(chain=chain, limit=limit)
    return Envelope(data=tokens, last_refreshed_at=utcnow())


@router.get("/traders/{trader_id}/positions")
async def get_trader_positions(
    client: FomoClientDep,
    trader_id: str,
) -> Envelope[list[dict]]:
    """CONFIRMED: /v2/users/{id}/balances — a trader's current holdings.
    Replaces the former phantom /profile/{id}/positions path."""
    positions = await client.get_trader_positions(trader_id)
    return Envelope(data=positions, last_refreshed_at=utcnow())


# NOTE: /traders/{id}/stats was removed — the former /profile/{id}/stats path
# never existed upstream and fomo.family exposes no direct per-trader stats
# endpoint. PnL/volume come from the profile + aggregatedSnapshot instead.


@router.get("/feed/token/thesis")
async def get_token_thesis_feed(
    client: FomoClientDep,
    token_address: str = Query(alias="tokenAddress"),
    network_id: int = Query(alias="networkId"),
    last_id: str | None = Query(default=None, alias="lastId"),
    threshold: int = Query(default=1000, ge=0),
) -> Envelope[list[dict[str, Any]]]:
    """CONFIRMED endpoint: /feed/token/thesis — thesis/signal feed for a token."""
    items = await client.get_token_thesis_feed(
        token_address=token_address,
        network_id=network_id,
        last_id=last_id,
        threshold=threshold,
    )
    return Envelope(data=items, last_refreshed_at=utcnow())


@router.post("/proxy/filterTokens")
async def filter_tokens(
    client: FomoClientDep,
    filters: list[str],
) -> Envelope[list[dict[str, Any]]]:
    """CONFIRMED endpoint: /proxy/filterTokens — body is a JSON array of token addresses."""
    tokens = await client.filter_tokens(filters)
    return Envelope(data=tokens, last_refreshed_at=utcnow())


@router.get("/hodlers/top")
async def get_top_hodlers(
    client: FomoClientDep,
    tokens: str = Query(description='JSON array e.g. [{"address":"0x...","networkId":1399811149}]'),
) -> Envelope[list[dict[str, Any]]]:
    """CONFIRMED endpoint: /hodlers/top — top holders for given tokens."""
    import json

    try:
        tokens_list = json.loads(tokens)
    except Exception as exc:
        from fomo_api.api.errors import VALIDATION_ERROR, ApiError
        raise ApiError(VALIDATION_ERROR, "tokens must be a valid JSON array", {}) from exc
    hodlers = await client.get_top_hodlers(tokens_list)
    return Envelope(data=hodlers, last_refreshed_at=utcnow())


@router.get("/signals/trending")
async def get_trending_tokens(client: FomoClientDep) -> Envelope[list[dict[str, Any]]]:
    """CONFIRMED: POST /proxy/trendingTokens — trending tokens with market data."""
    tokens = await client.get_trending_tokens()
    return Envelope(data=tokens, last_refreshed_at=utcnow())


@router.get("/signals/most-held")
async def get_most_held(client: FomoClientDep) -> Envelope[list[dict[str, Any]]]:
    """CONFIRMED: POST /proxy/mostHeld — most-held tokens with market data."""
    tokens = await client.get_most_held()
    return Envelope(data=tokens, last_refreshed_at=utcnow())


@router.get("/signals/verified")
async def get_verified_tokens(client: FomoClientDep) -> Envelope[list[dict[str, Any]]]:
    """CONFIRMED: GET /proxy/verifiedTokens — verified tokens with market data."""
    tokens = await client.get_verified_tokens()
    return Envelope(data=tokens, last_refreshed_at=utcnow())


@router.post("/hodlers/friends")
async def get_hodler_friends(
    client: FomoClientDep,
    tokens: list[dict[str, Any]],
) -> Envelope[list[dict[str, Any]]]:
    """CONFIRMED: POST /hodlers/friends — friends holding the given tokens.
    Body is a JSON array like [{"tokenAddress":"0x...","networkId":56}]."""
    friends = await client.get_hodler_friends(tokens)
    return Envelope(data=friends, last_refreshed_at=utcnow())
