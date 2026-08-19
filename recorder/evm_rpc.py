"""عميل قراءة لعقد EVM الرسميّة — بلا مفتاح، بلا مزوّد، بلا تسجيل.

**قراءة فقط** (FR-012): `eth_blockNumber`، `eth_getLogs`، `eth_call`،
`eth_getCode` — ولا توقيع ولا إرسال معاملة.

**لا مفتاح إطلاقاً**، وهذا نتيجة قياس لا تفضيل: Etherscan V2 يرفض بلا مفتاح
(`Missing/Invalid API Key`)، وV1 القديم مُلغى، وSourcify يعرف 1 من 10 عملات،
وBlockscout يكلّف 5.6 ثانية للعملة ويعطي أعلى 50 فقط. أمّا العقد الرسميّة فحرّة
وأسرع بـ27 ضعفاً (0.21 ث/عملة على روبن‑هود). فلا شيء يُشطب هنا لأنّ لا سرّ في
الرابط — بخلاف `solana_rpc.py` حيث المفتاح في الرابط نفسه.

**السقوف حدود المزوّد لا اختيارنا**، وكلّها مقيسة حيّاً 2026-08-13:
- `publicnode` (BSC) يرفض بـ403 مصفوفةَ عناوين أكبر من 5 ⇒ `EVM_ADDRESS_BATCH`.
- `bsc-dataseed1` يرفض المدى بـ`-32005 limit exceeded` عند كل عدد ⇒ مرفوض.
- روبن‑هود وBase قبلا 57 و22 عنواناً في نداء واحد (0.5 و0.4 ثانية).
- أي عقدة تقصّ عند 10,000 سجلّ ⇒ `_get_logs_paged` يقسم المدى نصفين ويعاود.
- وروبن‑هود يردّ على تعبئة من الكتلة صفر بـ`-32000 log query timed out` (مقيس
  على أوّل دورة حيّة) — وهي «المدى أوسع من طاقتي» بصياغة أخرى ⇒ تُقسَم مثله.
- وهو يكتم بـ429 بعد استعلام ثقيل ⇒ انتظار ثمّ إعادة **نفس** المدى: تسليمُه
  يعني ثغرة دائمة في الدفتر لا تأخيراً.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from typing import Any

import config
import httpx

# توقيع حدث `Transfer(address,address,uint256)` — keccak-256 لنصّ التوقيع.
# هذا **الطريق الوحيد** إلى قائمة حائزين على EVM: معيار ERC-20 لا يخزّن القائمة
# فلا نداء عقدة يعيدها، والرصيد لا يُعرف إلّا بإعادة تشغيل كل تحويل.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# موضوع بعرض 32 بايت لعنوان الصفر. `topics=[Transfer, ZERO_TOPIC]` يعني «التحويلات
# **من** عنوان الصفر» أي السكّ وحده — وأدنى كتلة فيها هي كتلة إنشاء العملة عمليّاً:
# لا رصيد يُوجَد إلّا بسكّ، ومعيار ERC-20 يُطلق `Transfer(0x0, …)` عند كل `_mint`.
ZERO_TOPIC = "0x" + "0" * 64

# عناوين لا تُحسب حائزاً: الصفر (سكّ وحرق) والحرق الصريح.
BURN_ADDRESSES = (
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
)


class EVMRPCError(RuntimeError):
    """فشل نداء عقدة EVM."""


class EVMLogLimit(EVMRPCError):
    """المدى أوسع ممّا تحمله العقدة ⇒ يجب أن يُقسَم لا أن يُقبَل أو يُهجَر.

    استثناء منفصل لا نصّ يُفحَص بـ`in` عند موضع الاستخدام: قبول ردٍّ مقصوص يعني
    دفتر أرصدة ناقصاً بصمت، وهو أسوأ من لا دفتر — الأرقام تبدو سليمة وهي كاذبة.

    ويشمل **مهلة الاستعلام** لا القصّ وحده: روبن‑هود ردّ على تعبئة من الكتلة صفر
    بـ`-32000 log query timed out` (مقيس 2026-08-13) — وهي نفس المعلومة بصياغة
    أخرى: «المدى أوسع من طاقتي». من دون ذلك تموت تعبئة العملة كلّها بدل أن تُقسَم.
    """


class EVMBatchLimit(EVMRPCError):
    """الردّ المجموع لدفعةٍ أكبر من طاقة العقدة ⇒ يُقلَّص **عدد** النداءات لا المدى.

    منفصل عن `EVMLogLimit` لأنّ العلاج مختلف: هناك المدى واسع، وهنا المدى مقبول
    ولكنّ حزم عشرة ردود مقبولة في ردٍّ واحد يتجاوز حدّ الحجم. مقيس على Base
    2026-08-19: `-32020 backend response too large` يسقط الدفعة **كلّها ذرّيّاً**،
    وأكبر دفعة مقبولة صفةُ عملةٍ لا صفةُ شبكة — قِيست 10 و5 و3 و2 و1 لخمس عملات
    مراقَبة. فقسمة المدى هنا تصحيحٌ في المكان الخطأ: تضاعف عدد النداءات لتحلّ
    مشكلة حجم، وتهدر سقف المدى المتاح.

    وحين تكون الدفعة **واحداً** أصلاً فلا عدد يُقلَّص ⇒ يُعامَل قصّاً في المدى.
    """


class EVMRateLimit(EVMRPCError):
    """العقدة العامّة كتمتنا (429) ⇒ تُنتظَر وتُعاد، ولا يُهجَر المدى.

    منفصل عن `EVMRPCError` لأنّ العلاج مختلف تماماً: القصّ يُقسَم، والكتم
    يُنتظَر — وقسمة المدى عند 429 تضاعف عدد النداءات فتزيد الكتم.
    """


def _num(value: Any) -> int | None:
    """يحوّل عدداً ستّ‑عشريّاً (`0x…`) أو عشريّاً إلى int، أو None إن تعذّر."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except (TypeError, ValueError):
        return None


def _topic_address(topic: Any) -> str | None:
    """يستخرج عنواناً من موضوع بعرض 32 بايت (آخر 20 بايت)."""
    if (
        not isinstance(topic, str)
        or re.fullmatch(r"0x[0-9a-fA-F]{64}", topic) is None
        or topic[2:26] != "0" * 24
    ):
        return None
    return "0x" + topic[-40:].lower()


def _evm_address(value: Any) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
        return None
    return value.lower()


def _uint256(value: Any) -> int | None:
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value) is None:
        return None
    return int(value, 16)


def _classify(label: str, code: Any, message: str) -> EVMRPCError:
    """رمز الخطأ ونصّه ⇒ الاستثناء الذي يحمل **العلاج** لا الوصف.

    ثلاثة علاجات لا يجوز خلطها: الكتم يُنتظَر، والمدى الواسع يُقسَم، والدفعة
    الثقيلة يُقلَّص عددها. والترتيب مقصود: «too many requests» كتمٌ لا سعةَ مدًى
    وإن شاركت كلمةً واحدة مع «too many logs».
    """
    low = message.lower()
    if (
        code in (429, -32011) or "too many requests" in low
        or "rate limit" in low or "no backend" in low or "try again" in low
    ):
        return EVMRateLimit(f"{label}: {message[:150]}")
    # حدّ **حجم الردّ** لا حدّ المدى: Base يردّ `-32020 backend response too large`
    # على دفعةٍ مدودها كلّها مقبولة (مقيس 2026-08-19) ⇒ يُقلَّص العدد. وحين يكون
    # العدد واحداً أصلاً يصعّده `get_logs_paged` إلى قسمةٍ في المدى.
    if code == -32020 or "response too large" in low:
        return EVMBatchLimit(f"{label}: {message[:150]}")
    # العقد تصوغ «المدى أوسع من طاقتي» بعبارات مختلفة (`exceeds limit of 10000`،
    # `query returned more than`، `limit exceeded`، `limited to a 10,000 range`،
    # و**مهلة الاستعلام**) وكلّها تعني شيئاً واحداً: قسّم المدى.
    if (
        code == -32614
        or "exceed" in low or "more than" in low or "too many" in low
        or "timed out" in low or "timeout" in low or "limited to a" in low
    ):
        return EVMLogLimit(f"{label}: {message[:150]}")
    return EVMRPCError(f"{label}: JSON-RPC {code}: {message[:150]}")


def _split_range(lo: int, hi: int, hint: int) -> list[tuple[int, int]]:
    """مدًى مرفوض ⇒ مدود أصغر مرتّبة **تصاعديّاً**، بلا فجوة ولا تراكب.

    التلميح يُستخدم **فقط إن قلّص المدى فعلاً**، وهذا شرط بقاء لا تحسين: مدًى
    طوله 10,000 بالضبط على Base يعيد `range(lo, hi+1, 10_000)` مدًى واحداً هو
    نفسه، فيُدفَع إلى المكدّس ليُرفَض ثانيةً — حلقةٌ تأكل سقف النداءات كلّه بصفر
    تقدّم. والتنصيف هو المخرج الوحيد المضمون التقلّص.
    """
    if hint > 0 and (hi - lo + 1) > hint:
        return [(start, min(hi, start + hint - 1)) for start in range(lo, hi + 1, hint)]
    mid = lo + (hi - lo) // 2
    return [(lo, mid), (mid + 1, hi)]


def _in_block_order(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ترتيب زمنيّ صارم للسجلّات المعادة.

    الدفعة تُرجع مدودها في ردٍّ واحد وقد يُعاد أحدها دون أخيه، فترتيب القراءة لم
    يبقَ هو ترتيب الكتل. والمستهلكون يرتّبون أو يجمّعون بأنفسهم، لكنّ العقد هنا
    أن يكون المُعاد مرتّباً: ترتيبٌ مرّةً هنا أرخص من ثقةٍ ضائعة في كل موضع.
    """
    return sorted(logs, key=lambda log: (
        _num(log.get("blockNumber")) or 0,
        _num(log.get("transactionIndex")) or 0,
        _num(log.get("logIndex")) or 0,
    ))


class EVMRPC:
    """عميل غير متزامن لعقد EVM. عميل httpx واحد لكل الشبكات.

    الشبكة تُمرَّر في كل نداء لا تُثبَّت في الكائن: نداءٌ واحد يغطّي شبكة كاملة،
    فالدورة تمرّ على ثلاث شبكات في ثوانٍ — وثلاثة كائنات لثلاث عقد تكلفة بلا مقابل.
    """

    def __init__(
        self,
        urls: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        self._urls = dict(urls or config.EVM_RPC_URLS)
        self._timeout = timeout if timeout is not None else config.EVM_TIMEOUT_SECONDS
        # بعض العقد العامّة ترفض بـ403 طلباً بلا `user-agent` (مقيس على
        # `bsc-rpc.publicnode.com`) — وترويسة واحدة أرخص من فقدان شبكة.
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"user-agent": "fomo-recorder/1.0", "content-type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def url_for(self, network_id: str) -> str:
        url = self._urls.get(str(network_id))
        if not url:
            raise EVMRPCError(f"لا عقدة معرّفة للشبكة {network_id}")
        return url

    async def _call(
        self, network_id: str, method: str, params: Any, *, url: str | None = None,
    ) -> Any:
        """نداء JSON-RPC واحد. يرفع `EVMLogLimit` عند القصّ و`EVMRPCError` سواه."""
        url = url or self.url_for(network_id)
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = await self._client.post(url, json=payload)
            status = resp.status_code
            if status == 429:
                raise EVMRateLimit(f"{method} [{network_id}] HTTP 429")
            if status >= 400:
                # الرمز وحده لا يكفي: Base تردّ على مدًى أوسع من 10,000 كتلة
                # بـ**HTTP 413** وجسمٍ فيه `-32614 eth_getLogs is limited to a
                # 10,000 range` (مقيس 2026-08-13)، فلو صار خطأً عامّاً لأُسقط
                # النداء بدل أن يُقسَّم المدى. و5xx العابر («no backend is
                # currently healthy») انتظارٌ لا عطب: نفس المدى يُعاد.
                text = resp.text[:400]
                low = text.lower()
                if status == 413 or "-32614" in low or "limited to a" in low:
                    raise EVMLogLimit(f"{method} [{network_id}] HTTP {status}: {text[:150]}")
                if status >= 500:
                    raise EVMRateLimit(f"{method} [{network_id}] HTTP {status}: {text[:150]}")
                raise EVMRPCError(f"{method} [{network_id}] HTTP {status}: {text[:200]}")
            body = resp.json()
        except EVMRPCError:
            raise
        except httpx.TransportError as exc:
            # مهلة قراءة أو انقطاع اتّصال: انتظارٌ لا عطب — وهو ما كان يُسقط
            # النداء نهائيّاً (شوهد `eth_blockNumber [4663] ReadTimeout` 2026-08-17)
            # لأنّه يقع في `except Exception` العامّ. تصنيفه كتماً يُشغّل التمهّل
            # الموجود في `get_logs_paged` بدل تسليم المدى — وتسليمه ثغرة دائمة.
            raise EVMRateLimit(f"{method} [{network_id}] {type(exc).__name__}") from None
        except Exception as exc:  # noqa: BLE001
            raise EVMRPCError(f"{method} [{network_id}] {type(exc).__name__}: {exc}") from None
        if not isinstance(body, dict):
            raise EVMRPCError(f"{method} [{network_id}]: ردّ غير متوقّع ({type(body).__name__})")
        err = body.get("error")
        if err is not None:
            code = err.get("code") if isinstance(err, dict) else None
            msg = str(err.get("message") if isinstance(err, dict) else err)
            raise _classify(f"{method} [{network_id}]", code, msg)
        if "result" not in body:
            raise EVMRPCError(f"{method} [{network_id}]: لا result ولا error")
        return body["result"]

    async def block_number(self, network_id: str) -> int:
        """رأس السلسلة — **مرساة** الدورة، فيُعاد عند الكتم والعطل العابر.

        فشله لا يُسقط عملةً بل مسحَ الشبكة بأسره (`evm_layer._apply_live`)، ونداءٌ
        واحد رخيص لا يستحقّ ذلك الثمن. راجع `EVM_HEAD_RETRIES`.
        """
        attempts = int(config.EVM_HEAD_RETRIES) + 1
        for attempt in range(attempts):
            try:
                raw = await self._call(network_id, "eth_blockNumber", [])
            except EVMRateLimit:
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(config.EVM_RATE_LIMIT_BACKOFF_SECONDS)
                continue
            block = _num(raw)
            if block is None:
                raise EVMRPCError(f"eth_blockNumber [{network_id}]: رقم كتلة غير مفهوم")
            return block
        raise EVMRateLimit(f"eth_blockNumber [{network_id}]: تعذّر بعد {attempts} محاولات")

    async def block_timestamp(self, network_id: str, block: int) -> int | None:
        """طابع كتلة واحدة بالثواني. لازم للإعادة الرجعيّة وحدها.

        الطبقة الحيّة لا تحتاجه (وقتها هو الآن)، أمّا الإعادة فتحوّل وقتاً إلى
        رقم كتلة والعكس — وعقدة روبن‑هود تعيد `blockTimestamp: '0x0'` في سجلّات
        `eth_getLogs` (مقيس 2026-08-13) فلا مصدر للوقت إلّا الكتلة نفسها.

        كتلة غير موجودة (أعلى من الرأس) تعيد `null` ⇒ `None` لا استثناء: السؤال
        عن كتلة لم تُنتَج بعد جوابه «ليست بعد» لا عطب.
        """
        blk = await self._call(network_id, "eth_getBlockByNumber", [hex(int(block)), False])
        if not isinstance(blk, dict):
            return None
        return _num(blk.get("timestamp"))

    async def get_code(self, network_id: str, address: str) -> str:
        """بايت‑كود العقد. `0x` = ليس عقداً (محفظة أو عنوان فارغ)."""
        result = await self._call(network_id, "eth_getCode", [address, "latest"])
        return result if isinstance(result, str) else "0x"

    async def get_code_at(self, network_id: str, address: str, block: int) -> str:
        """بايت‑كود عند كتلة محددة، مع خدمة الحالة التاريخية إن عُرّفت."""
        net = str(network_id)
        historical = config.EVM_HISTORICAL_RPC_URLS.get(net)
        if not historical:
            result = await self._call(net, "eth_getCode", [address, hex(int(block))])
            return result if isinstance(result, str) else "0x"
        result = await self._call(
            net, "eth_getCode", [address, hex(int(block))], url=historical,
        )
        return result if isinstance(result, str) else "0x"

    async def historical_block_number(self, network_id: str) -> int:
        """رأس خدمة الحالة التاريخية، أو رأس RPC الحي إن لم توجد خدمة خاصة."""
        net = str(network_id)
        historical = config.EVM_HISTORICAL_RPC_URLS.get(net)
        if not historical:
            return await self.block_number(net)
        block = _num(await self._call(net, "eth_blockNumber", [], url=historical))
        if block is None:
            raise EVMRPCError(f"eth_blockNumber [{net}]: رقم كتلة غير مفهوم")
        return block

    async def contract_creation_block(
        self, network_id: str, address: str, head: int,
    ) -> int | None:
        """أول كتلة كان فيها للعقد بايت‑كود، ببحث ثنائي مضبوط."""
        historical_head = await self.historical_block_number(network_id)
        hi = min(max(0, int(head)), historical_head)
        if await self.get_code_at(network_id, address, hi) in ("0x", "0x0", ""):
            return None
        lo = 0
        while lo < hi:
            mid = lo + (hi - lo) // 2
            code = await self.get_code_at(network_id, address, mid)
            if code in ("0x", "0x0", ""):
                lo = mid + 1
            else:
                hi = mid
        return lo

    async def eth_call(
        self, network_id: str, to: str, data: str, block: str = "latest",
    ) -> str | None:
        """نداء دالّة قراءة. يعيد None عند الارتداد بدل أن يُسقط الدورة.

        الارتداد **متوقّع** لا شاذّ: نسأل `owner()` عقداً قد لا يملكها، والدالّة
        الغائبة ترتدّ. فالفشل هنا جوابٌ («لا هذه الدالّة») لا خطأ.

        لكنّ الكتم (429) ليس جواباً: ابتلاعه يكتب «لا مالك لهذا العقد» وهي
        معلومة كاذبة تُخزَّن كأنّها مقيسة ⇒ يُرفع ليصير الصفّ خطأً يُعاد.
        """
        try:
            result = await self._call(
                network_id, "eth_call", [{"to": to, "data": data}, block],
            )
        except (EVMRateLimit, EVMLogLimit):
            raise
        except EVMRPCError:
            return None
        return result if isinstance(result, str) else None

    async def get_logs(
        self,
        network_id: str,
        addresses: Sequence[str],
        from_block: int,
        to_block: int,
        topics: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """`eth_getLogs` نداءً واحداً. يرفع `EVMLogLimit` إن قصّت العقدة."""
        params = [{
            "fromBlock": hex(int(from_block)),
            "toBlock": hex(int(to_block)),
            "address": [a.lower() for a in addresses],
            "topics": list(topics) if topics is not None else [TRANSFER_TOPIC],
        }]
        result = await self._call(network_id, "eth_getLogs", params)
        if not isinstance(result, list):
            raise EVMRPCError(f"eth_getLogs [{network_id}]: ردّ ليس قائمة")
        # بعض العقد تعيد 200 بقائمة مقصوصة عند السقف بلا كتلة `error` ⇒ الوصول
        # إلى العدد الحدّ بالضبط يُعامَل قصّاً. نصف مدى زائد أرخص من دفتر ناقص.
        if len(result) >= config.EVM_LOG_LIMIT:
            raise EVMLogLimit(
                f"eth_getLogs [{network_id}]: {len(result)} سجلّاً ⇒ السقف بلغ"
            )
        return [r for r in result if isinstance(r, dict)]

    async def get_logs_multi(
        self,
        network_id: str,
        addresses: Sequence[str],
        ranges: Sequence[tuple[int, int]],
        topics: Sequence[Any] | None = None,
    ) -> list[list[dict[str, Any]] | EVMRPCError]:
        """عدّة `eth_getLogs` في **طلب HTTP واحد** (دفعة JSON-RPC 2.0).

        هذا هو المخرج من سقوف المدى بلا تحايل: السقف حدّ للنداء الواحد لا للطلب،
        فحزم عشرة نداءات مقبولة في طلب واحد شرعيٌّ في المعيار نفسه ويضاعف الغلّة.
        مقيس 2026-08-19: Base 5×10,000 كتلة في 1.16ث (مقابل 0.88ث لنداء واحد)،
        وMonad 100×100 كتلة = 10,000 كتلة في طلب واحد بينما سقف نداءه 100.

        ويعيد قائمةً **مصفوفةً على `ranges`**: لكل مدًى إمّا سجلّاته أو استثناؤه.
        الرفع عند أوّل مدًى فاشل يرمي تسع نتائج ناجحة دُفع ثمن طلبها — والدفعة
        تخلط النجاح والفشل في ردٍّ واحد بطبيعتها. أمّا فشل **الطلب** نفسه (كتم أو
        حدّ حجم) فيُرفَع: لا نتيجة فيه تُنقَذ.
        """
        url = self.url_for(network_id)
        label = f"eth_getLogs×{len(ranges)} [{network_id}]"
        topic_filter = list(topics) if topics is not None else [TRANSFER_TOPIC]
        addrs = [a.lower() for a in addresses]
        payload = [
            {
                "jsonrpc": "2.0", "id": index, "method": "eth_getLogs",
                "params": [{
                    "fromBlock": hex(int(lo)), "toBlock": hex(int(hi)),
                    "address": addrs, "topics": topic_filter,
                }],
            }
            for index, (lo, hi) in enumerate(ranges)
        ]
        try:
            resp = await self._client.post(url, json=payload)
            status = resp.status_code
            if status == 429:
                raise EVMRateLimit(f"{label} HTTP 429")
            if status >= 500:
                raise EVMRateLimit(f"{label} HTTP {status}")
            if status >= 400:
                # 413 على دفعة مبهم: أهو مدًى واسع أم ردٌّ ثقيل؟ يُقرأ **دفعةً
                # ثقيلة** لأنّ تقليص العدد تقدّمٌ مضمون في الحالتين، وعند العدد
                # واحداً يصعّده المستدعي إلى قسمةِ مدًى. والعكس ليس صحيحاً:
                # قسمة المدى لحلّ مشكلة حجمٍ تهدر السقف المتاح كلّه.
                raise EVMBatchLimit(f"{label} HTTP {status}: {resp.text[:150]}")
            body = resp.json()
        except EVMRPCError:
            raise
        except httpx.TransportError as exc:
            raise EVMRateLimit(f"{label} {type(exc).__name__}") from None
        except Exception as exc:  # noqa: BLE001
            raise EVMRPCError(f"{label} {type(exc).__name__}: {exc}") from None
        if isinstance(body, dict):
            # ردّ مفرد على طلب مصفوف: خطأٌ يخصّ الطلب كلّه لا مدًى فيه.
            err = body.get("error")
            code = err.get("code") if isinstance(err, dict) else None
            msg = str(err.get("message") if isinstance(err, dict) else err)
            raise _classify(label, code, msg)
        if not isinstance(body, list):
            raise EVMRPCError(f"{label}: ردّ ليس مصفوفاً ({type(body).__name__})")
        by_id: dict[int, dict[str, Any]] = {}
        for item in body:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                by_id[item["id"]] = item
        out: list[list[dict[str, Any]] | EVMRPCError] = []
        for index in range(len(ranges)):
            item = by_id.get(index)
            if item is None:
                out.append(EVMRPCError(f"{label}: لا ردّ للنداء {index}"))
                continue
            err = item.get("error")
            if err is not None:
                code = err.get("code") if isinstance(err, dict) else None
                msg = str(err.get("message") if isinstance(err, dict) else err)
                out.append(_classify(label, code, msg))
                continue
            result = item.get("result")
            if not isinstance(result, list):
                out.append(EVMRPCError(f"{label}: نداء {index} بلا نتيجة"))
                continue
            if len(result) >= config.EVM_LOG_LIMIT:
                out.append(EVMLogLimit(f"{label}: {len(result)} سجلّاً ⇒ السقف بلغ"))
                continue
            out.append([row for row in result if isinstance(row, dict)])
        return out

    async def first_mint_block(
        self, network_id: str, address: str, head: int,
    ) -> int | None:
        """أوّل كتلة سُكَّ فيها هذا العقد — **نداء واحد** على المدى `0→head`.

        بديلُ `contract_creation_block` حيث لا أرشيف: عقدة روبن‑هود تحفظ الحالة
        ~128 كتلة فقط فالبحث الثنائي على `eth_getCode` مستحيل هناك، وكان البديل
        الوحيد المشيَ من الكتلة صفر. والفرق مقيس 2026-08-19: أربع عملات مراقَبة
        سُكَّت عند 0.2% و67.7% و88.6% و93.6% من السلسلة، أي 27–37 **مليون** كتلة
        فارغة تُمشى قبل أوّل تحويل — والمسح يجيب في 0.17 ثانية.

        والحيلة أنّ المرشّح بموضوعين يقصّ الردّ إلى المِنح وحدها (مِنحة واحدة في
        العملات الأربع) فلا يبلغ سقف الـ10,000 وإن كانت العملة صاخبة.

        يعيد `None` حين لا يُعرَف الجواب، وحينها **يجب** أن يبدأ المستدعي من
        الصفر لا أن يخمّن: عملة تسكّ باستمرار قد تقصّ الردّ (`EVMLogLimit`) فيصير
        أدنى ما رأيناه أعلى من الحقيقة، وأدنى خاطئ يعني حائزاً استلم قبله فيظهر
        رصيده سالباً — أي عملة تُرفض كلّها. والكتم يُرفَع لا يُبتلَع: «لم أستطع
        السؤال» ليس «لا سكّ قبل هذه الكتلة».
        """
        try:
            logs = await self.get_logs(
                network_id, [address], 0, int(head),
                topics=[TRANSFER_TOPIC, ZERO_TOPIC],
            )
        except EVMLogLimit:
            return None
        blocks = [
            block for block in (_num(log.get("blockNumber")) for log in logs)
            if block is not None
        ]
        return min(blocks) if blocks else None

    async def get_logs_paged(
        self,
        network_id: str,
        addresses: Sequence[str],
        from_block: int,
        to_block: int,
        topics: Sequence[Any] | None = None,
        max_calls: int | None = None,
        sleep=asyncio.sleep,
        deadline: float | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool, int]:
        """نفس النداء مع تقسيم المدى عند القصّ وحزم المدود المقبولة في دفعة.

        يعيد (السجلّات، عدد **الطلبات**، أُكمِل المدى كلّه؟، أوّل كتلة غير مقروءة).
        الرابع هو نقطة الاستئناف: عند الإكمال يساوي `to_block`، وعند بلوغ السقف
        يساوي أدنى كتلة بقيت في المكدّس — فيُستأنف من حيث توقّف بدل أن يُعاد المدى
        كلّه أو يُعلَن مكتملاً كذباً. والعدد صار عدد **طلبات HTTP** لا نداءات:
        الحصّة المقيسة على العقد العامّة حصّةُ طلبات (تسعة في ~15ث على Base)،
        فالدفعة تشتري عشرة أضعاف المدى بنفس الثمن.

        و**العقد الذي لا يُخَرق**: كل سجلّ مُعاد كتلته أدنى من نقطة الاستئناف. من
        دونه يُطبَّق سجلٌّ ثمّ يُقرأ مرّة أخرى في الدورة القادمة ⇒ مضاعفة رصيد،
        أو يتقدّم المؤشّر فوق مدًى لم يُقرأ ⇒ ثغرة دائمة. والدفعة تهدّده مباشرة:
        عشرة مدود في طلب واحد قد يفشل أقدمها وينجح أحدثها، فالأحدث يُعاد إلى
        المكدّس **وتُهمَل سجلّاته** ولو دُفع ثمنها — طلبٌ زائد أرخص من ثغرة.

        القسمة تكيّفيّة لا خطوة ثابتة: العملة الهادئة مقيسة بنداء واحد لـ1.71
        مليون كتلة، والصاخبة تقصّ عند 10,000 — وخطوة ثابتة تعني إمّا مئات
        النداءات للهادئة أو قصّاً للصاخبة. والسقف `max_calls` يمنع عملةً واحدة
        من أكل دقيقة الدورة كلّها.

        و`deadline` (لحظة `monotonic`) سقفٌ **بالزمن** لا بالعدد، وهو ما يلزم
        فعلاً: نداء واحد يعلَق حتى `EVM_TIMEOUT_SECONDS` (25ث) فعشرون نداءً قد
        تكون ثانيتين أو ثماني دقائق — والعدد لا يفرّق. مقيس على الدورة الحيّة
        الثانية: 72 نداءً في 118 ثانية بينما الفترة 60. والتوقّف هنا **ليس
        خسارة**: الخروج ناقصاً مع نقطة استئناف هو نفس مسار السقف، فتُكمِل الدورة
        القادمة من حيث توقّفنا. ويُسمح دائماً بطلب واحد ولو انتهت الميزانية،
        كي لا تدور العملة بلا تقدّم أبداً.
        """
        cap = config.EVM_BACKFILL_MAX_CALLS if max_calls is None else max_calls
        net = str(network_id)
        hint = int(config.EVM_LOG_RANGE_HINT.get(net, 0))
        # حجم الدفعة يبدأ من السقف المقيس للشبكة ثمّ **يتقلّص ولا يعود**: أكبر
        # دفعة مقبولة صفةُ عملةٍ لا صفةُ شبكة (قِيست 10 و5 و3 و2 و1 لخمس عملات
        # Base)، فالتعلّم داخل النداء الواحد أصدق من ثابت في الملفّ. والعودة إلى
        # الأعلى تعني رفضاً جديداً كل بضعة طلبات، أي ثمناً بلا مقابل.
        batch = max(1, int(config.EVM_BATCH_SIZE.get(net, 1)))
        out: list[dict[str, Any]] = []
        calls = 0
        # مكدّس مدى‑واحد بدل استدعاء ذاتيّ: العمق قد يبلغ 20 مستوًى على مدى
        # مليون كتلة، والمكدّس يجعل احترام `cap` سطراً واحداً. وثابتُه أنّ قراءته
        # من القمّة إلى القاع **تصاعديّة**، وهو ما يجعل قمّته نقطةَ الاستئناف.
        pending: list[tuple[int, int]] = [(int(from_block), int(to_block))]
        while pending:
            spent = (
                calls > 0 and deadline is not None and time.monotonic() >= deadline
            )
            if calls >= cap or spent:
                # ما بقي في المكدّس غير مقروء ⇒ المدى غير مكتمل.
                return (
                    _in_block_order(out), calls, False,
                    min(lo for lo, _ in pending),
                )
            taken: list[tuple[int, int]] = []
            while pending and len(taken) < batch:
                lo, hi = pending.pop()
                if lo <= hi:
                    taken.append((lo, hi))
            if not taken:
                continue
            if calls:
                await sleep(
                    config.EVM_BATCH_PACING_SECONDS if len(taken) > 1
                    else config.EVM_PACING_SECONDS
                )
            calls += 1
            if len(taken) == 1:
                lo, hi = taken[0]
                try:
                    results: list[Any] = [
                        await self.get_logs(net, addresses, lo, hi, topics)
                    ]
                except EVMRPCError as exc:
                    results = [exc]
            else:
                try:
                    results = list(
                        await self.get_logs_multi(net, addresses, taken, topics)
                    )
                except EVMRPCError as exc:
                    results = [exc] * len(taken)

            fresh: list[tuple[int, int, list[dict[str, Any]]]] = []
            requeue: list[tuple[int, int]] = []
            waited = False
            shrink = False
            # `strict`: ردٌّ واحد لكل مدى بحكم البناء، واختلافُ الطول يعني
            # ازدواج ردٍّ على مدى غير مداه — دفترٌ كاذب لا استثناءٌ صاخب.
            for (lo, hi), result in zip(taken, results, strict=True):
                if isinstance(result, list):
                    fresh.append((lo, hi, result))
                    continue
                if isinstance(result, EVMRateLimit):
                    # الكتم يُنتظَر ولا يُقسَم: القسمة تضاعف النداءات فتزيد الكتم.
                    # والمدى يُعاد إلى المكدّس كما هو — تسليمُه هنا يعني ثغرةً في
                    # الدفتر لا تأخيراً. والسقف `cap` هو ما يمنع الحلقة من الدوران.
                    requeue.append((lo, hi))
                    waited = True
                    continue
                if isinstance(result, EVMBatchLimit) and len(taken) > 1:
                    requeue.append((lo, hi))
                    shrink = True
                    continue
                if isinstance(result, (EVMLogLimit, EVMBatchLimit)):
                    if lo >= hi:
                        # كتلة واحدة تفوق السقف — لا قسمة ممكنة. يُرفع الاستثناء
                        # بدل أن تُعلَق الطبقة إلى الأبد على كتلة واحدة.
                        raise result
                    requeue.extend(_split_range(lo, hi, hint))
                    continue
                raise result

            floor = min((lo for lo, _ in requeue), default=None)
            for lo, hi, logs in fresh:
                if floor is not None and lo > floor:
                    # نجح هذا المدى لكنّ مدًى **أقدم منه** في نفس الدفعة لم ينجح.
                    # الاحتفاظ بسجلّاته يعني إمّا إعادة قراءتها لاحقاً (مضاعفة
                    # رصيد) أو تقديم الاستئناف فوق المدى الفاشل (ثغرة دائمة).
                    # فتُهمَل ويُعاد المدى: طلبٌ زائد أرخص من دفترٍ كاذب.
                    requeue.append((lo, hi))
                else:
                    out.extend(logs)
            if shrink:
                batch = max(1, batch // 2)
            requeue.sort()
            pending.extend(reversed(requeue))
            if waited:
                await sleep(config.EVM_RATE_LIMIT_BACKOFF_SECONDS)
        return _in_block_order(out), calls, True, int(to_block)


def decode_transfer(log: dict[str, Any]) -> dict[str, Any] | None:
    """يفكّ سجلّ `Transfer` إلى (العملة، من، إلى، القيمة، الكتلة).

    القيمة في `data` لا في المواضيع (غير مفهرسة في ERC-20 القياسيّ)، والعنوانان
    في `topics[1]` و`topics[2]`. سجلّ بمواضيع أقلّ من ثلاثة ليس Transfer قياسيّاً
    (بعض العقود تُطلق حدثاً بنفس التوقيع وحقول مختلفة) ⇒ يُهمَل ولا يُخمَّن.
    """
    topics = log.get("topics")
    if (
        not isinstance(topics, list)
        or len(topics) != 3
        or not isinstance(topics[0], str)
        or topics[0].lower() != TRANSFER_TOPIC
    ):
        return None
    sender = _topic_address(topics[1])
    recipient = _topic_address(topics[2])
    value = _uint256(log.get("data"))
    block = _num(log.get("blockNumber"))
    token = _evm_address(log.get("address"))
    if (
        sender is None or recipient is None or value is None
        or block is None or block < 0 or token is None
    ):
        return None
    return {
        "token_address": token,
        "from": sender,
        "to": recipient,
        "value": value,
        "block": block,
    }
