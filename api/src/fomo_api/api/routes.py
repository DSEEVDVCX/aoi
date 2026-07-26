from __future__ import annotations

from fastapi import APIRouter

api_router = APIRouter()


def _include_story_routers() -> None:
    try:
        from fomo_api.api.auth import router as auth_router

        api_router.include_router(auth_router, tags=["auth"])
    except ImportError:
        pass
    try:
        from fomo_api.api.leaderboard import router as leaderboard_router

        api_router.include_router(leaderboard_router, tags=["leaderboard"])
    except ImportError:
        pass
    try:
        from fomo_api.api.traders import router as traders_router

        api_router.include_router(traders_router, tags=["traders"])
    except ImportError:
        pass
    try:
        from fomo_api.api.alerts import router as alerts_router

        api_router.include_router(alerts_router, tags=["alerts"])
    except ImportError:
        pass
    try:
        from fomo_api.api.signals import router as signals_router

        api_router.include_router(signals_router, tags=["signals"])
    except ImportError:
        pass
    try:
        from fomo_api.api.feed import router as feed_router

        api_router.include_router(feed_router, tags=["feed"])
    except ImportError:
        pass
    try:
        from fomo_api.api.tokens import router as tokens_router

        api_router.include_router(tokens_router, tags=["tokens"])
    except ImportError:
        pass


_include_story_routers()
