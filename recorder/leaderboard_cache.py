"""تخزين مؤقّت لصدارة المتصدّرين لمطابقة topTraders[].id → رتبة.

يُحمّل الصدارة مرّة كل ساعة (LEADERBOARD_REFRESH_SECONDS) ويحتفظ بخريطة
id→rank في الذاكرة. المسجّل يستشيرها لحساب top_trader_match_count و
buyers_best_rank لكل حدث شراء — لالتقاط إشارة "أكثر من متصدّر اشترى".

**أربع مدد لا واحدة**: المصدر يسقّف `/v2/leaderboard` عند 50 متصدّراً ويُهمل
كل صيغ الترقيم بصمت (مقيس: تسع صيغ، قوائم متطابقة بايتاً). لكنّ المسارات
`/24h` و`/7d` و`/30d` تعيد كلٌّ **100** (مقيس على الخام المؤرشف)، فالاتّحاد 214
متداولاً — أربعة أضعاف التغطية (مقيس على 7,200 حدث شراء: 3.68% ← 15.26%).
نحفظ خريطة **منفصلة لكل مدّة**: رتبة 7 في `24h` ليست رتبة 7 في `totalPnL`،
ودمجهما في رقم واحد يخلط قياسين. `lookup` تبقى خريطة "all" وحدها حفاظاً على
العقد القديم.

**الخام مُحتفَظ به (`raw_by_period`)**: الصدارة كانت تُقرأ وتُرمى كل ساعة، فضاع
مسار كل متصدّر (صعود من #80 إلى #5 ثمّ احتراق) إلى الأبد — لا سبيل لإعادة
بنائه رجعياً. المسجّل يؤرشف خام كل مدّة في snapshots كل ساعة بمصدر مستقلّ
(`leaderboard` / `leaderboard_24h` …). ولهذا يجلب الكاش الخام عبر `_get`
مباشرة لا عبر `get_leaderboard` المُعيِّنة: التعيين يسقط حقولاً (سابقة موثّقة
في `_map_trader` نفسها)، والأرشيف الخام وحده يضمن إعادة الاشتقاق.

النجاة من الأعطال: فشل مدّة لا يمسح خريطتها القديمة ولا يُفشل بقيّة المدد ولا
الدورة. `refresh` تعيد True إن نجحت **مدّة واحدة على الأقل**. والتمييز بين
"محمّل" و"طازج" جوهريّ: `raw_by_period` (للأرشفة) و`failed_periods` (للتنبيه)
تقيسان آخر محاولة، لا ما تراكم في الذاكرة — وإلّا أُرشف مغلّف قديم بتوقيت جديد
وصمت عطلٌ مستمرّ خلف خريطة نجحت مرّة.
"""
from __future__ import annotations

import asyncio
from typing import Any

from extract import build_rank_lookup, leaderboard_items

# ترتيب المدد ومسارها الأساسيّ. "all" = totalPnL (المسار غير المُلحَق).
_ALL = "all"


class LeaderboardCache:
    def __init__(
        self,
        client: Any,
        size: int,
        refresh_seconds: int,
        periods: tuple[str, ...] = (_ALL,),
        pacing_seconds: float = 0.0,
        sleep=asyncio.sleep,
    ) -> None:
        self._client = client
        self._size = size
        self._refresh_seconds = refresh_seconds
        self._periods = tuple(periods) or (_ALL,)
        self._pacing = pacing_seconds
        self._sleep = sleep
        self._lookups: dict[str, dict[str, int]] = {p: {} for p in self._periods}
        self._raw: dict[str, Any] = {}
        self._fresh: tuple[str, ...] = ()
        self._loaded_at_mono: float | None = None

    # --- قراءة الحالة ---
    @property
    def lookup(self) -> dict[str, int]:
        """خريطة المدّة الأساسيّة (totalPnL). فارغة قبل أول تحميل ناجح."""
        return self._lookups.get(_ALL, {})

    @property
    def lookups(self) -> dict[str, dict[str, int]]:
        """خرائط المدد كلّها {period → {trader_id → rank}} — لا تُدمج."""
        return self._lookups

    @property
    def last_raw(self) -> Any | None:
        """خام المدّة الأساسيّة (للأرشفة). None قبل أوّل تحميل ناجح."""
        return self._raw.get(_ALL)

    @property
    def raw_by_period(self) -> dict[str, Any]:
        """خام المدد التي نجحت **في آخر تحديث** — لا كل ما سبق تحميله.

        الحقل `_raw` يحتفظ بآخر مغلّف ناجح لكل مدّة إلى الأبد (تعمّداً: القراءة
        بعد فشل جزئيّ ما زالت ممكنة)، لكنّ الأرشفة منه تكذب: ساعة تفشل فيها
        `30d` كانت ستعيد ختم مغلّف الساعة الماضية بتوقيت هذه الساعة، فيصير في
        الأرشيف صدارةٌ لم نجلبها قطّ. نعيد الطازج وحده.
        """
        return {p: self._raw[p] for p in self._fresh if p in self._raw}

    @property
    def raw_seen_by_period(self) -> dict[str, Any]:
        """آخر خام ناجح لكل مدّة أياً كان عمره — للقراءة لا للأرشفة."""
        return self._raw

    def set_client(self, client: Any) -> None:
        """يوجّه الكاش إلى عميل جديد (بعد تدوير التوكن). الخرائط القديمة تبقى
        صالحة حتى التحميل التالي — التوكن يتغيّر لا محتوى الصدارة."""
        self._client = client

    def is_stale(self, now_mono: float) -> bool:
        if self._loaded_at_mono is None:
            return True
        return (now_mono - self._loaded_at_mono) >= self._refresh_seconds

    # --- التحميل ---
    def _path(self, period: str) -> str:
        from fomo_api.config import settings

        if period == _ALL:
            return settings.upstream_leaderboard_path
        return settings.upstream_leaderboard_period_paths.get(
            period, settings.upstream_leaderboard_path
        )

    async def _refresh_period(self, period: str) -> bool:
        """يحمّل مدّة واحدة. الفشل موضعيّ: يبقي خريطة هذه المدّة وخامها."""
        try:
            data = await self._client._get(self._path(period), {"limit": self._size})
        except Exception:  # noqa: BLE001 — فشلٌ موضعيّ: الحلقةُ الأعلى تسجّله وتكمل
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
        self._lookups[period] = new_lookup
        self._raw[period] = data
        return True

    async def refresh(self, now_mono: float) -> bool:
        """يعيد تحميل كل المدد خاماً. True إن نجحت واحدة على الأقل.

        الفشل الجزئيّ مقبول ومقصود: مدّة معطوبة لا تُسقط الباقي، والختم يُحدَّث
        عند أيّ نجاح فلا ندخل حلقة إعادة محاولة كل دقيقة على مدّة ميتة.
        """
        succeeded = 0
        fresh: list[str] = []
        for i, period in enumerate(self._periods):
            if i and self._pacing:
                await self._sleep(self._pacing)
            if await self._refresh_period(period):
                succeeded += 1
                fresh.append(period)
        self._fresh = tuple(fresh)
        if not succeeded:
            return False
        self._loaded_at_mono = now_mono
        return True

    async def maybe_refresh(self, now_mono: float) -> bool:
        if self.is_stale(now_mono):
            return await self.refresh(now_mono)
        return False

    def failed_periods(self) -> tuple[str, ...]:
        """المدد التي فشلت في **آخر** محاولة — تُعرَض في meta كي لا يصمت العطل.

        ليست "المدد بلا خريطة": مدّة نجحت أمس وتفشل اليوم تُبقي خريطتها القديمة
        (وهذا مقصود)، فلو قسنا الفشل بخلوّ الخريطة لصمت العطل إلى الأبد بينما
        نطابق برتب بايتة — وهو بالضبط ما وُجدت هذه الدالة لمنعه.
        """
        return tuple(p for p in self._periods if p not in self._fresh)
