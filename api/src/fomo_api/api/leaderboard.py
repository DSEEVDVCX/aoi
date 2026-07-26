from __future__ import annotations

from fastapi import APIRouter, Query

from fomo_api.clients.deps import FomoClientDep
from fomo_api.config import settings
from fomo_api.models.envelope import Envelope, make_page
from fomo_api.models.trader import Trader
from fomo_api.services.leaderboard_service import LeaderboardService

router = APIRouter()


@router.get("/leaderboard", response_model=Envelope[list[Trader]])
async def get_leaderboard(
    client: FomoClientDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    period: str = Query(default="all", pattern="^(all|24h|7d|30d)$"),
) -> Envelope[list[Trader]]:
    service = LeaderboardService(client)
    leaderboard, refreshed_at = await service.get_leaderboard(page, page_size, period)
    page_meta = make_page(page, page_size, leaderboard.total_items)
    return Envelope(data=leaderboard.traders, last_refreshed_at=refreshed_at, page=page_meta)
