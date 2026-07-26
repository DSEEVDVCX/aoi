"""تخزين مؤقّت لصدارة المتصدّرين لمطابقة topTraders[].id → رتبة.

يُحمّل الصدارة عبر FomoClient.get_leaderboard مرّة كل ساعة (LEADERBOARD_REFRESH_SECONDS)
ويحتفظ بخريطة id→rank في الذاكرة. المسجّل يستشيرها لحساب top_trader_match_count
و buyers_best_rank لكل حدث شراء — لالتقاط إشارة "أكثر من متصدّر اشترى".

النجاة من الأعطال: فشل تحميل الصدارة لا يمسح الخريطة القديمة ولا يُفشل الدورة.
"""
from __future__ import annotations

from typing import Any

from extract import build_rank_lookup


class LeaderboardCache:
    def __init__(self, client: Any, size: int, refresh_seconds: int) -> None:
        self._client = client
        self._size = size
        self._refresh_seconds = refresh_seconds
        self._lookup: dict[str, int] = {}
        self._loaded_at_mono: float | None = None

    @property
    def lookup(self) -> dict[str, int]:
        """الخريطة الحالية (قد تكون فارغة قبل أول تحميل ناجح)."""
        return self._lookup

    def set_client(self, client: Any) -> None:
        """يوجّه الكاش إلى عميل جديد (بعد تدوير التوكن). الخريطة القديمة تبقى
        صالحة حتى التحميل التالي — التوكن يتغيّر لا محتوى الصدارة."""
        self._client = client

    def is_stale(self, now_mono: float) -> bool:
        if self._loaded_at_mono is None:
            return True
        return (now_mono - self._loaded_at_mono) >= self._refresh_seconds

    async def refresh(self, now_mono: float) -> bool:
        """يعيد تحميل الصدارة. يعيد True عند النجاح. الفشل يبقي الخريطة القديمة."""
        try:
            data = await self._client.get_leaderboard(
                page=1, page_size=self._size, period="all"
            )
        except Exception:
            return False  # الحلقة الأعلى تسجّل الفشل في meta وتكمل
        traders = data.get("traders") if isinstance(data, dict) else None
        if not isinstance(traders, list):
            return False
        new_lookup = build_rank_lookup(traders)
        if new_lookup:
            self._lookup = new_lookup
            self._loaded_at_mono = now_mono
            return True
        return False

    async def maybe_refresh(self, now_mono: float) -> bool:
        if self.is_stale(now_mono):
            return await self.refresh(now_mono)
        return False
