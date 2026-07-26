from __future__ import annotations

from datetime import UTC, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total_items: int | None = None
    total_pages: int | None = None


class Envelope(BaseModel, Generic[T]):
    data: T
    last_refreshed_at: datetime
    page: Page | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


def fresh(data: T, last_refreshed_at: datetime | None = None, page: Page | None = None) -> Envelope[T]:
    return Envelope(data=data, last_refreshed_at=last_refreshed_at or utcnow(), page=page)


def make_page(page: int, page_size: int, total_items: int | None = None) -> Page:
    total_pages: int | None = None
    if total_items is not None and page_size > 0:
        total_pages = max(1, (total_items + page_size - 1) // page_size)
    return Page(page=page, page_size=page_size, total_items=total_items, total_pages=total_pages)
