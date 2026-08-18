"""عميل قراءة لسولانا RPC (Helius) — طبقة السلسلة.

**قراءة فقط** (FR-012): لا يستدعي إلّا توابع استعلام (`getTokenSupply`،
`getTokenLargestAccounts`، `getAccountInfo`، `getAsset`، `getTokenAccountsByOwner`)
ولا يوقّع معاملة ولا يرسل شيئاً إلى السلسلة.

**المفتاح لا يُطبع ولا يُسجَّل** (FR-013)، وهذا أخطر ممّا يبدو هنا: مفتاح Helius
يقع في *مسار* الرابط (`?api-key=...`)، ورسائل استثناءات httpx تحمل الرابط كاملاً
(`httpx.HTTPStatusError` و`ConnectError` كلاهما). والمسجّل يكتب نصّ الاستثناء إلى
`meta.last_error_*` وتعرضه لوحة القيادة ⇒ استثناء غير مُعالَج = مفتاح مكشوف في
القاعدة وعلى الشاشة. فكل استثناء يمرّ عبر `_redact()` قبل أن يُرى، ولا يُعاد رفع
الأصليّ خارج هذه الوحدة.

ويُقرأ المفتاح **من القرص عند كل نداء** لا مرّة عند الإطلاق: عملية طويلة العمر
تُجمّد قيمة الإطلاق فتفشل بعد تدويرها بلا سبب ظاهر — نفس درس رمز جلسة FOMO.
"""
from __future__ import annotations

import asyncio
from typing import Any

import config
import httpx
from provider_keys import KeyPool, read_keys


class ChainKeyMissing(RuntimeError):
    """لا مفتاح على القرص — الطبقة تتوقّف بوضوح بدل أن تفشل بـ401 كل دورة."""


class ChainRPCError(RuntimeError):
    """فشل نداء السلسلة. رسالته **مشطوبة** من المفتاح دائماً."""


def _read_keys() -> list[str]:
    """كل المفاتيح المتاحة بالترتيب: البيئة، ثمّ الجمع، ثمّ المفرد القديم.

    (كان هنا `_read_key()` مفردٌ سابقٌ للتعدّد؛ حُذف بعد أن صار كل نداء يمرّ
    بالحوض — بقاؤه ميتاً يعني طريقَ قراءةٍ ثانياً بقواعدَ أخرى ينتظر مَن يستعمله
    بالخطأ فيفقد التدوير بلا أيّ خطأ ظاهر. رسالةُ الغياب انتقلت إلى `_post`
    محتفظةً بمسار الملف.)
    """
    return read_keys("helius_api_keys", "helius_api_key", "HELIUS_API_KEY")


def _redact(text: str, key: str) -> str:
    """يشطب المفتاح من أي نصّ قبل تسجيله أو رفعه.

    يشطب أيضاً `api-key=<أي شيء>` احتياطاً: لو تغيّر المفتاح على القرص بين
    النداء والاستثناء لم يعد `key` مطابقاً للموجود في الرابط، فالشطب بالقيمة
    وحدها لا يكفي.
    """
    out = text.replace(key, "<محجوب>") if key else text
    if "api-key=" in out:
        head, _, tail = out.partition("api-key=")
        rest = tail
        for i, ch in enumerate(tail):
            if ch in " \t\"'>)&#":
                rest = tail[i:]
                break
        else:
            rest = ""
        out = head + "api-key=<محجوب>" + rest
    return out


class SolanaRPC:
    """عميل غير متزامن لعقدة سولانا. يُعاد استخدام الاتّصال بين النداءات."""

    def __init__(self, url: str | None = None, timeout: float | None = None) -> None:
        self._base = url or config.SOLANA_RPC_URL
        self._timeout = timeout if timeout is not None else config.CHAIN_TIMEOUT_SECONDS
        self._client = httpx.AsyncClient(timeout=self._timeout)
        self._keys = KeyPool(_read_keys())

    async def aclose(self) -> None:
        await self._client.aclose()

    def key_stats(self) -> dict[str, Any]:
        """صورةُ حوض المفاتيح للرصد — أعدادٌ فقط، بلا أي قيمة (FR-013).

        تُقرأ من القرص أوّلاً كي يعكس العدد ما هو موجود **الآن** لا ما كان عند
        الإطلاق: المفتاح الجديد يُضاف بلا إعادة تشغيل، فتقريرٌ متجمّد على قيمة
        الإطلاق كان سيقول «مفتاح واحد» بعد إضافة الثاني.
        """
        try:
            self._keys.refresh(_read_keys())
        except Exception:  # noqa: BLE001 — قراءةُ قرصٍ فاشلة لا تُسقط تقريراً
            pass
        return self._keys.stats()

    async def _step_aside(self, *, rejected: bool) -> None:
        """رفضُ المفتاح يُدوَّر، والعطلُ العابر يُمهَل بنفس المفتاح.

        الفرق جوهريّ: 401/429 تعني «هذا المفتاح لا يخدمك الآن» ⇒ غيّره. أمّا
        522 ومهلة القراءة فتعني «الخدمة نفسها متعطّلة» ⇒ تدويرُ المفاتيح يبرّدها
        كلّها بلا ذنب ولا يجلب نجاحاً. وبمفتاح واحد لا بديل أصلاً ⇒ التمهّل هو
        كلّ ما نملكه، وهو ما كان غائباً.
        """
        if rejected and len(self._keys.keys) > 1:
            self._keys.rotate(block_current=True)
            return
        await asyncio.sleep(float(config.CHAIN_TRANSIENT_BACKOFF_SECONDS))

    async def _post(self, payload: Any) -> Any:
        """طلب HTTP واحد (منفرد أو دفعة). يرفع ChainRPCError مشطوبةً عند الفشل."""
        self._keys.refresh(_read_keys())
        if not self._keys.keys:
            # المسار في الرسالة (وهو ليس سرّاً): «لا مفاتيح» بلا موضعِ الملف
            # يُرسل القارئ يبحث عن مكانٍ يضع فيه المفتاح.
            raise ChainKeyMissing(
                "لا توجد قيم في helius_api_keys أو helius_api_key — الملف: "
                f"{config.chain_keys_path()}"
            )
        # المحاولات لا تُشتقّ من عدد المفاتيح وحده (راجع `CHAIN_TRANSIENT_RETRIES`).
        attempts = max(
            int(config.CHAIN_TRANSIENT_RETRIES) + 1, len(self._keys.keys),
        )
        for attempt in range(attempts):
            key = self._keys.current()
            url = f"{self._base}?api-key={key}"
            try:
                resp = await self._client.post(url, json=payload)
                status = resp.status_code
                if status in (401, 403, 429):
                    if attempt + 1 < attempts:
                        await self._step_aside(rejected=True)
                        continue
                elif status >= 500 and attempt + 1 < attempts:
                    # 522 مهلة Cloudflare إلى الأصل: المفتاح سليم والخدمة لا.
                    await self._step_aside(rejected=False)
                    continue
                if status >= 400:
                    raise ChainRPCError(_redact(f"HTTP {status}: {resp.text[:200]}", key))
                body = resp.json()
                errors = []
                if isinstance(body, dict) and body.get("error") is not None:
                    errors.append(body["error"])
                elif isinstance(body, list):
                    errors.extend(
                        item["error"] for item in body
                        if isinstance(item, dict) and item.get("error") is not None
                    )
                retryable = any(
                    (error.get("code") if isinstance(error, dict) else None)
                    in (-32600, -32603, 429)
                    or any(
                        word in str(
                            error.get("message") if isinstance(error, dict) else error
                        ).lower()
                        for word in ("deprioritized", "overloaded", "rate limit")
                    )
                    for error in errors
                )
                if retryable and attempt + 1 < attempts:
                    # «Slow down requests» إشارةُ حمل: بمفتاح ثانٍ نجرّبه، وبمفتاح
                    # واحد نُمهّل — ولا نمرّ بلا شيء كما كان.
                    await self._step_aside(rejected=True)
                    continue
                return body
            except ChainRPCError:
                raise
            except httpx.TransportError as exc:
                # مهلة قراءة أو انقطاع اتّصال: عطلٌ عابر لا عطبُ مفتاح.
                if attempt + 1 < attempts:
                    await self._step_aside(rejected=False)
                    continue
                raise ChainRPCError(_redact(f"{type(exc).__name__}: {exc}", key)) from None
            except Exception as exc:  # noqa: BLE001 — لا يخرج استثناء أصليّ من هنا
                raise ChainRPCError(_redact(f"{type(exc).__name__}: {exc}", key)) from None
        raise ChainRPCError("تعذّر النداء بعد استنفاد المحاولات والمفاتيح")

    @staticmethod
    def _unwrap(body: Any, method: str) -> Any:
        """يستخرج `result` أو يرفع خطأ JSON-RPC.

        المصدر يعيد 200 مع كتلة `error` (شوهد `-32603` و`-32600` عند المزاحمة)،
        فالنجاح ليس رمز الحالة بل وجود `result`.
        """
        if not isinstance(body, dict):
            raise ChainRPCError(f"{method}: ردّ غير متوقّع ({type(body).__name__})")
        if "error" in body and body["error"] is not None:
            err = body["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ChainRPCError(f"{method}: JSON-RPC {code}: {str(msg)[:150]}")
        if "result" not in body:
            raise ChainRPCError(f"{method}: لا result ولا error في الردّ")
        return body["result"]

    async def fetch_concentration_raw(self, mint: str) -> dict[str, Any]:
        """العرض + أكبر الحسابات في **طلب HTTP واحد** عبر دفعة JSON-RPC.

        `getTokenLargestAccounts` يعطي الكميّات ولا يعطي العرض الكلّي، والنسبة
        تحتاج المقامَ — فالنداءان لازمان. الدفعة تجعلهما طلباً واحداً (مقيس:
        4 من 4 في طلب واحد)، فتبقى الكلفة نداءً واحداً للعملة في الدورة.

        لا نأخذ العرض من `market_ticks.total_supply` (وهو مملوء 100%) عمداً:
        مصدر آخر بإيقاع آخر، فقد يقيس العرض لحظةً غير لحظة الكميّات — والحرق
        والطبع يغيّران العرض فعلاً. بسط ومقام من نفس اللحظة أو لا نسبة.
        """
        payload = [
            {"jsonrpc": "2.0", "id": "supply", "method": "getTokenSupply", "params": [mint]},
            {"jsonrpc": "2.0", "id": "largest",
             "method": "getTokenLargestAccounts", "params": [mint]},
        ]
        body = await self._post(payload)
        if not isinstance(body, list):
            raise ChainRPCError(f"الدفعة أعادت {type(body).__name__} لا قائمة")
        by_id = {str(item.get("id")): item for item in body if isinstance(item, dict)}
        missing = [k for k in ("supply", "largest") if k not in by_id]
        if missing:
            raise ChainRPCError(f"الدفعة ناقصة: {', '.join(missing)}")
        supply = self._unwrap(by_id["supply"], "getTokenSupply")
        largest = self._unwrap(by_id["largest"], "getTokenLargestAccounts")
        supply_slot = (supply.get("context") or {}).get("slot") if isinstance(supply, dict) else None
        largest_slot = (largest.get("context") or {}).get("slot") if isinstance(largest, dict) else None
        if (isinstance(supply_slot, int) and isinstance(largest_slot, int)
                and abs(supply_slot - largest_slot) > config.CHAIN_MAX_SLOT_LAG):
            raise ChainRPCError(
                "لقطتا العرض والحسابات غير متزامنتين: "
                f"فارق {abs(supply_slot - largest_slot)} slot"
            )
        return {
            "supply": supply,
            "largest": largest,
        }

    async def fetch_authority_raw(self, mint: str) -> dict[str, Any]:
        """حساب المِنت + أصل DAS في **طلب HTTP واحد** (الطبقة البطيئة).

        المصدران لازمان ولا يُغني أحدهما عن الآخر — مقيس على 48 عملة حيّة:
        `getAccountInfo` يعطي صلاحية السكّ والتجميد والعرض من حساب المِنت نفسه
        ولا يعرف `mutable` إطلاقاً؛ و`getAsset` (DAS) يعطي `mutable` والمنشئين
        ولا يعطي صلاحية السكّ. و21 من 48 عملة على `spl-token` القديم بلا
        امتدادات ميتاداتا أصلاً، فسلطة التعديل عندها لا تُقرأ إلّا من DAS.
        """
        payload = [
            {"jsonrpc": "2.0", "id": "mint", "method": "getAccountInfo",
             "params": [mint, {"encoding": "jsonParsed"}]},
            {"jsonrpc": "2.0", "id": "asset", "method": "getAsset", "params": {"id": mint}},
        ]
        body = await self._post(payload)
        if not isinstance(body, list):
            raise ChainRPCError(f"الدفعة أعادت {type(body).__name__} لا قائمة")
        by_id = {str(item.get("id")): item for item in body if isinstance(item, dict)}
        missing = [k for k in ("mint", "asset") if k not in by_id]
        if missing:
            raise ChainRPCError(f"الدفعة ناقصة: {', '.join(missing)}")
        out = {"mint": self._unwrap(by_id["mint"], "getAccountInfo")}
        # DAS قد يفشل وحده (عملة غير مفهرسة) وحساب المِنت وحده يكفي لأهمّ
        # عمودين — فلا نُسقِط القياس كلّه بسببه، بل نحفظ خطأه مكان القيمة.
        try:
            out["asset"] = self._unwrap(by_id["asset"], "getAsset")
        except ChainRPCError as exc:
            out["asset"] = None
            out["asset_error"] = str(exc)
        return out

    async def fetch_owner_token_balance_raw(self, owner: str, mint: str) -> Any:
        """رصيد عنوانٍ بعينه من عملةٍ بعينها — نداء ثانٍ مشروط لحيازة المطوّر.

        `getTokenAccountsByOwner` مع فلتر `mint` يعيد حسابات ذلك العنوان فقط،
        فهو نداء رخيص (لا مسح للسلسلة). لا يمكن دمجه في دفعة الأصل لأنّ عنوان
        المطوّر نفسه لا يُعرف إلّا من ردّها.
        """
        body = await self._post({
            "jsonrpc": "2.0", "id": "owner", "method": "getTokenAccountsByOwner",
            "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        })
        return self._unwrap(body, "getTokenAccountsByOwner")
