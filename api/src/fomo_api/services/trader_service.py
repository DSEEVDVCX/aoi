from __future__ import annotations

from datetime import datetime
from typing import Any

from fomo_api.api.errors import NotFoundError
from fomo_api.clients.fomo_client import FomoClient
from fomo_api.models.trader import Trader


class TraderService:
    def __init__(self, client: FomoClient) -> None:
        self._client = client

    async def get_profile(self, trader_id: str) -> tuple[Trader, datetime] | None:
        data = await self._client.get_trader_profile(trader_id)
        if data is None:
            return None
        return Trader(**data), self._client.last_refreshed_at

    async def require_profile(self, trader_id: str) -> tuple[Trader, datetime]:
        result = await self.get_profile(trader_id)
        if result is None:
            raise NotFoundError("Trader", trader_id)
        return result

    async def get_profile_by_handle(self, handle: str) -> tuple[Trader, datetime] | None:
        data = await self._client.get_trader_profile_by_handle(handle)
        if data is None:
            return None
        return Trader(**data), self._client.last_refreshed_at

    async def require_profile_by_handle(self, handle: str) -> tuple[Trader, datetime]:
        result = await self.get_profile_by_handle(handle)
        if result is None:
            raise NotFoundError("Trader", handle)
        return result

    async def get_trades(self, trader_id: str) -> tuple[dict[str, Any], datetime]:
        data = await self._client.get_trader_trades(trader_id)
        return data, self._client.last_refreshed_at

    async def get_balances(self, trader_id: str) -> tuple[Any, datetime] | None:
        data = await self._client.get_trader_balances(trader_id)
        if data is None:
            return None
        return data, self._client.last_refreshed_at

    async def require_balances(self, trader_id: str) -> tuple[Any, datetime]:
        result = await self.get_balances(trader_id)
        if result is None:
            raise NotFoundError("Trader", trader_id)
        return result
