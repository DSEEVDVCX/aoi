from __future__ import annotations

from pydantic import BaseModel, Field


class TraderMetrics(BaseModel):
    pnl_pct: float | None = None
    win_rate_pct: float | None = Field(default=None, ge=0, le=100)
    volume_usd: float | None = Field(default=None, ge=0)
    avg_trade_size_usd: float | None = Field(default=None, ge=0)
    trade_count: int | None = Field(default=None, ge=0)
    realized_pnl_usd: float | None = None


class Trader(BaseModel):
    id: str
    handle: str
    display_name: str | None = None
    # Upstream has no rank field — it is derived from leaderboard position and is
    # absent when fetching a bare profile (/v2/users/{id}).
    rank: int | None = Field(default=None, ge=1)
    followers_count: int = Field(default=0, ge=0)
    num_trades: int | None = Field(default=None, ge=0)
    total_volume_usd: float | None = Field(default=None, ge=0)
    metrics: TraderMetrics
    wallet_address: str | None = None
    chains: list[str] = Field(default_factory=list)
    # Social/profile fields present upstream (CONFIRMED 2026-07-25). All optional
    # (FR-007): absent -> None. `twitter`/`description` were previously dropped.
    twitter: str | None = None
    description: str | None = None
    avg_hold_time_seconds: int | None = Field(default=None, ge=0)
    following: int | None = Field(default=None, ge=0)
    created_at: str | None = None
    profile_picture_url: str | None = None
    is_private: bool | None = None
