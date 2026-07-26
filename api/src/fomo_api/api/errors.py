from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

UNAUTHORIZED = "UNAUTHORIZED"
RATE_LIMITED = "RATE_LIMITED"
NOT_FOUND = "NOT_FOUND"
VALIDATION_ERROR = "VALIDATION_ERROR"
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
STALE_DATA = "STALE_DATA"
UPSTREAM_CHANGED = "UPSTREAM_CHANGED"

STATUS_BY_CODE: dict[str, int] = {
    UNAUTHORIZED: 401,
    RATE_LIMITED: 429,
    NOT_FOUND: 404,
    VALIDATION_ERROR: 422,
    UPSTREAM_UNAVAILABLE: 502,
    STALE_DATA: 503,
    UPSTREAM_CHANGED: 502,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


class UnauthorizedError(ApiError):
    def __init__(self, message: str = "Valid consumer key required") -> None:
        super().__init__(UNAUTHORIZED, message)


class NotFoundError(ApiError):
    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(NOT_FOUND, f"{resource} not found", {"resource": resource, "id": identifier})


class UpstreamUnavailableError(ApiError):
    def __init__(self, message: str = "fomo.family is currently unreachable", details: dict[str, Any] | None = None) -> None:
        super().__init__(UPSTREAM_UNAVAILABLE, message, details)


class UpstreamChangedError(ApiError):
    def __init__(self, message: str = "fomo.family response shape changed and could not be mapped") -> None:
        super().__init__(UPSTREAM_CHANGED, message)


def _envelope(code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def _status_for(code: str) -> int:
    return STATUS_BY_CODE.get(code, 500)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=_status_for(exc.code), content=_envelope(exc.code, exc.message, exc.details))

    @app.exception_handler(UpstreamChangedError)
    async def _upstream_changed_handler(_: Request, exc: UpstreamChangedError) -> JSONResponse:
        return JSONResponse(status_code=502, content=_envelope(exc.code, exc.message, exc.details))
