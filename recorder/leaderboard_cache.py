"""تخزين مؤقّت لصدارة المتصدّرين لمطابقة topTraders[].id → رتبة.

يُحمّل الصدارة مرّة كل ساعة (LEADERBOARD_REFRESH_SECONDS) ويحتفظ بخريطة
id→rank في الذاكرة. المسجّل يستشيرها لحساب top_trader_match_count و
buyers_best_rank لكل حدث شراء — لالتقاط إشارة "أكثر من متصدّر اشترى".

**الخام مُحتفَظ به (`last_raw`)**: الصدارة كانت تُقرأ وتُرمى كل ساعة، فضاع
مسار كل متصدّر (صعود من #80 إلى #5 ثمّ احتراق) إلى الأبد — لا سبيل لإعادة
بنائه رجعياً. المسجّل يؤرشف `last_raw` في snapshots كل ساعة. ولهذا يجلب
الكاش الخام عبر `_get` مباشرة لا عبر `get_leaderboard` المُعيِّنة: التعيين
يسقط حقولاً (سابقة موثّقة في `_map_trader` نفسها)، والأرشيف الخام وحده
يضمن إعادة الاشتقاق.

النجاة من الأعطال: فشل تحميل الصدارة لا يمسح الخريطة القديمة ولا يُفشل الدورة.
"""
from __future__ import annotations

from typing import Any

from extract import build_rank_lookup, leaderboard_items


class LeaderboardCache:
    def __init__(self, client: Any, size: int, refresh_seconds: int) -> None:
        self._client = client
        self._size = size
        self._refresh_seconds = refresh_seconds
        self._lookup: dict[str, int] = {}
        self._loaded_at_mono: float | None = None
        self._last_raw: Any | None = None

    @property
    def lookup(self) -> dict[str, int]:
        """الخريطة الحالية (قد تكون فارغة قبل أول تحميل ناجح)."""
        return self._lookup

    @property
    def last_raw(self) -> Any | None:
        """آخر مغلّف خام كامل من الصدارة (للأرشفة في snapshots). None قبل
        أوّل تحميل ناجح."""
        return self._last_raw

    def set_client(self, client: Any) -> None:
        """يوجّه الكاش إلى عميل جديد (بعد تدوير التوكن). الخريطة القديمة تبقى
        صالحة حتى التحميل التالي — التوكن يتغيّر لا محتوى الصدارة."""
        self._client = client

    def is_stale(self, now_mono: float) -> bool:
        if self._loaded_at_mono is None:
            return True
        return (now_mono - self._loaded_at_mono) >= self._refresh_seconds

    async def refresh(self, now_mono: float) -> bool:
        """يعيد تحميل الصدارة خاماً. يعيد True عند النجاح. الفشل يبقي القديم."""
        from fomo_api.config import settings

        try:
            data = await self._client._get(
                settings.upstream_leaderboard_path, {"limit": self._size}
            )
        except Exception:
            return False  # الحلقة الأعلى تسجّل الفشل في meta وتكمل
        traders = leaderboard_items(data)
        if not traders:
            return False
        # الرتبة = الموضع (1-based) كما في _map_leaderboard — الخام بلا حقل rank.
        new_lookup = build_rank_lookup(
            [{**t, "rank": i + 1} for i, t in enumerate(traders)]
        )
        if not new_lookup:
            return False
        self._lookup = new_lookup
        self._loaded_at_mono = now_mono
        self._last_raw = data
        return True

    async def maybe_refresh(self, now_mono: float) -> bool:
        if self.is_stale(now_mono):
            return await self.refresh(now_mono)
        return False
