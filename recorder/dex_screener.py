"""عميل DEX Screener العام — طبقة socials فقط (القيمة المقيسة الوحيدة).

جرد كامل 2026-08-29 لكل حقول استجابتهم مقابل مخزوننا من fomo: سعر/سيولة/
حجم/تغيير/عدد صفقات/عمر pool/منصة/صور/مواقع — **كلها مكررة أو نتفوق عليها**
(flow عندنا أعمق: 5 دقائق مع فريدين مقابل m5 مجرد عدد). القيمة الوحيدة
المضافة فعلًا: `info.socials[]` — تغطية مقيسة 11/12 عملة نشطة (92%).

الدوران اللذان نبنيه فوقها:
  1. `social_channels_dex`   — عدد قنوات التواصل عند DEX (شرعية تسويقية).
  2. `social_match_fomo_dex` — توافق المصدرين: عملة تقول fomo لها تويتر
     وDEX لا يرى شيئًا = نمط ملف مزوّر (إشارة نصب شائعة) والعكس صدقٌ
     متبادل يرفع الثقة.

القيود المقيسة: حد المعدل سخي (لم نشهد 429 في القياس) لكن النداء واحد
لكل عملة **جديدة** فقط — تُسأل مرة عند القبول وتُخزَّن في token_static
(ثوابت العملة تُكتب مرة)، فلا حلقة مستمرة ولا ازدواج جمع. العملات التي
لا يجيب عنها DEX تبقى NULL (غياب لا صفر — قد لا تكون مفهرسة بعد).

الشبكات: خريطة معرفاتنا → معرفاتهم (سولانا حساسة لحالة الأحرف).
"""
from __future__ import annotations

from typing import Any

# خريطة شبكاتنا → chainId عند DEX Screener.
NETWORK_MAP = {
    "1399811149": "solana",
    "4663": "ethereum",
    "8453": "base",
    "56": "bsc",
    "143": "arbitrum",
}

_BASE = "https://api.dexscreener.com/latest/dex/tokens"
_TIMEOUT = 15


class DexScreenerClient:
    """زبين قراءة فقط لقنوات التواصل. يُنشأ مرة لكل دورة استخدامه."""

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

    async def social_channels(self, address: str, network_id: str) -> int | None:
        """عدد قنوات التواصل لأعلى زوج سيولة، أو None إن لم تُفهرس.

        لا يرفع أبدًا: فشل الشبكة والغياب كلها None — الطبقة العليا
        تخزن الغياب كما هو (غياب مقيس لا صفر).
        """
        chain = NETWORK_MAP.get(str(network_id))
        if not chain:
            return None
        addr = address if str(network_id) == "1399811149" else address.lower()
        try:
            resp = await self._get(f"{_BASE}/{addr}")
        except Exception:  # noqa: BLE001 — طبقة مساندة لا تُسقط الدورة أبدًا
            return None
        if getattr(resp, "status_code", None) != 200:
            return None            # غير مفهرسة أو حد المعدل — لاحقًا
        try:
            pairs = resp.json().get("pairs") or []
        except Exception:  # noqa: BLE001
            return None
        if not pairs:
            return None
        # أعلى زوج سيولة هو الزوج الرئيسي للمظهر العام للعملة.
        top = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        info = top.get("info") or {}
        socials = info.get("socials")
        return len(socials) if isinstance(socials, list) else None

    async def aclose(self) -> None:
        if self._session is not None:
            close = getattr(self._session, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 — إغلاق لا يهم فشله
                    pass
            self._session = None
