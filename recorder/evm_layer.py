# -*- coding: utf-8 -*-
"""دورة طبقة EVM: دفتر أرصدة نبنيه بأنفسنا من سجلّات `Transfer`.

**لماذا دفتر لا مزوّد؟** لأنّ معيار ERC-20 لا يخزّن قائمة حائزين، فلا نداء عقدة
يعيدها ولا مزوّد يعطيها بدقّة: Blockscout يعطي أعلى 50 وحدها بـ5.6 ثانية للعملة،
وNodeReal بـBSC وحدها وبميزانية ساعيّة، وEtherscan يرفض بلا مفتاح. والمقيس أنّ
الدفتر أرخص منهم جميعاً: نداء `eth_getLogs` واحد بمرشّح يحمل **كل** عناوين
الشبكة (روبن‑هود 57 عنواناً في 0.5 ثانية، Base 22 في 0.4) ⇒ ثمانية نداءات في
الدقيقة تكفي قائمة EVM كلّها.

وهو يعطي **أكثر** ممّا يعطيه أي مزوّد لا أقلّ: عدد حائزين مضبوطاً بلا سقف رتبة،
وأوّل كتلة استلم فيها كل عنوان — فسؤال «كم حائزاً جديداً في آخر خمس دقائق» يصير
عمليّة عدّ في القاعدة بلا نداء واحد، وهو سؤال لا يجيبه أي مزوّد إذ كلّهم لقطة
بلا تاريخ دخول.

ثلاث خطوات في الدورة، وترتيبها ملزم:
  1. **التطبيق** — نداء لكل شبكة من مؤشّرها إلى (الرأس − تأكيدات).
  2. **التعبئة** — عملات جديدة، من الكتلة صفر إلى المؤشّر، بسقف نداءات.
  3. **اللقطة** — قراءة من الدفتر إلى `chain_concentration` بلا نداء شبكة.

التعبئة **بعد** التطبيق لا قبله: لو عُبِّئت عملة إلى المؤشّر ثم طُبِّق السجلّ من
نفس المؤشّر لطُبِّق مدًى مرّتين فتضاعفت الأرصدة. والحدّ الفاصل هو `to_block`
المحفوظ في حالة التعبئة.

قراءة فقط (FR-012). لا مفتاح في هذه الطبقة إطلاقاً ⇒ لا سرّ يُشطب.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Sequence

import config
import evm_rpc
from db import RecorderDB, StaleEVMState, utcnow_iso
from evm_rpc import EVMLogLimit

_CHAIN_TIERS = ((1, "top1_pct"), (5, "top5_pct"), (10, "top10_pct"), (20, "top20_pct"))


def _batches(items: Sequence[str], size: int) -> list[list[str]]:
    """يقسم العناوين إلى دفعات بحجم يقبله المزوّد (حدّه لا اختيارنا)."""
    size = max(1, int(size))
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def _deltas_by_token(
    logs: Sequence[dict[str, Any]],
) -> dict[str, dict[str, tuple[int, int, int | None]]]:
    """سجلّات خام → {عملة: {عنوان: (تغيّر موقَّع، آخر كتلة)}}.

    التجميع قبل الكتابة مقصود: عنوان يتحرّك عشر مرّات في دقيقة يصير كتابةً واحدة،
    والجمع في بايثون بأعداد بلا حدّ (uint256 يتجاوز 64 بتّاً فلا يصحّ في SQL).

    عناوين الحرق **لا تُستثنى هنا** بل عند اللقطة: رصيد عنوان الصفر معلومةٌ
    (كم حُرق فعلاً)، والاستثناء موضعه حساب النسب لا الدفتر.
    """
    out: dict[str, dict[str, tuple[int, int, int | None]]] = {}
    for log in logs:
        rec = evm_rpc.decode_transfer(log)
        if rec is None:
            continue
        token = rec["token_address"]
        per_token = out.setdefault(token, {})
        for holder, delta in ((rec["from"], -rec["value"]), (rec["to"], rec["value"])):
            if delta == 0:
                continue
            prev_delta, prev_block, first_receive = per_token.get(holder, (0, 0, None))
            if delta > 0 and (first_receive is None or rec["block"] < first_receive):
                first_receive = rec["block"]
            per_token[holder] = (
                prev_delta + delta, max(prev_block, rec["block"]), first_receive,
            )
    return out


def build_evm_concentration_row(
    stats: dict[str, Any],
    top: Sequence[tuple[str, int]],
    token_address: str,
    network_id: str,
    recorded_at: str,
    watch_first_seen_at: str,
    entry_signal_id: str | None,
    is_control: int = 0,
    decimals: int | None = None,
) -> dict[str, Any] | None:
    """إحصاء الدفتر + أعلى الأرصدة → صفّ `chain_concentration`.

    نفس الجدول الذي تكتبه طبقة سولانا، فعائلة ميزات `onchain_*` القائمة تغطّي
    EVM بلا كود ميزات جديد — والعمود الوحيد الذي يفترق هو `holder_count`
    (مضبوط هنا، ويبقى NULL على سولانا حيث المصدر يعيد 20 حساباً بحدّ أقصى).

    دفتر فارغ ⇒ `None` لا صفّ أصفار: عملة لم تُعبَّأ بعد ليست عملةً بلا حائزين
    (FR-007). و`supply` مجموع الأرصدة الحيّة لا `totalSupply()`: النِّسب تُحسب
    على ما يمكن بيعه فعلاً، فعملة حُرق نصفها تظهر بتركّزها الحقيقيّ.

    والعمود `supply` هنا **تشخيصيّ لا ميزة** (لا تقرأه أي دالّة في
    `features.py`)، فتقريب الفاصلة العائمة فيه لا يضرّ — والقيمة المضبوطة محفوظة
    نصّاً في `raw_json.supply_base`. أمّا النِّسب فتُحسب بأعداد بايثون الصحيحة قبل
    التحويل، فلا تفقد شيئاً.
    """
    supply = int(stats.get("supply") or 0)
    holder_count = int(stats.get("holder_count") or 0)
    if holder_count == 0 and supply == 0:
        return None
    amounts = sorted((int(v) for _, v in top), reverse=True)
    row: dict[str, Any] = {
        "token_address": token_address,
        "network_id": network_id,
        "recorded_at": recorded_at,
        "watch_first_seen_at": watch_first_seen_at,
        "entry_signal_id": entry_signal_id,
        "is_control": 1 if is_control else 0,
        # العرض بوحدات العرض إن عُرفت المنازل، وبالوحدة الأساسيّة إن لم تُعرف —
        # والنِّسب لا تتأثّر (بسط ومقام بنفس الوحدة).
        "supply": (supply / (10 ** decimals)) if decimals else float(supply),
        "decimals": decimals,
        "holder_count": holder_count,
        "top_accounts": len(amounts),
        "raw_json": {
            "source": "evm_ledger",
            "supply_base": str(supply),
            "holder_count": holder_count,
            "top": [[a, str(v)] for a, v in top],
        },
    }
    for _, col in _CHAIN_TIERS:
        row[col] = None
    if supply > 0 and amounts:
        for n, col in _CHAIN_TIERS:
            row[col] = 100.0 * sum(amounts[:n]) / supply
    return row


async def _apply_network(
    rpc: Any, db: RecorderDB, network_id: str, recorded_at: str,
    stats: dict[str, int], sleep,
) -> None:
    """الخطوة 1 لشبكة واحدة: من المؤشّر إلى (الرأس − تأكيدات)."""
    generation = db.evm_ledger_generation()
    all_watched = db.evm_watched([network_id])
    if not all_watched:
        return
    expected_done = [
        str(w["token_address"]).lower()
        for w in all_watched if (w.get("backfill_status") or "") == "done"
    ]
    head = await rpc.block_number(network_id)
    target = head - config.EVM_CONFIRMATIONS
    if target <= 0:
        return

    cursor = db.evm_cursor(network_id)
    if cursor is None:
        # أوّل تشغيل: لا مدًى يُطبَّق. المؤشّر يوضع عند الهدف والتعبئة تتكفّل
        # بالتاريخ كلّه — إذ لا معنى لتطبيق «من صفر» هنا: التعبئة تفعله لكل
        # عملة بسقف نداءات، أمّا هنا فمرشّح واحد لكل الشبكة قد يقصّ مراراً.
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            db.assert_evm_cursor(network_id, None)
            db.assert_evm_done_tokens(network_id, expected_done)
            db.set_evm_cursor(network_id, target, recorded_at, "ok")
        stats["evm_cursor_init"] += 1
        return
    watched = [
        w for w in all_watched if (w.get("backfill_status") or "") == "done"
    ]
    if not watched:
        # لا دفتر مكتمل يحتاج تطبيقاً، فيجوز تقديم الحدّ إلى الهدف ثم تعبئة
        # العملات الجديدة إليه. وهي مستثناة من التطبيق أعلاه، فلا يتكرر المدى.
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            db.assert_evm_cursor(network_id, int(cursor["last_block"]))
            db.assert_evm_done_tokens(network_id, expected_done)
            db.set_evm_cursor(network_id, target, recorded_at, "ok")
        return
    addresses = [w["token_address"].lower() for w in watched]
    start = int(cursor["last_block"]) + 1
    if start > target:
        return  # لا كتلة جديدة (شبكة بطيئة أو دورة سبقت وقتها)

    batch_size = config.EVM_ADDRESS_BATCH.get(str(network_id), 5)
    calls = 0
    fetched: list[tuple[list[dict[str, Any]], int]] = []
    for chunk in _batches(addresses, batch_size):
        if calls:
            await sleep(config.EVM_PACING_SECONDS)
        logs, used, complete, resume = await rpc.get_logs_paged(
            network_id, chunk, start, target,
            max_calls=max(1, config.EVM_APPLY_MAX_CALLS - calls), sleep=sleep,
        )
        calls += used
        reached = target if complete else resume - 1
        fetched.append((logs, reached))

    # لا يجوز تطبيق دفعة إلى أبعد ممّا أكملته أبطأ دفعة: المؤشّر واحد للشبكة، ولو
    # كتبنا مستقبل دفعة مكتملة ثم أعدناه لأن أختها تأخّرت تضاعفت تحويلاتها.
    common_reached = min((reached for _, reached in fetched), default=target)
    applied = 0
    # الأرصدة والمؤشّر معاملة واحدة. سقوط العملية بينهما كان يعيد نفس السجلّات
    # عند الإقلاع التالي ويضاعف الدفتر بلا أثر ظاهر.
    with db.batch():
        db.assert_evm_ledger_generation(generation)
        db.assert_evm_cursor(network_id, int(cursor["last_block"]))
        db.assert_evm_done_tokens(network_id, addresses)
        for logs, _ in fetched:
            safe_logs = [
                log for log in logs
                if (evm_rpc._num(log.get("blockNumber")) or -1) <= common_reached
            ]
            for token, deltas in _deltas_by_token(safe_logs).items():
                applied += db.evm_apply_transfers(
                    network_id, token, deltas, recorded_at,
                    allow_negative=evm_rpc.BURN_ADDRESSES,
                )
        db.set_evm_cursor(
            network_id, max(int(cursor["last_block"]), common_reached), recorded_at,
            "ok", logs_applied=applied,
        )
    stats["evm_logs"] += applied
    stats["evm_calls"] += calls
    if common_reached < target:
        stats["evm_lagging"] += 1


async def _backfill_token(
    rpc: Any, db: RecorderDB, watch: dict[str, Any], recorded_at: str,
    stats: dict[str, int], sleep, deadline: float | None = None,
    max_calls: int | None = None,
) -> None:
    """الخطوة 2 لعملة واحدة: كل تاريخها حتى المؤشّر الحاليّ.

    الحدّ الأعلى هو مؤشّر الشبكة **لحظةَ بدء التعبئة**، وهو ما يمنع التطبيق
    المزدوج: كل كتلة فوقه ستأتي من الخطوة 1، وكل كتلة تحته تأتي من هنا.
    """
    generation = db.evm_ledger_generation()
    net = str(watch["network_id"])
    token = watch["token_address"].lower()
    cursor = db.evm_cursor(net)
    if cursor is None:
        return  # لا مؤشّر بعد ⇒ لا حدّ أعلى معروف؛ الدورة القادمة
    to_block = int(cursor["last_block"])
    state = watch.get("backfill_status")
    expected_from = watch.get("from_block")
    expected_to = watch.get("to_block")
    from_block = config.EVM_BACKFILL_FROM_BLOCK
    if state in ("partial", "retry") and watch.get("from_block") is not None:
        # `retry` هنا كـ`partial` **إلزاماً** لا تحسيناً: نقطة الاستئناف تبقى
        # محفوظة عند الفشل العابر (`COALESCE` في `set_evm_backfill_state`)،
        # والبدء من الصفر مع بقائها يعني إعادة تطبيق مدًى مطبَّق ⇒ **مضاعفة كل
        # رصيد فيه**. والنقطة لا تُكتب إلّا بعد نداء ناجح، فهي دائماً حدٌّ صادق.
        from_block = int(watch["from_block"])  # استئناف من حيث توقّف السقف
        # العملة الجزئية مستثناة من التطبيق الحي، لذا يجب أن تقرأ حتى مؤشر بداية
        # هذه الدورة لا حتى هدف قديم ينمو الرأس بنفس سرعته ويبقيها `partial` أبداً.
        to_block = max(to_block, int(watch.get("to_block") or to_block))
    creation_due = state is None or watch.get("from_block") is None
    # لسؤال «متى نشأت هذه العملة؟» طريقان بحسب ما تحفظه العقدة، والشبكة في أحدهما
    # لا كليهما: الأرشيف يسمح ببحث ثنائيّ على `eth_getCode`، وحيث لا أرشيف
    # (روبن‑هود تحفظ ~128 كتلة) يجيب مرشّح السكّ في نداء واحد.
    origin_networks = (
        *config.EVM_CREATION_BLOCK_NETWORKS, *config.EVM_MINT_SCAN_NETWORKS,
    )
    if (
        not creation_due
        and net in origin_networks
        and int(watch.get("backfill_transfers") or 0) == 0
    ):
        ledger = db.evm_ledger_stats(net, token, exclude=evm_rpc.BURN_ADDRESSES)
        creation_due = int(ledger.get("holder_count") or 0) == 0
    if creation_due and net in config.EVM_MINT_SCAN_NETWORKS:
        # القياس: ثلاث من أربع عملات روبن‑هود سُكَّت فوق 67% من السلسلة، أي 27–37
        # **مليون** كتلة فارغة كانت تُمشى قبل أوّل تحويل. و`None` تعني «لم يُعرَف»
        # فنبقى على `EVM_BACKFILL_FROM_BLOCK`: حدٌّ أدنى خاطئ أسوأ من مشيٍ طويل،
        # لأنّ حائزاً استلم قبله يظهر رصيده سالباً ⇒ العملة كلّها تُرفض.
        minted = await rpc.first_mint_block(net, token, to_block)
        if minted is not None:
            from_block = max(from_block, minted)
    elif creation_due and net in config.EVM_CREATION_BLOCK_NETWORKS:
        creation = await rpc.contract_creation_block(net, token, to_block)
        if creation is not None:
            from_block = max(from_block, creation)
    try:
        logs, calls, complete, resume = await rpc.get_logs_paged(
            net, [token], from_block, to_block,
            max_calls=(config.EVM_BACKFILL_MAX_CALLS if max_calls is None else max_calls),
            sleep=sleep, deadline=deadline,
        )
    except EVMLogLimit as exc:
        # كتلة واحدة تفوق السقف — لا قسمة ممكنة. تُسجَّل ولا تُعاد كل دقيقة.
        with db.batch():
            db.assert_evm_ledger_generation(generation)
            db.assert_evm_backfill_state(
                net, token, state, expected_from, expected_to,
            )
            db.set_evm_backfill_state(
                net, token, "error", recorded_at,
                last_error=f"EVMLogLimit: {exc}"[:300],
            )
        stats["evm_backfill_errors"] += 1
        return

    # إذا تقدم مؤشر الشبكة أثناء تعبئة هذه العملة فلا يكفي إنهاء المدى القديم:
    # كانت العملة مستثناة من التطبيق الحي، لذا يجب أن تلحق الفجوة قبل `done`.
    latest_cursor = db.evm_cursor(net)
    latest_block = int(latest_cursor["last_block"]) if latest_cursor else to_block
    caught_up = complete and to_block >= latest_block
    state_status = "done" if caught_up else "partial"
    next_from = (
        to_block + 1 if complete else resume
    )
    next_to = max(to_block, latest_block) if complete else to_block

    with db.batch():
        db.assert_evm_ledger_generation(generation)
        db.assert_evm_backfill_state(
            net, token, state, expected_from, expected_to,
        )
        if caught_up:
            # `done` ينقل ملكية الكتل اللاحقة إلى التطبيق الحي. إن تحرك المؤشر
            # بعد فحص اللحاق وقبل التثبيت فالفجوة تضيع، لذا نحرس الانتقال النهائي
            # فقط؛ `partial` يبقى قابلاً للتقدم بالتوازي دون تجويع.
            db.assert_evm_cursor(net, latest_block)
        transfers = 0
        for tok, deltas in _deltas_by_token(logs).items():
            if tok != token:
                continue  # مرشّح بعنوان واحد؛ أي غيره ردٌّ لا نثق به
            transfers += db.evm_apply_transfers(
                net, tok, deltas, recorded_at,
                allow_negative=evm_rpc.BURN_ADDRESSES,
            )
        # الرصيد ونقطة الاستئناف وحدة ذرية. تثبيت أحدهما دون الآخر يجعل الإقلاع
        # التالي يعيد نفس المدى ويضاعف كل رصيد فيه.
        db.set_evm_backfill_state(
            net, token, state_status, recorded_at,
            from_block=next_from, to_block=next_to,
            transfers=int(watch.get("backfill_transfers") or 0) + transfers,
            calls=calls,
        )
    stats["evm_backfill_calls"] += calls
    if caught_up:
        stats["evm_backfilled"] += 1
    else:
        stats["evm_backfill_partial"] += 1


def _snapshot_token(
    db: RecorderDB, watch: dict[str, Any], recorded_at: str, stats: dict[str, int],
) -> None:
    """الخطوة 3 لعملة واحدة: لقطة تركّز من الدفتر، بلا نداء شبكة."""
    if (watch.get("backfill_status") or "") != "done":
        raise ValueError("لا يمكن أخذ لقطة من دفتر EVM غير مكتمل")
    generation = db.evm_ledger_generation()
    net = str(watch["network_id"])
    token = watch["token_address"].lower()
    with db.batch():
        db.assert_evm_ledger_generation(generation)
        db.assert_evm_backfill_state(
            net, token, watch.get("backfill_status"),
            watch.get("from_block"), watch.get("to_block"),
        )
        ledger = db.evm_ledger_stats(net, token, exclude=evm_rpc.BURN_ADDRESSES)
        top = db.evm_top_balances(net, token, 20, exclude=evm_rpc.BURN_ADDRESSES)
        row = build_evm_concentration_row(
            ledger, top, token, net, recorded_at,
            watch["first_seen_at"], watch.get("entry_signal_id"),
            int(watch.get("is_control") or 0),
        )
        if row is None:
            db.set_chain_state(token, net, "empty", None, recorded_at)
        else:
            db.insert_chain_concentration(row)
            db.set_chain_state(token, net, "ok", row["top1_pct"], recorded_at)
    if row is None:
        stats["evm_snap_empty"] += 1
        return
    stats["evm_snapshots"] += 1


async def run_evm_cycle(
    rpc: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep,
) -> dict[str, int]:
    """دورة كاملة: تطبيق ثمّ تعبئة ثمّ لقطة، لكل شبكة مفعَّلة.

    خطأ شبكة واحدة لا يُسقط البقيّة، وخطأ عملة واحدة لا يُسقط شبكتها — نفس حرس
    `chain_layer`. والحالة تُكتب في `evm_block_cursor.last_error` و
    `meta.last_error_evm` معاً: الأولى للتشخيص لكل شبكة، والثانية لأنّ لوحة
    القيادة تقرأ `meta` وحدها.
    """
    stats = {
        "evm_networks": 0, "evm_calls": 0, "evm_logs": 0, "evm_cursor_init": 0,
        "evm_lagging": 0, "evm_backfill_due": 0, "evm_backfilled": 0,
        "evm_backfill_partial": 0, "evm_backfill_calls": 0,
        # عابر وقابل للإعادة (مهلة، كتم، عقدة تعثّرت) مقابل دائم لا قسمة تنجيه:
        # عدّادان لأنّ الأوّل يهدأ وحده والثاني يحتاج يداً.
        "evm_backfill_retry": 0, "evm_backfill_errors": 0,
        # عملات أُخِّرت لأنّ ميزانية الزمن انتهت — لا فشل: الدورة القادمة تأخذها.
        "evm_backfill_skipped": 0,
        "evm_snapshots": 0, "evm_snap_empty": 0,
        "evm_errors": 0,
    }

    networks = [str(n) for n in config.EVM_NETWORKS]
    if not networks:
        return stats

    # ١) التطبيق الدوريّ — نداء لكل دفعة عناوين لكل شبكة.
    for net in networks:
        stats["evm_networks"] += 1
        try:
            await _apply_network(rpc, db, net, recorded_at, stats, sleep)
        except StaleEVMState:
            # عامل آخر أكمل تعبئة أو حرّك المؤشر أثناء نداء الشبكة. حالته هي
            # الحقيقة؛ لا نحوله إلى خطأ تشغيلي ولا نكتب فوق تقدمه.
            continue
        except Exception as exc:  # noqa: BLE001 — شبكة واحدة لا تُسقط الدورة
            stats["evm_errors"] += 1
            msg = f"{type(exc).__name__}: {exc}"[:300]
            db.note_error("last_error_evm", f"{recorded_at}: [{net}] {msg}")

    watched = db.evm_watched(networks)

    # ٢) التعبئة — الأقدم انتظاراً أوّلاً، بسقف عملات في الدورة.
    # `retry` في القائمة و`error` ليست: الفرق بينهما هو الفرق بين عطبٍ عابر
    # (مهلة، كتم، عقدة تعثّرت) وعطبٍ دائم (كتلة واحدة تفوق السقف فلا قسمة
    # تنجيها). ودمجهما في «خطأ» واحد يُخرج العملة من الدفتر **إلى الأبد** بسبب
    # ثانيةٍ سيّئة — وقع فعلاً في أوّل دورة حيّة: 3 من 58 عملة.
    pending = [
        w for w in watched
        if (w.get("backfill_status") or None) in (None, "partial", "retry")
    ]
    # آخرُ من جُرِّب آخرُ من يُجرَّب: الترتيب بوقت المحاولة يجعل العابرَ يعود في
    # ذيل الطابور بلا عمود «كم مرّة» ولا مؤقّت.
    pending.sort(key=lambda w: (w.get("backfill_last_try_at") or "",))
    stats["evm_backfill_due"] = len(pending)
    # ميزانية زمنيّة للخطوة كلّها: التعبئة هي الوحيدة التي يجوز قطعها (تُستأنف
    # من نقطتها بلا فقدان سجلّ)، وما بعدها في نفس الدورة ونفس العمليّة يخسر
    # إيقاعه إن أكلت التعبئةُ الفترة — الخطوة ٣ (اللقطات) هنا، ثمّ `bsc_layer`
    # و`evm_contract` و`evm_replay` في `run_evm_replay.run_cycle`. مقيس: 118
    # ثانية والفترة 60. (وليست «سولانا»: `chain_layer` عمليّةٌ أخرى — صُحّح
    # 08-22، والتفصيل عند `EVM_BACKFILL_BUDGET_SECONDS` في config.)
    deadline = time.monotonic() + config.EVM_BACKFILL_BUDGET_SECONDS
    for i, w in enumerate(pending[: config.EVM_BACKFILL_TOKENS_PER_CYCLE]):
        if i and time.monotonic() >= deadline:
            stats["evm_backfill_skipped"] += 1
            continue
        generation = db.evm_ledger_generation()
        try:
            await _backfill_token(rpc, db, w, recorded_at, stats, sleep, deadline)
        except StaleEVMState:
            # عامل آخر أو reset سبقنا. حالته هي الحقيقة؛ لا نكتب retry فوقها.
            continue
        except Exception as exc:  # noqa: BLE001
            stats["evm_backfill_retry"] += 1
            try:
                with db.batch():
                    db.assert_evm_ledger_generation(generation)
                    db.assert_evm_backfill_state(
                        w["network_id"], w["token_address"],
                        w.get("backfill_status"), w.get("from_block"),
                        w.get("to_block"),
                    )
                    db.set_evm_backfill_state(
                        w["network_id"], w["token_address"], "retry", recorded_at,
                        last_error=f"{type(exc).__name__}: {exc}"[:300],
                    )
            except StaleEVMState:
                stats["evm_backfill_retry"] -= 1
                continue
            db.note_error(
                "last_error_evm",
                f"{recorded_at}: {w['token_address']}: {type(exc).__name__}: {exc}",
            )
        if i + 1 < min(len(pending), config.EVM_BACKFILL_TOKENS_PER_CYCLE):
            await sleep(config.EVM_PACING_SECONDS)

    # ٣) اللقطة — للمعبَّأة وحدها. لقطة عملة نصف معبَّأة رقمٌ كاذب لا رقم ناقص.
    # والقراءة تُعاد بعد التعبئة لا تُؤخَذ من `watched` أعلاه: تلك حالةٌ قبل
    # الخطوة 2، فعملة اكتملت تعبئتها الآن تبدو فيها ناقصةً وتخسر دورةً بلا سبب.
    ready = [
        w for w in db.evm_watched(networks)
        if (w.get("backfill_status") or "") == "done"
    ]
    stale_before = (
        datetime.fromisoformat(recorded_at)
        - timedelta(seconds=config.EVM_SNAPSHOT_SECONDS)
    ).isoformat()
    due = db.evm_snapshot_due(
        limit=config.EVM_SNAPSHOT_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=stale_before,
        networks=networks,
    )
    ready_keys = {(w["token_address"].lower(), str(w["network_id"])): w for w in ready}
    for w in due:
        key = (str(w["token_address"]).lower(), str(w["network_id"]))
        target = ready_keys.get(key)
        if target is None:
            continue
        try:
            _snapshot_token(db, target, recorded_at, stats)
        except StaleEVMState:
            continue
        except Exception as exc:  # noqa: BLE001
            stats["evm_errors"] += 1
            db.note_error(
                "last_error_evm",
                f"{recorded_at}: {w['token_address']}: {type(exc).__name__}: {exc}",
            )
    return stats


async def run_evm_backfill_assist(
    rpc: Any, db: RecorderDB, networks: Sequence[str], recorded_at: str | None = None,
    sleep=asyncio.sleep,
) -> dict[str, int]:
    """تعبئة الدفاتر الحيّة فقط، لتعمل كأولوية قبل الإعادة التاريخية.

    لا يطبّق السجلّ الدوري ولا يكتب لقطات؛ `FomoChain` يبقى مالك هاتين الخطوتين.
    هذا العامل يسرّع العملات الجديدة من دون إنشاء مسار ثانٍ لتطبيق نفس المدى.
    """
    now = recorded_at or utcnow_iso()
    stats = {
        "evm_backfill_due": 0, "evm_backfilled": 0, "evm_backfill_partial": 0,
        "evm_backfill_calls": 0, "evm_backfill_retry": 0,
        "evm_backfill_errors": 0, "evm_backfill_skipped": 0,
    }
    watched = db.evm_watched([str(n) for n in networks])
    pending = [
        w for w in watched
        if (w.get("backfill_status") or "") in ("", "partial", "retry")
    ]
    pending.sort(key=_assist_priority)
    stats["evm_backfill_due"] = len(pending)
    deadline = time.monotonic() + config.EVM_BACKFILL_ASSIST_BUDGET_SECONDS
    for index, watch in enumerate(pending[:config.EVM_BACKFILL_TOKENS_PER_CYCLE]):
        if index and time.monotonic() >= deadline:
            stats["evm_backfill_skipped"] += 1
            continue
        generation = db.evm_ledger_generation()
        try:
            await _backfill_token(
                rpc, db, watch, now, stats, sleep, deadline,
                max_calls=config.EVM_REPLAY_MAX_CALLS,
            )
        except StaleEVMState:
            continue
        except Exception as exc:  # noqa: BLE001 - the next cycle retries this token
            stats["evm_backfill_retry"] += 1
            try:
                with db.batch():
                    db.assert_evm_ledger_generation(generation)
                    db.assert_evm_backfill_state(
                        watch["network_id"], watch["token_address"],
                        watch.get("backfill_status"), watch.get("from_block"),
                        watch.get("to_block"),
                    )
                    db.set_evm_backfill_state(
                        watch["network_id"], watch["token_address"], "retry", now,
                        last_error=f"{type(exc).__name__}: {exc}"[:300],
                    )
            except StaleEVMState:
                stats["evm_backfill_retry"] -= 1
        if index + 1 < min(len(pending), config.EVM_BACKFILL_TOKENS_PER_CYCLE):
            await sleep(config.EVM_PACING_SECONDS)
    return stats


def _assist_priority(watch: dict[str, Any]) -> tuple[int, int, int, str]:
    """الجديد أولاً، ثم الأقل كلفة والأقرب؛ العامل الحي يحافظ على الدوران العادل."""
    if not watch.get("backfill_status"):
        return (0, 0, 0, "")
    start = watch.get("from_block")
    end = watch.get("to_block")
    remaining = (
        max(0, int(end) - int(start) + 1)
        if start is not None and end is not None else 2 ** 63 - 1
    )
    calls = int(watch.get("backfill_calls") or 2 ** 31 - 1)
    return (1, calls, remaining, str(watch.get("backfill_last_try_at") or ""))
