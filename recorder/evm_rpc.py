# -*- coding: utf-8 -*-
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
from typing import Any, Sequence

import httpx

import config

# توقيع حدث `Transfer(address,address,uint256)` — keccak-256 لنصّ التوقيع.
# هذا **الطريق الوحيد** إلى قائمة حائزين على EVM: معيار ERC-20 لا يخزّن القائمة
# فلا نداء عقدة يعيدها، والرصيد لا يُعرف إلّا بإعادة تشغيل كل تحويل.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

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
        except Exception as exc:  # noqa: BLE001
            raise EVMRPCError(f"{method} [{network_id}] {type(exc).__name__}: {exc}") from None
        if not isinstance(body, dict):
            raise EVMRPCError(f"{method} [{network_id}]: ردّ غير متوقّع ({type(body).__name__})")
        err = body.get("error")
        if err is not None:
            code = err.get("code") if isinstance(err, dict) else None
            msg = str(err.get("message") if isinstance(err, dict) else err)
            low = msg.lower()
            if (
                code in (429, -32011) or "too many requests" in low
                or "rate limit" in low or "no backend" in low
                or "try again" in low
            ):
                raise EVMRateLimit(f"{method} [{network_id}]: {msg[:150]}")
            # العقد تصوغ «المدى أوسع من طاقتي» بعبارات مختلفة (`exceeds limit of
            # 10000`، `query returned more than`، `limit exceeded`، `limited to a
            # 10,000 range`، و**مهلة الاستعلام**) وكلّها تعني شيئاً واحداً:
            # قسّم المدى.
            if (
                code == -32614
                or "exceed" in low or "more than" in low or "too many" in low
                or "timed out" in low or "timeout" in low or "limited to a" in low
            ):
                raise EVMLogLimit(f"{method} [{network_id}]: {msg[:150]}")
            raise EVMRPCError(f"{method} [{network_id}]: JSON-RPC {code}: {msg[:150]}")
        if "result" not in body:
            raise EVMRPCError(f"{method} [{network_id}]: لا result ولا error")
        return body["result"]

    async def block_number(self, network_id: str) -> int:
        block = _num(await self._call(network_id, "eth_blockNumber", []))
        if block is None:
            raise EVMRPCError(f"eth_blockNumber [{network_id}]: رقم كتلة غير مفهوم")
        return block

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
        """نفس النداء مع تقسيم المدى نصفين عند القصّ.

        يعيد (السجلّات، عدد النداءات، أُكمِل المدى كلّه؟، أوّل كتلة غير مقروءة).
        الرابع هو نقطة الاستئناف: عند الإكمال يساوي `to_block`، وعند بلوغ سقف
        النداءات يساوي أدنى كتلة بقيت في المكدّس — فيُستأنف من حيث توقّف بدل أن
        يُعاد المدى كلّه أو يُعلَن مكتملاً كذباً.

        القسمة تكيّفيّة لا خطوة ثابتة: العملة الهادئة مقيسة بنداء واحد لـ1.71
        مليون كتلة، والصاخبة تقصّ عند 10,000 — وخطوة ثابتة تعني إمّا مئات
        النداءات للهادئة أو قصّاً للصاخبة. والسقف `max_calls` يمنع عملةً واحدة
        من أكل دقيقة الدورة كلّها.

        و`deadline` (لحظة `monotonic`) سقفٌ **بالزمن** لا بالعدد، وهو ما يلزم
        فعلاً: نداء واحد يعلَق حتى `EVM_TIMEOUT_SECONDS` (25ث) فعشرون نداءً قد
        تكون ثانيتين أو ثماني دقائق — والعدد لا يفرّق. مقيس على الدورة الحيّة
        الثانية: 72 نداءً في 118 ثانية بينما الفترة 60. والتوقّف هنا **ليس
        خسارة**: الخروج ناقصاً مع نقطة استئناف هو نفس مسار السقف، فتُكمِل الدورة
        القادمة من حيث توقّفنا. ويُسمح دائماً بنداء واحد ولو انتهت الميزانية،
        كي لا تدور العملة بلا تقدّم أبداً.
        """
        cap = config.EVM_BACKFILL_MAX_CALLS if max_calls is None else max_calls
        out: list[dict[str, Any]] = []
        calls = 0
        # مكدّس مدى‑واحد بدل استدعاء ذاتيّ: العمق قد يبلغ 20 مستوًى على مدى
        # مليون كتلة، والمكدّس يجعل احترام `cap` سطراً واحداً.
        pending: list[tuple[int, int]] = [(int(from_block), int(to_block))]
        while pending:
            spent = (
                calls > 0 and deadline is not None and time.monotonic() >= deadline
            )
            if calls >= cap or spent:
                # ما بقي في المكدّس غير مقروء ⇒ المدى غير مكتمل.
                return out, calls, False, min(lo for lo, _ in pending)
            lo, hi = pending.pop()
            if lo > hi:
                continue
            if calls:
                await sleep(config.EVM_PACING_SECONDS)
            calls += 1
            try:
                out.extend(await self.get_logs(network_id, addresses, lo, hi, topics))
            except EVMRateLimit:
                # الكتم يُنتظَر ولا يُقسَم: القسمة تضاعف النداءات فتزيد الكتم.
                # والمدى يُعاد إلى المكدّس كما هو — تسليمُه هنا يعني ثغرةً في
                # الدفتر لا تأخيراً. والسقف `cap` هو ما يمنع الحلقة من الدوران.
                pending.append((lo, hi))
                await sleep(config.EVM_RATE_LIMIT_BACKOFF_SECONDS)
                continue
            except EVMLogLimit:
                if lo >= hi:
                    # كتلة واحدة تفوق السقف — لا قسمة ممكنة. يُرفع الاستثناء بدل
                    # أن تُعلَق الطبقة إلى الأبد على كتلة واحدة.
                    raise
                # بعض العقد تفرض سقف مدى ثابتاً (Base: 10,000 كتلة، Monad:
                # 100 كتلة). التنصيف وحده يهدر 20+ نداءً قبل بلوغ السقف على
                # مدى genesis؛ بعد أول رفض نتعلم السقف الأعلى المقبول لهذه
                # الشبكة ونقسم مباشرةً إلى شرائح أقدم-أولاً.
                hint = int(config.EVM_LOG_RANGE_HINT.get(str(network_id), 0))
                if hint > 0:
                    # بعد أول رفض لا نعيد تجربة بقايا المدى الكبير مراراً؛ نحوّله
                    # دفعةً واحدة إلى شرائح معلومة القبول، مع دفع الأحدث أولاً
                    # كي يسحب المكدّس الأقدم أولاً.
                    chunks = [
                        (start, min(hi, start + hint - 1))
                        for start in range(lo, hi + 1, hint)
                    ]
                    pending.extend(reversed(chunks))
                    continue
                mid = lo + (hi - lo) // 2
                # الأقدم يُقرأ أوّلاً (آخر ما يُدفَع أوّل ما يُسحَب): الترتيب
                # الزمنيّ شرط صحّة الرصيد حين يُثبَّت عند صفر.
                pending.append((mid + 1, hi))
                pending.append((lo, mid))
        return out, calls, True, int(to_block)


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
