from __future__ import annotations

from datetime import datetime
from typing import Any

from fomo_api.api.errors import VALIDATION_ERROR, ApiError, NotFoundError
from fomo_api.clients.fomo_client import FomoClient
from fomo_api.config import settings
from fomo_api.models.activity import TraderActivity


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class ActivityService:
    def __init__(self, client: FomoClient) -> None:
        self._client = client

    async def get_activity(
        self,
        trader_id: str,
        page: int,
        page_size: int,
        from_ts: str | None = None,
        to_ts: str | None = None,
        chain: str | None = None,
        token_id: str | None = None,
    ) -> tuple[list[TraderActivity], datetime, int | None] | None:
        if from_ts and to_ts:
            from_dt = _parse_ts(from_ts)
            to_dt = _parse_ts(to_ts)
            if from_dt is None or to_dt is None:
                raise ApiError(VALIDATION_ERROR, "Invalid timestamp format; use RFC 3339", {})
            if to_dt < from_dt:
                raise ApiError(
                    VALIDATION_ERROR, "Invalid time range: 'to' must be >= 'from'", {"from": from_ts, "to": to_ts}
                )
        page = max(1, page)
        page_size = max(1, min(page_size, settings.max_page_size))
        data = await self._client.get_trader_activity(
            trader_id, page, page_size, from_ts, to_ts, chain, token_id
        )
        if data is None:
            return None
        actions = [TraderActivity(**a) for a in data.get("actions", [])]
        return actions, self._client.last_refreshed_at, data.get("total_items")

    async def require_activity(self, trader_id: str, **kwargs: Any) -> tuple[list[TraderActivity], datetime, int | None]:
        result = await self.get_activity(trader_id, **kwargs)
        if result is None:
            raise NotFoundError("Trader", trader_id)
        return result
