from __future__ import annotations

from fastapi import APIRouter

from fomo_api.api.alerts import router as alerts_router
from fomo_api.api.auth import router as auth_router
from fomo_api.api.feed import router as feed_router
from fomo_api.api.leaderboard import router as leaderboard_router
from fomo_api.api.signals import router as signals_router
from fomo_api.api.tokens import router as tokens_router
from fomo_api.api.traders import router as traders_router

api_router = APIRouter()

api_router.include_router(auth_router, tags=["auth"])
api_router.include_router(leaderboard_router, tags=["leaderboard"])
api_router.include_router(traders_router, tags=["traders"])
api_router.include_router(alerts_router, tags=["alerts"])
api_router.include_router(signals_router, tags=["signals"])
api_router.include_router(feed_router, tags=["feed"])
api_router.include_router(tokens_router, tags=["tokens"])
