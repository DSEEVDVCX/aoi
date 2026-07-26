from __future__ import annotations

from pydantic import BaseModel

from fomo_api.models.trader import Trader


class Leaderboard(BaseModel):
    traders: list[Trader]
    total_items: int | None = None
