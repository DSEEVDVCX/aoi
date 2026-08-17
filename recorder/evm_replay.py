# -*- coding: utf-8 -*-
"""إعادة رجعيّة لتركّز حائزي EVM: صفوف تركّز لماضٍ لم تكن الطبقة تعمل فيه.

**لماذا هذه مسموحة وتعبئة الإشارات الرجعيّة ممنوعة؟** الفرق ليس في الزمن بل في
مصدر المعلومة. تلك تختلق نافذة مراقبة لعملة لم يرها البوت أصلاً — فتُدخِل إلى
التدريب اختياراً لم يكن ليقع. وهذه لا تُنشئ صفّاً ولا نافذة: تأخذ صفّ تدريب
**موجوداً** وتُكمِل أعمدةً كانت NULL فيه، بمعلومة كانت **متاحة فعلاً** في لحظته.

والسلسلة هي ما يجعل ذلك ممكناً وحدها بين مصادرنا: سجلّ لا يُغيَّر ومؤرَّخ
بالكتل. فإعادة تشغيل تحويلات عملة حتى الكتلة التي كانت رأساً عند اللحظة القديمة
تعطي **ما كان معلوماً في تلك اللحظة بالضبط** — لا شيء من مستقبلها. أمّا الأسعار
والسيولة والحائزون من FOMO فحالةٌ لحظيّة لا سجلّ، فلا يمكن إعادتها ولا تُحاوَل.

وثلاثة حرّاس تجعل الصفّ المُعاد إمّا صحيحاً أو غائباً، ولا شيء بينهما:

1. **`is_replay = 1`** على كل صفّ. الرقم صادق لكنّ طريقه مختلف (الحيّ بتأخير
   تأكيد وإيقاع دورة، والمُعاد عند الكتلة بالضبط) ⇒ الفصل عمودٌ حقيقيّ لا حقل
   في `raw_json`، ليبقى ممكناً تدريبٌ على المقيس حيّاً وحده.
2. **فحص الأرصدة السالبة.** الرصيد تراكم لا معدّل: من بدأ القراءة بعد أوّل تحويل
   يرى إرسالاً بلا استلام فيصير الرصيد سالباً — أي أنّ الأرقام **كاذبة لا
   ناقصة**. الطبقة الحيّة تُثبّت السالب عند صفر (عمودها نصّ سِتّينيّ لا يحمل
   إشارة)، وهنا لا يُثبَّت بل يُكشَف: عملة واحد من عناوينها سالب لا تُكتب أصلاً.
3. **الهامش الزمنيّ يُضاف لا يُطرح.** حيث لا طابع في السجلّ (روبن‑هود) يُستقرَأ
   الوقت بين مرساتين، والاستقراء يخطئ ثوانٍ. فالهامش يُبعِد السجلّ الحدوديّ إلى
   ما **بعد** اللقطة: أسوأ ما يقع تأخيرُ قياس (كما تفعل تأكيدات الكتل في الطبقة
   الحيّة) لا استباقُه — وهو الاتّجاه الوحيد المقبول.

والإيقاع 5 دقائق لا لقطة واحدة لكل صفّ تدريب: عائلة `onchain_*` تقرأ لقطةً
عند/قبل t0 **ولقطةً قبلها بـ240–900ث** لتحسب فروق الخمس دقائق (`features.py`)،
فلقطةٌ واحدة تعطي الفروق كلّها NULL — وهي أنفس ما في الطبقة.

الاستخدام:
  python evm_replay.py --check          # ما هو المتاح وما كلفته، بلا نداء كتابة
  python evm_replay.py --limit 1        # عملة واحدة (تحقّق يدويّ)
  python evm_replay.py --token 0x… --dry-run
  python evm_replay.py                  # كل المتاح على شبكات الإعادة
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import heapq
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402 — تشغيل الملف مباشرة يتطلب إضافة HERE أولاً
import evm_rpc  # noqa: E402
from db import RecorderDB, StaleEVMState, decode_raw, utcnow_iso  # noqa: E402
from evm_layer import build_evm_concentration_row  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - depends on host terminal
        pass

# عناوين لا تُحسب حائزاً — نفس مجموعة الطبقة الحيّة، وإلّا اختلف عمود عن عمود.
_BURN = {a.lower() for a in evm_rpc.BURN_ADDRESSES}
_TOP_N = 20
# كم لقطة قبل لحظة الدخول: فرق الخمس دقائق عند t0 يحتاج سلفاً قبله، فبلا هذه
# السابقة يبقى `onchain_*_delta_5m` فارغاً في أوّل صفّ — وهو أهمّ صفوف النافذة.
_LEAD_STEPS = 2


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())


def _iso(ts: int) -> str:
    """نفس صيغة `utcnow_iso` بالضبط: القراءة في `features.py` عبر
    `strftime('%s', recorded_at)` وSQLite يفهم الإزاحة `+00:00` لا حرف Z."""
    return datetime.fromtimestamp(int(ts), UTC).isoformat()


def grid_points(start_ts: int, end_ts: int, step: int) -> list[int]:
    """لحظات اللقطات: من البداية إلى النهاية بخطوة ثابتة، والنهاية داخلة.

    الشبكة تُحاذى على مضاعفات الخطوة (`ts // step * step`) لا على لحظة الدخول:
    عملتان دخلتا بفارق ثلاث دقائق تصيران على نفس الشبكة، فلو أُضيفت لقطات حيّة
    لاحقاً لم تتشابك سلسلتان بإيقاعين.
    """
    step = max(1, int(step))
    first = (int(start_ts) // step) * step
    return list(range(first, int(end_ts) + 1, step))


def watch_grid(
    watch: dict[str, Any], start_ts: int, end_ts: int, step: int,
    live_coverage: dict[str, str] | None = None,
) -> list[int]:
    """اتحاد نقاط نوافذ العملة الفعلية؛ لا يملأ الفجوات بين إعادة التنشيط."""
    windows = watch.get("replay_windows")
    if not isinstance(windows, list) or not windows:
        windows = [{
            "first_seen_at": watch["first_seen_at"],
            "watch_until": watch["watch_until"],
        }]
    coverage = live_coverage or {}
    points: set[int] = set()
    for window in windows:
        if not isinstance(window, dict):
            continue
        lo = _epoch(window["first_seen_at"]) - _LEAD_STEPS * step
        hi = min(_epoch(window["watch_until"]), int(end_ts))
        live_first = coverage.get(str(window["first_seen_at"]))
        if live_first:
            hi = min(hi, _epoch(live_first) - step)
        if hi > lo:
            points.update(grid_points(lo, hi, step))
    return sorted(point for point in points if int(start_ts) <= point <= int(end_ts))


class BlockClock:
    """جدول تحويل وقت↔كتلة لشبكة واحدة، مخزَّن في `evm_block_time`.

    مشترك بين العملات بحكم التصميم: المرساة صفة كتلة لا صفة عملة، ونوافذ الـ48
    ساعة متراكبة بكثافة ⇒ العملة العاشرة على الشبكة تكاد لا تنادي شيئاً. ولذلك
    تُحاذى المراسي على شبكة ثابتة (مضاعفات `EVM_REPLAY_ANCHOR_BLOCKS`): أرقام
    عشوائية لكل عملة تعني صفراً من إعادة الاستخدام.

    والمرساة لا تُبطل أبداً — طابع الكتلة لا يتغيّر — فهي أرخص ذاكرة في المشروع.
    """

    def __init__(self, db: RecorderDB, network_id: str) -> None:
        self.db = db
        self.net = str(network_id)
        points = db.block_anchors(self.net)
        self.blocks = [b for b, _ in points]
        self.times = [t for _, t in points]
        self.calls = 0

    def __len__(self) -> int:
        return len(self.blocks)

    def add(self, block: int, ts: int, now_iso: str) -> None:
        block, ts = int(block), int(ts)
        i = bisect.bisect_left(self.blocks, block)
        if i < len(self.blocks) and self.blocks[i] == block:
            return
        self.blocks.insert(i, block)
        self.times.insert(i, ts)
        self.db.add_block_anchor(self.net, block, ts, now_iso)

    def time_at(self, block: int) -> int | None:
        """وقت كتلة بالاستقراء الخطّيّ بين أقرب مرساتين.

        الخطّيّة مشروعة بقياس: زمن كتلة روبن‑هود 0.1002ث على 100 ألف كتلة
        و0.1003ث على 300 ألف (2026-08-13) ⇒ الانحراف داخل قوس واحد (18 ألف كتلة
        = نصف ساعة) ثوانٍ لا دقائق. وخارج القوسين يُمَدّ الميل من أقرب زوج بدل
        `None`: العملة الجديدة كل تحويلاتها فوق آخر مرساة، ورفضُها يعني ألّا
        نقيس شيئاً.
        """
        block = int(block)
        n = len(self.blocks)
        if n == 0:
            return None
        if n == 1:
            return self.times[0] if self.blocks[0] == block else None
        i = bisect.bisect_left(self.blocks, block)
        if i < n and self.blocks[i] == block:
            return self.times[i]
        lo = min(max(i - 1, 0), n - 2)   # قوس داخليّ، أو أقرب زوج عند الطرفين
        b0, b1 = self.blocks[lo], self.blocks[lo + 1]
        t0, t1 = self.times[lo], self.times[lo + 1]
        if b1 == b0:
            return t0
        return int(round(t0 + (block - b0) * (t1 - t0) / (b1 - b0)))

    def has_exact(self, block: int) -> bool:
        i = bisect.bisect_left(self.blocks, int(block))
        return i < len(self.blocks) and self.blocks[i] == int(block)

    async def probe(
        self, rpc: Any, block: int, now_iso: str, sleep=asyncio.sleep,
        retries: int | None = None,
    ) -> int | None:
        """يجلب طابع كتلة ويحفظه مرساةً. الكتلة المعروفة لا تُنادى.

        والكتم (429) يُنتظَر ويُعاد **هنا** لا يُرفَع: عقدة روبن‑هود العامّة تكتم
        بعد تسعة نداءات (مقيس 2026-08-13 على تباعد 0.4ث و1.0ث سواءً — فهي حصّة
        لا تباعد)، وأوّل تشغيل حيّ مات عند العملة الأولى بـ`EVMRateLimit` من
        `eth_getBlockByNumber`. ورفعُه يُسقط العملة كلّها بسبب ثانية مزدحمة،
        بينما مسار السجلّات ينتظر ويعيد نفس المدى — فالمرساة تستحقّ نفس المعاملة.
        وكل محاولة تُحسب نداءً ولو رُدّت: الحصّة تُستهلك بالطلب لا بالجواب.
        """
        block = int(block)
        if block < 0:
            return None
        if self.has_exact(block):
            return self.time_at(block)
        tries = (
            int(config.EVM_REPLAY_ANCHOR_RETRIES) if retries is None else int(retries)
        )
        for attempt in range(max(0, tries) + 1):
            self.calls += 1
            try:
                ts = await rpc.block_timestamp(self.net, block)
            except evm_rpc.EVMRateLimit:
                if attempt >= max(0, tries):
                    raise
                await sleep(config.EVM_RATE_LIMIT_BACKOFF_SECONDS)
                continue
            break
        if ts is None:
            return None
        self.add(block, ts, now_iso)
        return ts

    async def ensure_blocks(
        self, rpc: Any, blocks: Iterable[int], now_iso: str, sleep,
        max_calls: int, head: int | None = None,
    ) -> int:
        """يضمن قوساً حول كل كتلة مطلوبة، على الشبكة الثابتة.

        يعيد عدد الكتل التي بقيت بلا قوس (نفدت الميزانية) — والمنادي يعدّها
        سجلّات لا وقت لها فيُهملها ولا يخمّنها.
        """
        step = max(1, int(config.EVM_REPLAY_ANCHOR_BLOCKS))
        wanted: set[int] = set()
        for b in blocks:
            b = int(b)
            wanted.add((b // step) * step)
            wanted.add((b // step) * step + step)
        missing = sorted(x for x in wanted if not self.has_exact(x))
        if head is not None:
            missing = [x for x in missing if x <= int(head)]
        left = 0
        for i, block in enumerate(missing):
            if self.calls >= max_calls:
                left = len(missing) - i
                break
            if self.calls:
                await sleep(config.EVM_PACING_SECONDS)
            await self.probe(rpc, block, now_iso, sleep)
        return left

    def _guess_block(self, target_ts: int, head: int) -> int:
        """تقدير أوّليّ لرقم الكتلة عند وقت، بعكس الاستقراء."""
        n = len(self.times)
        if n == 0:
            return max(0, head // 2)
        i = bisect.bisect_left(self.times, int(target_ts))
        if n == 1:
            return self.blocks[0]
        lo = min(max(i - 1, 0), n - 2)
        t0, t1 = self.times[lo], self.times[lo + 1]
        b0, b1 = self.blocks[lo], self.blocks[lo + 1]
        if t1 == t0:
            return b0
        guess = b0 + (int(target_ts) - t0) * (b1 - b0) / (t1 - t0)
        return int(min(max(0, guess), head))

    async def block_at_time(
        self, rpc: Any, target_ts: int, head: int, now_iso: str, sleep,
        max_calls: int, tolerance: int = 300,
    ) -> int:
        """أعلى كتلة طابعها ≤ الوقت المطلوب — بحثاً بالاستقراء لا بالتنصيف.

        التنصيف يكلّف ~25 نداءً على شبكة بـ30 مليون كتلة، والاستقراء يكلّف 3–4:
        زمن الكتلة شبه ثابت فالتقدير الأوّل يقع داخل دقائق، ونداء واحد يصحّحه.

        والخطأ **مقصود في اتّجاه القِدَم**: أيّ كتلة أقدم من المطلوب تكلّف نداءات
        سجلّات زائدة وتنتهي، أمّا كتلة أحدث فتُفقِد تحويلات ⇒ أرصدة سالبة وصفوف
        لا تُكتب. فالجواب أدنى مرساة تحقّق الشرط، لا أقربها.
        """
        target = int(target_ts)
        if len(self.blocks) < 2:
            await self.probe(rpc, head, now_iso, sleep)
            await sleep(config.EVM_PACING_SECONDS)
            await self.probe(rpc, max(0, head - 1_000_000), now_iso, sleep)
        for _ in range(5):
            if self.calls >= max_calls:
                break
            guess = self._guess_block(target, head)
            if self.has_exact(guess):
                ts = self.time_at(guess)
            else:
                await sleep(config.EVM_PACING_SECONDS)
                ts = await self.probe(rpc, guess, now_iso, sleep)
            if ts is None or abs(ts - target) <= tolerance:
                break
        below = [b for b, t in zip(self.blocks, self.times) if t <= target]
        return max(below) if below else 0


def resolve_log_times(
    logs: Sequence[dict[str, Any]], clock: BlockClock, margin: int,
) -> tuple[dict[int, int], int]:
    """{رقم كتلة: وقتها} لكل كتلة ظهرت في السجلّات، وعدد الكتل بلا وقت.

    الطابع من السجلّ نفسه إن أعطته العقدة (Base وBSC تعطيانه) — لا استقراء ولا
    هامش حينها، فهو الحقيقة. وروبن‑هود تعيد `blockTimestamp: '0x0'` (مقيس) ⇒
    استقراءٌ **زائد هامش**: الزيادة تُخرِج السجلّ الحدوديّ من اللقطة، والنقص
    يُدخِل تحويلاً من مستقبلها.

    و`'0x0'` تُعامَل غياباً لا وقتاً: كتلة عام 1970 كانت ستُدخِل كل التحويلات في
    كل اللقطات — أي عكس ما نريد بالضبط.
    """
    out: dict[int, int] = {}
    unknown = 0
    for log in logs:
        block = evm_rpc._num(log.get("blockNumber"))
        if block is None or block in out:
            continue
        raw = evm_rpc._num(log.get("blockTimestamp"))
        if raw:
            out[block] = raw
            continue
        est = clock.time_at(block)
        if est is None:
            unknown += 1
            continue
        out[block] = est + int(margin)
    return out, unknown


def replay_rows(
    logs: Sequence[dict[str, Any]],
    block_ts: dict[int, int],
    grid: Sequence[int],
    token_address: str,
    network_id: str,
    watch: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """سجلّات عملة + شبكة لحظات ⇒ صفوف `chain_concentration` بتواريخها.

    دالّة خالصة بلا شبكة ولا قاعدة: هي قلب الصحّة هنا فيجب أن تكون قابلة
    للاختبار بلا أيّ منهما. والمرور واحد: التحويلات تُرتَّب زمنيّاً مرّة، ثمّ
    يمشي مؤشّر واحد مع الشبكة — فالتكلفة خطّيّة لا (لحظات × تحويلات).
    """
    token = token_address.lower()
    events: list[tuple[int, int, str, str, int]] = []
    skipped = 0
    for log in logs:
        rec = evm_rpc.decode_transfer(log)
        if rec is None or rec["token_address"] != token:
            skipped += 1
            continue
        ts = block_ts.get(rec["block"])
        if ts is None:
            skipped += 1
            continue
        events.append((ts, rec["block"], rec["from"], rec["to"], rec["value"]))
    events.sort(key=lambda e: (e[0], e[1]))

    balances: dict[str, int] = {}
    negatives: set[str] = set()
    rows: list[dict[str, Any]] = []
    empty = 0
    idx = 0
    for point in grid:
        while idx < len(events) and events[idx][0] <= point:
            _, _, src, dst, value = events[idx]
            idx += 1
            for holder, delta in ((src, -value), (dst, value)):
                if delta == 0:
                    continue
                new = balances.get(holder, 0) + delta
                balances[holder] = new
                # لا تثبيت عند صفر كما تفعل الطبقة الحيّة: السالب هنا **دليل**
                # على أنّ القراءة بدأت متأخّرة، وطمسُه يحوّل الدليل إلى رقم
                # يبدو سليماً وهو كاذب.
                if new < 0 and holder not in _BURN:
                    negatives.add(holder)
        live = {h: v for h, v in balances.items() if v > 0 and h not in _BURN}
        if not live:
            empty += 1
            continue
        supply = sum(live.values())
        top = heapq.nlargest(_TOP_N, live.items(), key=lambda kv: kv[1])
        row = build_evm_concentration_row(
            {"holder_count": len(live), "supply": supply}, top, token,
            str(network_id), _iso(point), watch["first_seen_at"],
            watch.get("entry_signal_id"), int(watch.get("is_control") or 0),
        )
        if row is None:
            empty += 1
            continue
        row["is_replay"] = 1
        rows.append(row)

    meta = {
        "events": len(events), "skipped": skipped, "empty": empty,
        "negatives": len(negatives), "applied": idx,
    }
    # عملة واحد من عناوينها سالب ⇒ لا صفّ واحد. الجزئيّة هنا ليست «أقلّ دقّة» بل
    # نِسبٌ محسوبة على معروض ناقص: عملة فاتنا سكّها تظهر بتركّز 90% وهو 9%.
    if negatives:
        return [], meta
    return rows, meta


def _replay_segment(
    logs: Sequence[dict[str, Any]], block_ts: dict[int, int], grid: Sequence[int],
    token_address: str, network_id: str, watch: dict[str, Any],
    initial_balances: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    """نسخة تراكميّة من قلب الإعادة، تعيد الرصيد checkpoint للجزء التالي."""
    token = token_address.lower()
    events: list[tuple[int, int, str, str, int]] = []
    skipped = 0
    for log in logs:
        rec = evm_rpc.decode_transfer(log)
        if rec is None or rec["token_address"] != token:
            skipped += 1
            continue
        ts = block_ts.get(rec["block"])
        if ts is None:
            skipped += 1
            continue
        events.append((ts, rec["block"], rec["from"], rec["to"], rec["value"]))
    events.sort(key=lambda event: (event[0], event[1]))

    balances = dict(initial_balances or {})
    negatives: set[str] = set()
    rows: list[dict[str, Any]] = []
    empty = 0
    idx = 0
    for point in grid:
        while idx < len(events) and events[idx][0] <= point:
            _, _, src, dst, value = events[idx]
            idx += 1
            for holder, delta in ((src, -value), (dst, value)):
                if delta == 0:
                    continue
                new = balances.get(holder, 0) + delta
                balances[holder] = new
                if new < 0 and holder not in _BURN:
                    negatives.add(holder)
        live = {h: v for h, v in balances.items() if v > 0 and h not in _BURN}
        if not live:
            empty += 1
            continue
        supply = sum(live.values())
        top = heapq.nlargest(_TOP_N, live.items(), key=lambda item: item[1])
        row = build_evm_concentration_row(
            {"holder_count": len(live), "supply": supply}, top, token,
            str(network_id), _iso(point), watch["first_seen_at"],
            watch.get("entry_signal_id"), int(watch.get("is_control") or 0),
        )
        if row is not None:
            row["is_replay"] = 1
            rows.append(row)

    # طبّق ما يقع بعد آخر نقطة أيضاً كي يمثل checkpoint نهاية المدى المقروء، لا
    # آخر لقطة فقط. هذه الأحداث ستؤثر في أول لقطة من الجزء التالي.
    while idx < len(events):
        _, _, src, dst, value = events[idx]
        idx += 1
        for holder, delta in ((src, -value), (dst, value)):
            if delta == 0:
                continue
            new = balances.get(holder, 0) + delta
            balances[holder] = new
            if new < 0 and holder not in _BURN:
                negatives.add(holder)

    meta = {
        "events": len(events), "skipped": skipped, "empty": empty,
        "negatives": len(negatives), "applied": idx,
    }
    if negatives:
        return [], meta, balances
    return rows, meta, balances


def _window(
    db: RecorderDB, watch: dict[str, Any], step: int,
) -> tuple[int, int, dict[str, str]]:
    """حدود الشبكة الزمنيّة لعملة: من قبيل الدخول إلى منتهى النافذة.

    والحدّ الأعلى يتوقّف حيث **تبدأ التغطية الحيّة**: قياسان لنفس اللحظة من
    طريقين (الحيّ بتأخير تأكيد وإيقاع دورة، والمُعاد عند الكتلة بالضبط) يتفاوتان
    قليلاً، وتشابكهما في سلسلة واحدة يخلق فروق خمس‑دقائق وهميّة — وهي أنفس ما
    تقرؤه الميزات. فالإعادة تملأ ما قبل أوّل صفّ حيّ ولا تلمس ما بعده.
    """
    token = str(watch["token_address"]).lower()
    net = str(watch["network_id"])
    start_ts = _epoch(watch["first_seen_at"]) - _LEAD_STEPS * step
    end_ts = _epoch(watch["watch_until"])
    return start_ts, end_ts, db.chain_live_coverage(token, net)


async def replay_token(
    rpc: Any, db: RecorderDB, watch: dict[str, Any], clock: BlockClock,
    head: int, now_iso: str, sleep=asyncio.sleep, write: bool = True,
    replace_existing: bool = False, deadline: float | None = None,
) -> dict[str, Any]:
    """إعادة عملة واحدة كاملة: مدًى واحد من السجلّات ثمّ مرور واحد على الشبكة."""
    net = str(watch["network_id"])
    token = str(watch["token_address"]).lower()
    generation = db.evm_ledger_generation()
    expected_status = watch.get("replay_status")
    expected_from = watch.get("replay_from_block")
    expected_checkpoint = watch.get("replay_checkpoint_json")
    expected_revision = watch.get("replay_revision")
    expected_windows = watch.get("replay_windows")
    step = int(config.EVM_REPLAY_STEP_SECONDS)
    out: dict[str, Any] = {
        "token": token, "network": net, "status": "skip", "rows": 0,
        "written": 0, "calls": 0, "anchor_calls": 0, "events": 0,
        "negatives": 0, "unknown_time": 0, "from_block": None,
        "to_block": None, "note": "",
    }
    start_ts, end_ts, live_coverage = _window(db, watch, step)
    full_grid = watch_grid(watch, start_ts, end_ts, step, live_coverage)
    if end_ts <= start_ts or not full_grid:
        out["note"] = "التغطية الحيّة تسبق النافذة" if live_coverage else "نافذة فارغة"
        out["status"] = "skip"
        if write:
            with db.batch():
                db.assert_evm_ledger_generation(generation)
                db.assert_chain_live_coverage(token, net, live_coverage)
                if isinstance(expected_windows, list):
                    db.assert_evm_replay_windows(token, net, expected_windows)
                db.assert_evm_replay_state(
                    token, net, expected_status, expected_from, expected_checkpoint,
                    expected_revision,
                )
                if replace_existing:
                    db.reset_evm_replay_token(token, net)
                db.set_evm_replay_state(
                    token, net, "skip", now_iso, from_block=0, to_block=0,
                    transfers=0, snapshots=0, calls=0, balance_check="ok",
                    last_error=out["note"],
                )
        return out

    # ميزانيّتان منفصلتان: المراسي والسجلّات. سقفٌ واحد مشترك يجعل عملةً
    # صاخبة تأكل ميزانية المراسي فتبقى سجلّاتها بلا وقت — أي نداءات بلا صفوف.
    base_calls = clock.calls
    anchor_ceiling = base_calls + max(4, int(config.EVM_REPLAY_ANCHOR_MAX_CALLS))
    log_budget = max(1, int(config.EVM_REPLAY_MAX_CALLS))
    to_block = max(0, int(head) - int(config.EVM_CONFIRMATIONS))

    checkpoint: dict[str, Any] | None = None
    if (watch.get("replay_status") or "") in ("partial", "error"):
        raw_checkpoint = watch.get("replay_checkpoint_json")
        if raw_checkpoint is not None:
            try:
                decoded = decode_raw(raw_checkpoint)
                checkpoint = decoded if isinstance(decoded, dict) else None
            except Exception:  # noqa: BLE001 — checkpoint تالف يعاد بأمان من البداية
                checkpoint = None
    if checkpoint is not None and watch.get("replay_from_block") is not None:
        from_block = int(watch["replay_from_block"])
    else:
        # الرصيد حالة تراكميّة منذ إنشاء العقد. نافذة زمنية أو غياب أرصدة سالبة
        # لا يثبتان الاكتمال: حائز استلم قبل النافذة ولم يتحرك بعدها يختفي بصمت.
        # نبدأ من genesis ونستأنف عبر checkpoint؛ أبطأ لكنه الدليل الوحيد الكامل.
        from_block = 0
        if net in config.EVM_HISTORICAL_RPC_URLS:
            creation = await rpc.contract_creation_block(net, token, to_block)
            if creation is not None:
                from_block = creation

    rows: list[dict[str, Any]] = []
    meta: dict[str, int] = {"events": 0, "skipped": 0, "empty": 0,
                            "negatives": 0, "applied": 0}
    calls_used = 0
    unknown = 0
    complete = False
    covered_to = to_block
    attempt_from = from_block
    initial_balances = {
        str(holder): int(value)
        for holder, value in (checkpoint or {}).get("balances", {}).items()
    }
    next_grid = int((checkpoint or {}).get("next_grid") or grid_points(start_ts, start_ts, step)[0])
    final_balances = dict(initial_balances)
    for _attempt in (0,):
        logs, used, complete, resume = await rpc.get_logs_paged(
            net, [token], attempt_from, to_block, max_calls=log_budget, sleep=sleep,
            deadline=deadline,
        )
        calls_used += used
        covered_to = to_block if complete else int(resume) - 1
        blocks = [
            b for b in (evm_rpc._num(lg.get("blockNumber")) for lg in logs)
            if b is not None
        ]
        if any(not evm_rpc._num(lg.get("blockTimestamp")) for lg in logs):
            await clock.ensure_blocks(
                rpc, blocks, now_iso, sleep, anchor_ceiling, head=to_block,
            )
        if covered_to >= attempt_from:
            await clock.probe(rpc, covered_to, now_iso, sleep)
        block_ts, unknown = resolve_log_times(
            logs, clock, config.EVM_REPLAY_TIME_MARGIN_SECONDS,
        )
        made_progress = covered_to >= attempt_from
        cap_ts = clock.time_at(covered_to) if made_progress else None
        window_end = end_ts if cap_ts is None else min(end_ts, int(cap_ts))
        grid = [] if not made_progress else [
            point for point in full_grid
            if point <= window_end and point >= next_grid
        ]
        rows, meta, final_balances = _replay_segment(
            logs, block_ts, grid, token, net, watch, initial_balances,
        )
        break

    window_complete = bool(
        complete and cap_ts is not None
        and int(cap_ts) >= full_grid[-1]
    )

    out.update({
        "calls": calls_used, "anchor_calls": clock.calls - base_calls,
        "events": meta["events"], "negatives": meta["negatives"],
        "unknown_time": unknown, "from_block": attempt_from,
        "to_block": covered_to, "rows": len(rows),
    })

    # ثلاثة أسباب لعدم الكتابة، ولكلٍّ حالته: أرصدة سالبة (أرقام كاذبة)، وسجلّ
    # بلا وقت (لا نعرف في أيّ لقطة يدخل فيخمَّن)، ولا تحويل أصلاً (عملة لم
    # تتحرّك — غياب لا صفر، FR-007).
    if meta["negatives"]:
        status, check = "negative", "negative"
        out["note"] = f"{meta['negatives']} عنواناً سالباً ⇒ لا صفّ"
    elif unknown:
        status, check = "no_time", "ok"
        out["note"] = f"{unknown} كتلة بلا وقت ⇒ لا صفّ"
    elif not window_complete:
        status, check = "partial", "ok"
        if not rows:
            out["note"] = "المدى جزئيّ ولم يبلغ أول لقطة بعد"
    elif not rows:
        status, check = "empty", "ok"
        out["note"] = "لا تحويل في المدى"
    else:
        status, check = "done", "ok"

    written = 0
    if write:
        # الصفوف والـcheckpoint معاملة واحدة. سقوط العملية بينهما كان يعيد
        # الجزء نفسه من checkpoint قديم؛ INSERT OR IGNORE يمنع التكرار المرئي
        # لكنه يترك عدادات وحالة تقدم غير متسقة.
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            db.assert_chain_live_coverage(token, net, live_coverage)
            if isinstance(expected_windows, list):
                db.assert_evm_replay_windows(token, net, expected_windows)
            db.assert_evm_replay_state(
                token, net, expected_status, expected_from, expected_checkpoint,
                expected_revision,
            )
            if replace_existing and checkpoint is None:
                db.delete_evm_replay_rows(token, net)
            for row in rows:
                if db.insert_chain_concentration(row):
                    written += 1
            if status in ("negative", "no_time"):
                # checkpoint سابق قد كتب صفوفاً قبل اكتشاف فساد لاحق؛ بقاء بعضها
                # يجعل العملة تبدو مكتملة جزئياً وهي مرفوضة كلياً.
                db.delete_evm_replay_rows(token, net)
                written = 0
            prior_snapshots = int(watch.get("replay_snapshots") or 0) if checkpoint else 0
            if status in ("negative", "no_time"):
                prior_snapshots = 0
            prior_transfers = int(watch.get("replay_transfers") or 0) if checkpoint else 0
            prior_calls = int(watch.get("replay_calls") or 0) if checkpoint else 0
            # في الحالة المكتملة يبقى `from_block` تاريخياً: بداية المدى الذي
            # فُحص. أما `partial` فيحمل نقطة الاستئناف الفعلية.
            state_from_block = (
                from_block if window_complete else
                (covered_to + 1 if complete else int(resume))
            )
            next_point = (grid[-1] + step) if grid else next_grid
            saved_checkpoint = (
                {"balances": {h: str(v) for h, v in final_balances.items()},
                 "next_grid": next_point}
                if status == "partial" else None
            )
            # **لا `set_chain_state`**: ذاك جدول إيقاع الطبقة الحيّة، وكتابته هنا
            # تُخبر الحيّة أنّ العملة قيست الآن فتؤجّل لقطتها الحقيقيّة.
            db.set_evm_replay_state(
                token, net, status, now_iso, from_block=state_from_block,
                to_block=covered_to, transfers=prior_transfers + meta["applied"],
                snapshots=prior_snapshots + written, calls=prior_calls + calls_used,
                balance_check=check, last_error=out["note"] or None,
                checkpoint=saved_checkpoint,
            )
    out["written"] = written
    out["status"] = status
    return out


# حالات نهائيّة لا تُعاد إلّا بـ`--redo`: أُنجزت، أو تبيّن أنّها لا تُنجَز.
# و`partial`/`error` **ليست** نهائيّة: الأولى نفد سقف نداءاتها والثانية عطبٌ قد
# يكون عابراً (كتم، مهلة) — ودمجُهما مع النهائيّة يعني هجر عملة بسبب ثانية سيّئة.
_FINAL_STATUSES = ("done", "negative", "empty", "no_time", "skip")


async def run_replay(
    rpc: Any, db: RecorderDB, networks: Sequence[str] | None = None,
    limit: int | None = None, token: str | None = None,
    sleep=asyncio.sleep, write: bool = True, redo: bool = False,
    log=None, budget_seconds: float | None = None,
) -> dict[str, Any]:
    """المهمّة كاملة: كل عملة مستحقّة على كل شبكة إعادة مسموحة.

    خطأ عملة واحدة لا يُسقط شبكتها ولا يُسقط الشبكة التالية — نفس حرس الطبقة
    الحيّة. والحالة محفوظة لكل عملة فالقطع في أيّ لحظة لا يفقد إلّا العملة
    الجارية، وتُستأنف بتشغيل آخر.
    """
    emit = log or (lambda _m: None)
    allowed = {str(n) for n in config.EVM_REPLAY_NETWORKS}
    asked = [str(n) for n in (networks or config.EVM_REPLAY_NETWORKS)]
    nets = [n for n in asked if n in allowed]
    stats: dict[str, Any] = {
        "networks": len(nets), "tokens": 0, "rows": 0, "written": 0,
        "calls": 0, "anchor_calls": 0, "done": 0, "partial": 0,
        "negative": 0, "empty": 0, "no_time": 0, "skip": 0, "errors": 0,
        "refused_networks": [n for n in asked if n not in allowed],
    }
    remaining = None if limit is None else int(limit)
    budget = (
        config.EVM_REPLAY_BUDGET_SECONDS
        if budget_seconds is None else float(budget_seconds)
    )
    deadline = time.monotonic() + budget if budget > 0 else None
    for net in nets:
        targets = [
            w for w in db.evm_replay_targets([net])
            if redo or (w.get("replay_status") or None) not in _FINAL_STATUSES
        ]
        if token:
            targets = [w for w in targets if w["token_address"].lower() == token.lower()]
        if not targets:
            emit(f"[{net}] لا عملة مستحقّة")
            continue
        head = await rpc.block_number(net)
        clock = BlockClock(db, net)
        emit(f"[{net}] رأس {head} · مستحقّ {len(targets)} · مراسٍ محفوظة {len(clock)}")
        anchors_before = clock.calls
        for watch in targets:
            if remaining is not None and remaining <= 0:
                break
            if deadline is not None and time.monotonic() >= deadline:
                emit(f"[{net}] انتهت الميزانية الزمنيّة — تُستأنف لاحقاً")
                break
            now_iso = utcnow_iso()
            generation = db.evm_ledger_generation()
            try:
                res = await replay_token(
                    rpc, db, watch, clock, head, now_iso, sleep=sleep, write=write,
                    replace_existing=redo, deadline=deadline,
                )
            except StaleEVMState:
                # عامل آخر أو reset سبقنا؛ لا نخفض حالته الناجحة إلى error.
                continue
            except Exception as exc:  # noqa: BLE001 — عملة لا تُسقط شبكة
                stats["errors"] += 1
                msg = f"{type(exc).__name__}: {exc}"[:300]
                emit(f"  ✗ {watch['token_address'][:12]}… {msg}")
                if write and not redo:
                    try:
                        with db.batch():
                            db.assert_evm_ledger_generation(generation)
                            db.assert_evm_replay_state(
                                watch["token_address"], net,
                                watch.get("replay_status"),
                                watch.get("replay_from_block"),
                                watch.get("replay_checkpoint_json"),
                                watch.get("replay_revision"),
                            )
                            db.mark_evm_replay_error(
                                watch["token_address"], net, now_iso, msg,
                            )
                    except StaleEVMState:
                        stats["errors"] -= 1
                continue
            stats["tokens"] += 1
            stats[res["status"]] = stats.get(res["status"], 0) + 1
            for key in ("rows", "written", "calls"):
                stats[key] += res[key]
            if remaining is not None:
                remaining -= 1
            emit(
                f"  {res['status']:<8} {res['token'][:12]}… "
                f"صفوف {res['written']}/{res['rows']} · تحويلات {res['events']} "
                f"· نداءات {res['calls']}+{res['anchor_calls']} "
                f"· كتل {res['from_block']}→{res['to_block']}"
                + (f" · {res['note']}" if res["note"] else "")
            )
        stats["anchor_calls"] += clock.calls - anchors_before
    return stats


def _log_line(msg: str) -> None:
    print(msg, flush=True)
    try:
        with open(config.EVM_REPLAY_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{utcnow_iso()} {msg}\n")
    except OSError:
        pass


def _check(db: RecorderDB) -> int:
    """ما هو المستحقّ وما كلفته — بلا نداء شبكة واحد."""
    print(
        "شبكات الإعادة: "
        + (", ".join(str(n) for n in config.EVM_REPLAY_NETWORKS) or "—")
        + f" · خطوة {config.EVM_REPLAY_STEP_SECONDS}ث"
        f" · سقف {config.EVM_REPLAY_MAX_CALLS} نداءً/عملة"
    )
    total_rows = 0
    for net in (str(n) for n in config.EVM_REPLAY_NETWORKS):
        targets = db.evm_replay_targets([net])
        by_status: dict[str, int] = {}
        for w in targets:
            key = str(w.get("replay_status") or "—")
            by_status[key] = by_status.get(key, 0) + 1
        due = [
            w for w in targets
            if (w.get("replay_status") or None) not in _FINAL_STATUSES
        ]
        active = sum(1 for w in targets if int(w.get("active") or 0))
        # 48 ساعة ÷ 5 دقائق = 576 لقطة للنافذة الكاملة، والمنتهية نافذتها كاملة.
        est = len(due) * (48 * 3600 // int(config.EVM_REPLAY_STEP_SECONDS))
        total_rows += est
        print(
            f"[{net}] مراقَبات {len(targets)} (نشطة {active}) · مستحقّ {len(due)}"
            f" · مراسٍ محفوظة {len(db.block_anchors(net))}"
            f" · حالات: {by_status or '—'} ⇒ حتّى ~{est:,} صفّاً"
        )
    print(f"المجموع الأقصى: ~{total_rows:,} صفّ تركّز مُعاد (is_replay=1)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="إعادة رجعيّة لتركّز حائزي EVM")
    ap.add_argument("--networks", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="يحسب ولا يكتب صفّاً ولا حالة")
    ap.add_argument("--redo", action="store_true",
                    help="يعيد العملات المنتهية حالتها أيضاً")
    ap.add_argument("--check", action="store_true", help="تقرير بلا نداء شبكة")
    args = ap.parse_args()

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    if args.check:
        try:
            return _check(db)
        finally:
            db.close()

    async def _go() -> dict[str, Any]:
        rpc = evm_rpc.EVMRPC()
        try:
            return await run_replay(
                rpc, db, networks=args.networks, limit=args.limit,
                token=args.token, write=not args.dry_run, redo=args.redo,
                log=_log_line,
            )
        finally:
            await rpc.aclose()

    started = time.monotonic()
    try:
        stats = asyncio.run(_go())
    finally:
        db.close()
    stats["seconds"] = round(time.monotonic() - started, 1)
    _log_line(f"replay: {stats}")
    return 0 if not stats["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
