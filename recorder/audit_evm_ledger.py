"""تدقيقُ دفترِ EVM في مواجهة حقيقةِ السلسلة — لا في مواجهة نفسه.

دفترُ الأرصدة الذي نبنيه من سجلّاتِ `Transfer` **تراكميّ**: كلُّ لقطةٍ تعتمد على
كلِّ ما قبلها، فسجلٌّ واحد ضائع يُفسد كلَّ اللقطات التالية بهدوءٍ ولا يتركُ أثراً
في صفٍّ ولا سجلّ. وهذه هي المصيدةُ التي أوقعنا فيها مزوّدٌ حُذف (ردٌّ ناجحٌ لكنّه
مقصوصٌ عند الصفحة الأولى بلا أيّ علامةِ خطأ). ولا يكشفُها إلّا سؤالُ السلسلة
نفسِها: **ما رصيدُ هذا الحائز عند تلك الكتلة؟**

وهذا السؤالُ يحتاج **حالةً أرشيفيّة**، وعقدُنا العامّة لا تملكها: عقدةُ روبن‑هود
عمقُها ~128 كتلة (`metadata is not found`)، وعقدةُ BSC العامّة تطلب رمزاً
(`Archive requests require a personal token`). ولهذا وحدَه تُستعمل هنا مفاتيحُ
المزوّدين: لا لتوسيع الدفتر بل لتدقيقه. ومقيسٌ 2026‑08‑19 أنّ أرشيف Alchemy يخدم
الشبكات الأربع، وأنّ سقفَ `eth_getLogs` عنده عشرُ كتلٍ — فلا يصلح لبناء الدفتر،
ويصلح تماماً لـ`eth_call` عند كتلةٍ ماضية.

وطبقةُ EVM الحيّة تبقى بلا مفتاح (FR‑012): هذه أداةٌ يدويّةٌ منفصلة، لا تكتب في
القاعدة حرفاً، ولا تستورد `evm_rpc` كي لا يتسلّل مفتاحٌ إلى مسارٍ صُمّم كي لا
يحمل واحداً.

ثلاثةُ أسئلةٍ في كلّ لقطة، وكلٌّ منها يكشف عطباً مختلفاً:

1. **الكتلة** — أعلى كتلةٍ طابعُها ≤ `recorded_at`. تُوجَد بطوابعِ السلسلة لا
   بمرساةٍ عندنا (المراسي قوسٌ أوّليٌّ مجّانيّ، ثمّ يُبحَث داخلَه بطوابعَ طازجة)،
   فصحّتُها **مُبرهَنة** لا مُفترَضة: نرى بأعيننا أنّ التي تليها أحدثُ من الوقت.

2. **الاكتمال** — وهذه هي التي تُصطاد بها المصيدة. `supply` في الدفتر مجموعُ
   الأرصدةِ الحيّة لا `totalSupply()` من العقد (تعريفٌ مقصود: النِّسب على ما يمكن
   بيعُه). فإن كان الدفترُ قد رأى **كلَّ** تحويل تكون المتطابقة:
       `supply_base + رصيدُ عناوين الحرق == totalSupply()`
   ودفترٌ مقصوصٌ يُنقص الطرفَ الأيسر ولا يمسّ الأيمن — فالفجوةُ هي بالضبط ما ضاع.

3. **الأرصدة** — `balanceOf(h)` لأكبر K حائزاً، بالوحدة الأساسيّة بلا قسمةٍ على
   `decimals` (لا خطأَ تقريبٍ يُخلط بخطأِ دفتر)، ثمّ تُعاد حسبةُ `top1/5/10/20`
   وتُقارَن بالمخزّن بنفس المقام الذي حُسبت به.

وما **لا** يُدقَّق، صراحةً: `holder_count`. النداءُ يُخبرنا برصيدِ عنوانٍ نسأل عنه،
ولا سبيلَ به إلى عنوانٍ لم يعرفه دفترُنا أصلاً. لكنّ سؤالَ الاكتمالِ يسدّ الثغرة
من جهةِ المال: حائزٌ مجهولٌ يحمل رصيداً يظهر فجوةً في المتطابقة، وحائزٌ مجهولٌ
برصيدِ صفرٍ لا يغيّر نسبةً ولا تركّزاً.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import db as db_module  # noqa: E402
import evm_contract  # noqa: E402
from provider_keys import read_keys  # noqa: E402

# مسارُ الأرشيف لكلّ شبكة، مرتّباً بالأفضليّة. **مقيسٌ 2026‑08‑19 لا مفترض**:
#   Alchemy: أرشيفٌ عامل على الشبكات الأربع (`eth_call` عند الرأس−3م ✓).
#   dRPC   : أرشيفُ Base ✓، وأرشيفُ BSC ردَّ خطأً داخليّاً مرّتين ⇒ لا يُعتمد
#            هناك، وشريحةُ روبن‑هود عنده تُجيب `eth_chainId` ثمّ ترفض ما بعده.
# فلا يُدرَج مزوّدٌ «يُفترض» أنّه يخدم شبكة: البديلُ حيث قِيس أنّه يعمل فقط.
ARCHIVE_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "4663": (("alchemy", "https://robinhood-mainnet.g.alchemy.com/v2/{key}"),),
    "8453": (
        ("alchemy", "https://base-mainnet.g.alchemy.com/v2/{key}"),
        ("drpc", "https://lb.drpc.org/ogrpc?network=base&dkey={key}"),
    ),
    "143": (("alchemy", "https://monad-mainnet.g.alchemy.com/v2/{key}"),),
    "56": (("alchemy", "https://bnb-mainnet.g.alchemy.com/v2/{key}"),),
}
# الأسماءُ الثلاثة كما في `key_file.PROVIDERS` — واختبارُ الحرس يُطابقها بالنصّ،
# فأيُّ اختلافٍ هنا يسقط هناك لا في الإنتاج.
PROVIDER_FIELDS = {
    "alchemy": ("alchemy_api_keys", "alchemy_api_key", "ALCHEMY_API_KEY"),
    "drpc": ("drpc_api_keys", "drpc_api_key", "DRPC_API_KEY"),
}
TOTAL_SUPPLY = evm_contract.selector("totalSupply()")
BALANCE_OF = evm_contract.selector("balanceOf(address)")
BURN_ADDRESSES = (
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
)
# انزياحُ كتلةٍ واحدةٍ عند الحدّ يحرّك رصيداً، فليس كلُّ فارقٍ عطبَ دفتر. والحدُّ
# بنقاطٍ مئويّةٍ من المعروض لأنّه وحدةُ ما نخزّنه فعلاً (`top1_pct`): ما دون
# العُشر يُسمّى انزياحاً، وما فوقه تفاوتاً يستحقّ النظر.
DRIFT_POINTS = 0.1


class ArchiveError(RuntimeError):
    """فشلُ نداءٍ أرشيفيّ — برسالةٍ نُظّفت من المفتاح قبل أن تُرفَع."""


class ArchiveRPC:
    """عميلٌ صغيرٌ لعقدةٍ أرشيفيّةٍ بمفتاح — قراءةً فقط، ولا يطبع مفتاحاً أبداً.

    لا يُعاد استخدام `evm_rpc.EVMRPC` عن قصد: هي بلا مفتاحٍ بحكم التصميم، وتمريرُ
    عنوانٍ يحمل مفتاحاً إليها يجعل السرَّ يمرّ في سجلّاتها ومهلاتها ورسائل كتمها —
    وكلُّها مواضعُ لم تُكتَب وهي تحمي سرّاً. فالفصلُ هنا خطُّ الدفاع لا تكراراً،
    وثمنُه ثلاثون سطراً.
    """

    def __init__(self, url: str, secret: str, timeout: float = 30.0) -> None:
        self._url = url
        self._secret = secret
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)
        self.calls = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ArchiveRPC:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def hide(self, text: object) -> str:
        """كلُّ نصٍّ يخرج من هنا يمرّ عليها.

        رسالةُ الخطأ تحمل الرابطَ عادةً، والرابطُ يحمل المفتاح: فالكتمُ عند
        **التكوين** لا عند الطباعة، كي لا يوجد طريقٌ ثانٍ يفوته.
        """
        clean = str(text).replace(self._secret, "«مفتاح»")
        return " ".join(clean.split())[:160]

    def _call(self, method: str, params: list) -> object:
        self.calls += 1
        try:
            response = self._client.post(
                self._url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ArchiveError(self.hide(f"{method}: {type(exc).__name__}")) from None
        if not isinstance(body, dict):
            raise ArchiveError(self.hide(f"{method}: ردٌّ ليس كائناً"))
        if body.get("error"):
            raise ArchiveError(self.hide(f"{method}: {body['error']}"))
        return body.get("result")

    def block_number(self) -> int:
        return int(str(self._call("eth_blockNumber", [])), 16)

    def block_timestamp(self, block: int) -> int:
        result = self._call("eth_getBlockByNumber", [hex(int(block)), False])
        if not isinstance(result, dict) or "timestamp" not in result:
            raise ArchiveError(f"eth_getBlockByNumber: لا كتلةَ عند {block}")
        return int(str(result["timestamp"]), 16)

    def read_uint(self, to: str, data: str, block: int) -> int:
        """`eth_call` يعيد عدداً. والارتدادُ هنا **خطأٌ** لا جواب.

        في الطبقة الحيّة يكون ارتدادُ `owner()` جواباً («لا مالك»)، أمّا هنا فنحن
        نسأل عقدَ ERC‑20 نعرف أنّه كذلك: الارتدادُ يعني أنّ العقدَ لم يكن موجوداً
        عند تلك الكتلة، أو أنّ العقدةَ بلا أرشيفٍ لذلك العمق. وكلاهما نتيجةُ
        تدقيقٍ تُقال، لا تُبتلَع لتصير صفراً يظهر «مطابقاً».
        """
        result = self._call("eth_call", [{"to": to, "data": data}, hex(int(block))])
        text = str(result or "")
        if not text.startswith("0x") or len(text) < 3:
            raise ArchiveError(f"eth_call: ردٌّ غير عدديّ عند الكتلة {block}")
        return int(text, 16)

    def balance_of(self, token: str, holder: str, block: int) -> int:
        padded = holder.lower().removeprefix("0x").rjust(64, "0")
        return self.read_uint(token, BALANCE_OF + padded, block)

    def total_supply(self, token: str, block: int) -> int:
        return self.read_uint(token, TOTAL_SUPPLY, block)


def open_route(
    network_id: str, provider: str | None = None, timeout: float = 30.0,
) -> tuple[str, ArchiveRPC]:
    """(اسمُ المزوّد، عميلٌ مفتوح) لأوّل مسارٍ له مفتاحٌ على القرص.

    الترتيبُ في `ARCHIVE_ROUTES` أفضليّةٌ لا تفضيل: الأوّل هو الأوثقُ مقيساً على
    تلك الشبكة، والثاني احتياطٌ إن غاب مفتاحُ الأوّل أو سقطت خدمتُه. والمفاتيح
    تُقرأ عبر `read_keys` وحدَها (تصفيةُ `enabled` + تقدّمُ متغيّر البيئة)، فمفتاحٌ
    أوقفه المشغّل من اللوحة لا يُنادى به هنا أيضاً.
    """
    routes = ARCHIVE_ROUTES.get(str(network_id), ())
    if not routes:
        raise ArchiveError(f"لا مسارَ أرشيفٍ معروفاً لشبكة {network_id}")
    wanted = [(name, url) for name, url in routes if not provider or name == provider]
    if not wanted:
        raise ArchiveError(f"المزوّد {provider} ليس مساراً لشبكة {network_id}")
    for name, pattern in wanted:
        plural, singular, env = PROVIDER_FIELDS[name]
        keys = read_keys(plural, singular, env)
        if keys:
            return name, ArchiveRPC(pattern.format(key=keys[0]), keys[0], timeout)
    names = ", ".join(name for name, _ in wanted)
    raise ArchiveError(f"لا مفتاحَ مفعَّلاً لشبكة {network_id} — مطلوبٌ أحدُ: {names}")


def bracket_from_anchors(
    db: db_module.RecorderDB, network_id: str, target_ts: int, head: int,
) -> tuple[int, int]:
    """قوسٌ أوّليّ [أدنى، أعلى] من مراسينا المخزّنة — مجّانيٌّ ويقصّر البحث.

    المراسي طوابعُ كتلٍ حقيقيّة قُرئت من السلسلة وحُفظت (`evm_block_time`)، فهي
    حقيقةٌ مخزَّنة لا تقدير. لكنّها متباعدةٌ (كلّ 18,000 كتلة) فلا تُجيب وحدَها:
    تُعطي القوسَ ويُبحَث داخلَه بطوابعَ طازجة. وإن لم توجد مرساةٌ مناسبة فالقوسُ
    كلُّ السلسلة — أبطأُ بنداءاتٍ معدودة، وصحيحٌ سواءً. ولا يُقصَر القوسُ بمرساةٍ
    أبداً بلا التحقّق منها لاحقاً: البحثُ يقرأ طرفيه من السلسلة قبل أن يثق بهما.
    """
    low, high = 0, int(head)
    for block, stamp in db.block_anchors(str(network_id)):
        if int(stamp) <= target_ts and int(block) > low:
            low = int(block)
        elif int(stamp) > target_ts and int(block) < high:
            high = int(block)
    return low, max(high, low + 1)


def boundary_block(rpc: ArchiveRPC, target_ts: int, low: int, high: int) -> int:
    """أعلى كتلةٍ طابعُها ≤ الوقت المطلوب، داخل القوس المعطى.

    استقراءٌ ثمّ تنصيف: زمنُ الكتلة شبهُ ثابتٍ فالاستقراءُ يقع قريباً في نداءين أو
    ثلاثة، لكنّه قد يعلَق (يستكشف الكتلةَ نفسَها مرّتين) فالتنصيفُ يضمن الانتهاء.
    والجوابُ محدَّدٌ لا مقارَب: نخرج حين يتلاصقُ الطرفان، أي حين نكون قد قرأنا
    بأنفسِنا أنّ الكتلةَ التي تليه أحدثُ من المطلوب.
    """
    # الطرفُ الأدنى يُقرأ ولو كان الكتلةَ صفراً: النشأةُ كتلةٌ حقيقيّةٌ لها طابعٌ
    # حقيقيّ، وافتراضُ صفرٍ مكانَه يُفسد الاستقراءَ بمقدارِ عمرِ الحقبة كلِّها —
    # فيهبط البحثُ إلى تنصيفٍ محضٍ (خمسٌ وعشرون نداءً بدل ثلاثة).
    low_ts = rpc.block_timestamp(low)
    if low_ts > target_ts:  # مرساةٌ كاذبة ⇒ انزل إلى النشأة وأعِد السؤال
        low = 0
        low_ts = rpc.block_timestamp(low)
        if low_ts > target_ts:
            return 0  # وقتٌ يسبق نشأةَ السلسلة: لا كتلةَ تُسأل
    high_ts = rpc.block_timestamp(high)
    if high_ts <= target_ts:
        return high
    tries, seen = 0, {low, high}
    while high - low > 1:
        probe = 0
        if tries < 4 and high_ts > low_ts:
            span = (target_ts - low_ts) * (high - low) / (high_ts - low_ts)
            probe = min(max(low + max(1, int(span)), low + 1), high - 1)
        if not probe or probe in seen:
            probe = (low + high) // 2
        seen.add(probe)
        tries += 1
        stamp = rpc.block_timestamp(probe)
        if stamp <= target_ts:
            low, low_ts = probe, stamp
        else:
            high, high_ts = probe, stamp
    return low


def _percentages(balances: list[int], supply: int) -> dict[str, float | None]:
    """نِسَبُ أعلى 1/5/10/20 — بنفس تعريف الدفتر ونفس مقامه.

    المقامُ `supply_base` لا `totalSupply()`: نحن نقارن رقماً مخزّناً برقمٍ يُعاد
    حسابُه، فلا يجوز أن يفترق المقام وإلّا صار الفرقُ فرقَ تعريفٍ لا فرقَ بيانات.
    """
    if supply <= 0:
        return {f"top{n}_pct": None for n in (1, 5, 10, 20)}
    ordered = sorted(balances, reverse=True)
    return {
        f"top{n}_pct": sum(ordered[:n]) / supply * 100 for n in (1, 5, 10, 20)
    }


def sample_rows(
    db: db_module.RecorderDB, network_id: str, tokens: int, per_token: int,
    only: tuple[str, ...] = (),
) -> list[dict]:
    """لقطاتٌ موزّعةٌ على عمرِ كلّ عملة، وآخرُها دائماً ضمنها.

    الآخِرةُ ليست واحدةً من عدّة: الخطأُ في دفترٍ تراكميّ يتراكم، فأقصى انحرافٍ
    ممكنٍ يقع في آخرِ لقطة. والباقي موزّعٌ بانتظامٍ لا عشوائيّاً كي يكون تشغيلان
    على نفس القاعدة مقارنَين — أداةُ تدقيقٍ تعطي جواباً مختلفاً كلّ مرّةٍ لا يُبنى
    عليها قرار.
    """
    net = str(network_id)
    if only:
        addresses = [token.lower() for token in only]
    else:
        addresses = [
            row["token_address"] for row in db._conn.execute(
                """SELECT token_address, COUNT(*) AS n FROM chain_concentration
                    WHERE network_id = ? AND is_replay = 1
                    GROUP BY token_address ORDER BY n DESC, token_address LIMIT ?""",
                (net, max(1, int(tokens))),
            ).fetchall()
        ]
    picked: list[dict] = []
    for address in addresses:
        rows = db._conn.execute(
            """SELECT token_address, network_id, recorded_at, supply, decimals,
                      top1_pct, top5_pct, top10_pct, top20_pct, holder_count,
                      top_accounts, raw_json
                 FROM chain_concentration
                WHERE token_address = ? AND network_id = ? AND is_replay = 1
                ORDER BY recorded_at""",
            (address, net),
        ).fetchall()
        if not rows:
            continue
        count = max(1, min(int(per_token), len(rows)))
        step = (len(rows) - 1) / (count - 1) if count > 1 else 0
        wanted = sorted({round(i * step) for i in range(count)} | {len(rows) - 1})
        picked.extend(dict(rows[i]) for i in wanted)
    return picked


def _epoch(stamp: object) -> int:
    """`recorded_at` نصّاً بـISO → ثانيةً منذ الحقبة، والمنطقةُ UTC إن لم تُذكر."""
    text = str(stamp).replace("Z", "+00:00")
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return int(moment.timestamp())


def audit_row(
    rpc: ArchiveRPC, db: db_module.RecorderDB, row: dict, holders: int,
    head: int | None = None,
) -> dict:
    """تدقيقُ لقطةٍ واحدة → سجلٌّ يوصف نفسَه. بلا طباعةٍ وبلا كتابةٍ في القاعدة."""
    payload = db_module.decode_raw(row["raw_json"])
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    payload = payload or {}
    top = [(str(a).lower(), int(b)) for a, b in (payload.get("top") or [])]
    stored_supply = int(payload.get("supply_base") or 0)
    token, net = str(row["token_address"]), str(row["network_id"])
    out: dict = {
        "token": token, "network_id": net, "recorded_at": row["recorded_at"],
        "verdict": "تعذّر", "note": "", "block": None,
        "supply_stored": stored_supply, "supply_chain": None, "burned": None,
        "coverage_pct": None, "holder_count_stored": row["holder_count"],
        "holders_checked": 0, "holders_matched": 0, "worst_holder": None,
        "stored_pct": {f"top{n}_pct": row[f"top{n}_pct"] for n in (1, 5, 10, 20)},
        "chain_pct": {}, "delta_points": None, "calls": 0,
    }
    started = rpc.calls
    try:
        target = _epoch(row["recorded_at"])
        low, high = bracket_from_anchors(db, net, target, head or rpc.block_number())
        block = boundary_block(rpc, target, low, high)
        out["block"] = block
        if not block:
            out["note"] = "لا كتلةَ قبل هذا الوقت على هذه الشبكة"
            return out

        # ١) الاكتمال: `supply_base + المحروق` يجب أن يساوي `totalSupply()` بالضبط.
        supply_chain = rpc.total_supply(token, block)
        burned = sum(rpc.balance_of(token, address, block) for address in BURN_ADDRESSES)
        out["supply_chain"], out["burned"] = supply_chain, burned
        sellable = supply_chain - burned
        out["coverage_pct"] = stored_supply / sellable * 100 if sellable > 0 else None

        # ٢) الأرصدة: أكبرُ K حائزاً في اللقطة، مطابقةً تامّةً بالوحدة الأساسيّة.
        checked = top[: max(1, int(holders))]
        measured = [
            (address, stored, rpc.balance_of(token, address, block))
            for address, stored in checked
        ]
        out["holders_checked"] = len(measured)
        out["holders_matched"] = sum(1 for _, s, c in measured if s == c)
        off = [item for item in measured if item[1] != item[2]]
        if off and stored_supply > 0:
            worst = max(off, key=lambda item: abs(item[1] - item[2]))
            out["worst_holder"] = {
                "address": worst[0], "stored": str(worst[1]), "chain": str(worst[2]),
                "points": abs(worst[1] - worst[2]) / stored_supply * 100,
            }

        # ٣) النسبُ المشتقّة — ولا تُقارَن إلّا حيث يكفي عددُ ما فُحص: فحصُ خمسةٍ
        #    لا يقول شيئاً عن `top20_pct`، وإدراجُه هنا يكون فرقاً مصنوعاً.
        out["chain_pct"] = _percentages([c for _, _, c in measured], stored_supply)
        deltas = [
            abs(out["stored_pct"][f"top{n}_pct"] - out["chain_pct"][f"top{n}_pct"])
            for n in (1, 5, 10, 20)
            if n <= len(measured)
            and out["stored_pct"][f"top{n}_pct"] is not None
            and out["chain_pct"][f"top{n}_pct"] is not None
        ]
        out["delta_points"] = max(deltas, default=None)

        # الحكم: النقصُ أوّلاً. دفترٌ ناقصٌ خطؤه في المقام، فكلُّ نسبةٍ بُنيت عليه
        # مغلوطةٌ ولو طابق كلُّ حائزٍ فُحص — والعكسُ ليس صحيحاً.
        gap = sellable - stored_supply
        if sellable <= 0:
            out["verdict"] = "تعذّر"
            out["note"] = "المعروضُ القابل للبيع صفرٌ أو سالب ⇒ لا مقامَ للنسب"
        elif abs(gap) * 10_000 > sellable:  # أكثر من نقطةِ أساسٍ واحدة
            out["verdict"] = "ناقص" if gap > 0 else "زائد"
            cause = "سجلّاتُ تحويلٍ لم تُقرأ" if gap > 0 else "أرصدةٌ حُسبت مرّتين"
            out["note"] = (
                f"الدفترُ يحمل {out['coverage_pct']:.4f}% من المعروض القابل للبيع"
                f" — فجوةٌ {abs(gap) / sellable * 100:.4f}% ⇒ {cause}"
            )
        elif out["holders_matched"] == out["holders_checked"]:
            out["verdict"] = "مطابق"
        elif (out["delta_points"] or 0) <= DRIFT_POINTS:
            out["verdict"] = "انزياح"
            out["note"] = "فارقٌ دون عُشرِ نقطة ⇒ كتلةُ حدٍّ لا عطبُ دفتر"
        else:
            out["verdict"] = "تفاوت"
    except ArchiveError as exc:
        out["note"] = str(exc)
    finally:
        out["calls"] = rpc.calls - started
    return out


_MARK = {"مطابق": "✓", "انزياح": "≈", "ناقص": "✗", "زائد": "✗", "تفاوت": "✗",
         "تعذّر": "؟"}
_FAIL = ("ناقص", "زائد", "تفاوت")


def _line(record: dict) -> str:
    block = f"{record['block']:,}" if record["block"] else "—"
    cover = (
        f"{record['coverage_pct']:.4f}%" if record["coverage_pct"] is not None else "—"
    )
    delta = (
        f"{record['delta_points']:.4f}ن" if record["delta_points"] is not None else "—"
    )
    text = (
        f"   {_MARK.get(record['verdict'], '؟')} {record['token'][:14]}… · "
        f"{str(record['recorded_at'])[:16]} · كتلة {block} · "
        f"تغطية {cover} · حائزون {record['holders_matched']}/"
        f"{record['holders_checked']} · فرق {delta} · {record['calls']} نداءً"
    )
    if record["note"]:
        text += f"\n       {record['note']}"
    worst = record["worst_holder"]
    if worst:
        text += (
            f"\n       أسوأُ حائز {worst['address'][:12]}… "
            f"مخزّن {worst['stored']} · سلسلة {worst['chain']} "
            f"({worst['points']:.4f}ن)"
        )
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="تدقيقُ لقطاتِ دفترِ EVM بأرشيفٍ مفتاحيّ — قراءةً فقط.",
    )
    parser.add_argument("--networks", nargs="+", default=list(config.EVM_NETWORKS))
    parser.add_argument("--tokens", type=int, default=3, help="عملاتٌ لكلّ شبكة")
    parser.add_argument("--token", action="append", default=[], help="عملةٌ بعينها")
    parser.add_argument("--rows", type=int, default=3, help="لقطاتٌ لكلّ عملة")
    parser.add_argument("--holders", type=int, default=5, help="أكبرُ كم حائزاً يُسأل")
    parser.add_argument("--provider", choices=sorted(PROVIDER_FIELDS), default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--json", dest="json_path", default="", help="مسارُ تقريرٍ JSON")
    args = parser.parse_args(argv)

    db = db_module.RecorderDB(config.DB_PATH, os.path.join(HERE, "schema.sql"))
    records: list[dict] = []
    try:
        for network in [str(n) for n in args.networks]:
            rows = sample_rows(
                db, network, args.tokens, args.rows, tuple(args.token),
            )
            if not rows:
                print(f"── شبكة {network}: لا صفوفَ إعادةٍ تُدقَّق")
                continue
            try:
                provider, rpc = open_route(network, args.provider, args.timeout)
            except ArchiveError as exc:
                print(f"── شبكة {network}: {exc}")
                continue
            tokens = len({row["token_address"] for row in rows})
            print(
                f"── شبكة {network} · أرشيف {provider} · {tokens} عملة · "
                f"{len(rows)} لقطة"
            )
            started = time.monotonic()
            with rpc:
                # رأسُ السلسلة يُقرأ مرّةً للشبكة كلّها: هو سقفُ القوس فقط، ولقطاتُنا
                # كلُّها ماضيةٌ بأيّامٍ — فقراءتُه لكلّ صفٍّ نداءٌ يُدفَع بلا مقابل.
                try:
                    head = rpc.block_number()
                except ArchiveError as exc:
                    print(f"   ؟ تعذّر قراءةُ الرأس: {exc}")
                    continue
                for row in rows:
                    record = audit_row(rpc, db, row, args.holders, head)
                    record["provider"] = provider
                    records.append(record)
                    print(_line(record))
            print(f"   {rpc.calls} نداءً في {time.monotonic() - started:.1f}ث\n")
    finally:
        db.close()

    if not records:
        print("لا شيءَ دُقّق.")
        return 0
    tally: dict[str, int] = {}
    for record in records:
        tally[record["verdict"]] = tally.get(record["verdict"], 0) + 1
    print("المحصّلة: " + " · ".join(
        f"{_MARK.get(name, '؟')} {name} {count}" for name, count in tally.items()
    ))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=2)
        print(f"سُجّل في {args.json_path}")
    # حكمٌ سيّئٌ واحد يكفي لرمزِ خروجٍ غير صفر: هذه أداةُ فحصٍ لا تقرير، ونجاحُها
    # الصامتُ في وجهِ دفترٍ ناقصٍ هو بالضبط ما بُنيت لتمنعه.
    return 1 if any(tally.get(name) for name in _FAIL) else 0


if __name__ == "__main__":
    raise SystemExit(main())
