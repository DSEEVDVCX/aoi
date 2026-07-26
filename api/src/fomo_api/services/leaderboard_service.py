from __future__ import annotations

from datetime import datetime

from fomo_api.clients.fomo_client import FomoClient
from fomo_api.config import settings
from fomo_api.models.leaderboard import Leaderboard
from fomo_api.models.trader import Trader


class LeaderboardService:
    def __init__(self, client: FomoClient) -> None:
        self._client = client

    async def get_leaderboard(
        self, page: int, page_size: int, period: str = "all"
    ) -> tuple[Leaderboard, datetime]:
        page = max(1, page)
        page_size = max(1, min(page_size, settings.max_page_size))
        data = await self._client.get_leaderboard(page, page_size, period)
        traders = [Trader(**t) for t in data["traders"]]
        total = data.get("total_items")
        return Leaderboard(traders=traders, total_items=total), self._client.last_refreshed_at
