from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from fomo_api.clients.deps import FomoClientDep
from fomo_api.config import settings
from fomo_api.models.activity import TraderActivity
from fomo_api.models.envelope import Envelope, make_page
from fomo_api.models.feed import Spotlight
from fomo_api.models.trader import Trader
from fomo_api.services.activity_service import ActivityService
from fomo_api.services.trader_service import TraderService

router = APIRouter()


# NOTE: static sub-routes ("/traders/by-handle/...") are declared before the
# "/traders/{trader_id}" catch-all so FastAPI matches them first.
@router.get("/traders/by-handle/{handle}", response_model=Envelope[Trader])
async def get_trader_by_handle(client: FomoClientDep, handle: str) -> Envelope[Trader]:
    service = TraderService(client)
    trader, refreshed_at = await service.require_profile_by_handle(handle)
    return Envelope(data=trader, last_refreshed_at=refreshed_at)


@router.get("/traders/{trader_id}/spotlight", response_model=Envelope[Spotlight])
async def get_trader_spotlight(client: FomoClientDep, trader_id: str) -> Envelope[Spotlight]:
    """CONFIRMED: /v2/users/{id}/spotlight — a trader's best trades and comments."""
    from fomo_api.api.errors import NotFoundError
    from fomo_api.models.envelope import utcnow

    data = await client.get_trader_spotlight(trader_id)
    if data is None:
        raise NotFoundError("Trader", trader_id)
    return Envelope(data=Spotlight(**data), last_refreshed_at=utcnow())


@router.get("/traders/{trader_id}", response_model=Envelope[Trader])
async def get_trader_profile(client: FomoClientDep, trader_id: str) -> Envelope[Trader]:
    service = TraderService(client)
    trader, refreshed_at = await service.require_profile(trader_id)
    return Envelope(data=trader, last_refreshed_at=refreshed_at)


@router.get("/traders/{trader_id}/trades", response_model=Envelope[dict[str, Any]])
async def get_trader_trades(client: FomoClientDep, trader_id: str) -> Envelope[dict[str, Any]]:
    service = TraderService(client)
    trades, refreshed_at = await service.get_trades(trader_id)
    return Envelope(data=trades, last_refreshed_at=refreshed_at)


@router.get("/traders/{trader_id}/balances", response_model=Envelope[Any])
async def get_trader_balances(client: FomoClientDep, trader_id: str) -> Envelope[Any]:
    service = TraderService(client)
    balances, refreshed_at = await service.require_balances(trader_id)
    return Envelope(data=balances, last_refreshed_at=refreshed_at)


@router.get("/traders/{trader_id}/activity", response_model=Envelope[list[TraderActivity]])
async def get_trader_activity(
    client: FomoClientDep,
    trader_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    chain: str | None = None,
    token_id: str | None = None,
) -> Envelope[list[TraderActivity]]:
    service = ActivityService(client)
    actions, refreshed_at, total = await service.require_activity(
        trader_id, page=page, page_size=page_size, from_ts=from_ts, to_ts=to_ts, chain=chain, token_id=token_id
    )
    return Envelope(
        data=actions, last_refreshed_at=refreshed_at, page=make_page(page, page_size, total)
    )
