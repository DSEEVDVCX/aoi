"""عميل GeckoTerminal العام — fallback عمر العملات عبر pool_created_at.

لماذا هذه الطبقة: بوابة العمر تقفل على «مجهول» fail-closed حين لا يجد
`filterTokens` العملة (عملات غير رائجة تحديدًا — مقيس: فئة المجهول ليست فئة
الحديثة). مقيس 2026-08-27 على عيّنة من مجهولي fomo: **17 من 18** أرجعها
GeckoTerminal بعمر حقيقي عبر أقدم pool. عميل HTTP العادي يُحجب (403 على
أي UA غير متصفح) لذا نستخدم curl_cffi ببصمة كروم، كخط fomo نفسه.

القيود المقيسة التي يبنى عليها التصميم:
- الحد العام ~30 نداء/دقيقة بلا مفتاح → نداء واحد لكل عملة، والدورة تجرّ
  المرشّحين الجدد فقط (وسيط 1.11/دورة)، والنتيجة تُخزَّن فلا يُسأل ثانيةً.
- القائمة تُرتَّب بالحجم لا بالزمن → نأخذ **أقدم** `pool_created_at` من
  أوّل صفحة (العملة قد تفتح pools جديدة متأخرة؛ القديم هو الميلاد).
- 429 يعني «لاحقًا» لا «فشل»: يعيد None فيكمل fomo مساره الطبيعي، والإعادة
  تُدار من `AGE_MISSING_RETRY_SECONDS` في `token_age_lookup_state`.

الأمان: `pool_created_at` يمثّل ميلاد أول pool سيول — وهو ما تقيسه البوابة
(«عمر قابل للتداول»)؛ ومقارنة حيّة على عملة مشتركة أظهرت فرق 88 ثانية عن
`createdAt` لدى fomo، أي تطابق ضمن دقيقة على مدى بوابة يومين.
"""
from __future__ import annotations

import asyncio
from typing import Any

# خريطة معرّفات شبكاتنا إلى معرّفات GeckoTerminal (ميدان مقيس 2026-08-27).
NETWORK_MAP = {
    "1399811149": "solana",
    "4663": "eth",
    "8453": "base",
    "143": "arbitrum",
    "56": "bsc",
}

_BASE = "https://api.geckoterminal.com/api/v2/networks"
_TIMEOUT = 15


class GeckoTerminalClient:
    """زبون قراءة فقط لـpool_created_at. يُنشأ مرة ويُعاد استخدامه."""

    def __init__(self, session: Any = None) -> None:
        self._session = session

    async def _get(self, url: str) -> Any:
        if self._session is None:
            from curl_cffi.requests import AsyncSession

            self._session = AsyncSession(
                impersonate="chrome124",
                headers={"accept": "application/json"},
                timeout=_TIMEOUT,
            )
        return await self._session.get(url)

    async def pool_created_at(self, address: str, network_id: str) -> str | None:
        """أقدم pool_created_at للعملة، أو None إن لم توجد/انتهى الحد.

        لا يرفع أبدًا: فشل الشبكة والحد والغياب كلها None — الطبقة العليا
        تفرّق بينها عبر الحالة المخزّنة، وهذا النداء مجرّد fallback.
        """
        gecko_net = NETWORK_MAP.get(str(network_id))
        if not gecko_net:
            return None
        url = f"{_BASE}/{gecko_net}/tokens/{address}/pools"
        try:
            resp = await self._get(url)
        except Exception:  # noqa: BLE001 — fallback لا يُسقط الدورة أبدًا
            return None
        if getattr(resp, "status_code", None) != 200:
            return None            # 404 غير موجود، 429 حد المعدل — لاحقًا
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 — إجابة غير متوقعة
            return None
        created: list[str] = [
            pool["attributes"]["pool_created_at"]
            for pool in data.get("data", [])
            if isinstance(pool, dict)
            and isinstance(pool.get("attributes"), dict)
            and pool["attributes"].get("pool_created_at")
        ]
        return min(created) if created else None

    async def aclose(self) -> None:
        if self._session is not None:
            close = getattr(self._session, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 — إغلاق لا يهم فشله
                    pass
            self._session = None


_shared: GeckoTerminalClient | None = None
_shared_lock = asyncio.Lock()


async def shared_gecko_client() -> GeckoTerminalClient:
    """زبون واحد على مستوى العملية — الجلسة تُعاد استخدامها بلا إنشاء متكرر."""
    global _shared
    if _shared is None:
        async with _shared_lock:
            if _shared is None:
                _shared = GeckoTerminalClient()
    return _shared
