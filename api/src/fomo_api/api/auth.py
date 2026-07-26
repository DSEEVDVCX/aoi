from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from fomo_api.auth.deps import get_session_store, require_consumer_key
from fomo_api.auth.privy_login import PrivyLoginError, PrivyLoginService
from fomo_api.auth.session import SessionStore
from fomo_api.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    method: str
    params: dict[str, Any] = {}


class LoginResponse(BaseModel):
    consumer_key: str
    expires_at: datetime


@router.post("/auth/login", response_model=LoginResponse)
async def login(req: LoginRequest, store: SessionStore = Depends(get_session_store)) -> LoginResponse:
    service = PrivyLoginService(settings.upstream_app_base)
    try:
        creds = await service.login_with_credentials()
    except PrivyLoginError as exc:
        from fomo_api.api.errors import UnauthorizedError

        logger.warning("Privy login failed: %s", exc)
        raise UnauthorizedError("Login failed") from exc
    consumer_key = await store.create(creds.access_token)
    if creds.refresh_token:
        await store.store_refresh_creds(
            creds.refresh_token,
            creds.app_id,
            pat=creds.pat,
            client_id=creds.client_id,
            ca_id=creds.ca_id,
        )
    expires_at = await store.expiry_at(consumer_key) or datetime.now(UTC)
    return LoginResponse(consumer_key=consumer_key, expires_at=expires_at)


@router.post("/auth/dev-token", response_model=LoginResponse)
async def dev_token_login(
    token: str,
    store: SessionStore = Depends(get_session_store),
) -> LoginResponse:
    """Dev-only: inject a raw Privy Bearer token directly (no browser needed).
    Only available when FOMO_API_DEV=true."""
    if not settings.dev:
        from fomo_api.api.errors import UnauthorizedError
        raise UnauthorizedError("dev-token endpoint is disabled in production")
    consumer_key = await store.create(token)
    expires_at = await store.expiry_at(consumer_key) or datetime.now(UTC)
    return LoginResponse(consumer_key=consumer_key, expires_at=expires_at)



@router.post("/auth/logout", status_code=204, response_class=Response)
async def logout(
    consumer_key: str = Depends(require_consumer_key),
    store: SessionStore = Depends(get_session_store),
) -> Response:
    await store.revoke(consumer_key)
    return Response(status_code=204)
