from __future__ import annotations

from pydantic import BaseModel, Field

from fomo_api.models.activity import TokenRef


class Alert(BaseModel):
    id: str
    trader_id: str
    token: TokenRef
    amount_usd: float | None = Field(default=None, ge=0)
    timestamp: str
