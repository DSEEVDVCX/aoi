"""عامل الإعادة التاريخية المجدول لقياسات تركّز حائزي EVM.

يعمل منفصلاً عن `FomoChain`: الالتقاط الحي لا ينتظر الأرشيف القديم، والإعادة
تلتقط عملة واحدة في الدورة وتستأنف من `evm_replay_state` بعد أي توقف.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import bsc_layer
import evm_contract
import evm_replay
import evm_layer

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _log(message: str) -> None:
    import config
    from db import utcnow_iso

    try:
        with open(config.EVM_REPLAY_RUN_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{utcnow_iso()} {message}\n")
    except OSError:
        pass


def _next_network(db, networks: tuple[str, ...]) -> tuple[str, str]:
    """يعيد الشبكة الحالية والتالية، مع استرداد آمن من إعداد قديم."""
    saved = str(db.get_meta("evm_replay_next_network") or "")
    current_index = networks.index(saved) if saved in networks else 0
    current = networks[current_index]
    following = networks[(current_index + 1) % len(networks)]
    return current, following


def _stamp(db, pairs: dict) -> None:
    """أختامٌ دفتريّة تُكتب إن أمكن ولا تُسقط دورةً نجحت.

    `set_meta` عاريةً هنا كانت تجعل قفلَ القاعدة يُسجَّل «cycle crashed» عن دورةٍ
    تمّت وكُتبت صفوفُها فعلاً. أسوأُ ما تفقده هذه الطريقة هو موضعُ الدوران بين
    الشبكات، فتُعاد نفس الشبكة مرّةً — وهو أرخص من كذبةٍ في السجلّ واللوحة.
    """
    for key, value in pairs.items():
        db.note_error(key, value)


# ما ليس عدّاد عمل: اسمُ الشبكة، عددُ الشبكات المطلوبة، المرفوضة منها، والزمن.
_NOT_WORK = frozenset({"network", "networks", "refused_networks", "seconds"})


def _worked(stats: dict) -> bool:
    """هل جرى عملٌ أو خطأ في هذه الدورة؟ **بالاستثناء لا بالتعداد**.

    كان الشرط `stats["tokens"] or stats["errors"]`، وفرعُ المساعدة الحيّة يعيد
    مفاتيح أخرى تماماً (`evm_backfill_*`) ويرجع قبل أن يصل إلى تلك — فبقي السجلّ
    صامتاً أربع ساعات والعامل يعمل سليماً، واحتاج التشخيص قراءة `meta` بدلاً منه.
    قائمةُ مفاتيحٍ مسموحة كانت ستُكرّر العطب عند أوّل مفتاح جديد، فنستثني ما ليس
    عدّاداً ونعدّ الباقي: المفتاح الجديد يُحسب تلقائياً.
    """
    for key, value in stats.items():
        if key in _NOT_WORK or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value:
            return True
    return False


async def run_cycle(rpc, db, nodereal=None) -> dict:
    import config

    from db import utcnow_iso

    # This is the sole EVM writer. Keeping live application, backfill,
    # snapshots, contract checks, and historical replay in one connection
    # prevents concurrent SQLite writers from holding incompatible batches.
    live = await evm_layer.run_evm_cycle(rpc, db, utcnow_iso())
    if nodereal is not None:
        live.update(await bsc_layer.run_bsc_cycle(nodereal, db, utcnow_iso()))
    live.update(await evm_contract.run_evm_contract_cycle(rpc, db, utcnow_iso()))
    networks = tuple(str(network) for network in config.EVM_REPLAY_NETWORKS)
    if not networks:
        return {**live, "tokens": 0, "written": 0, "errors": 0, "network": None}

    network, following = _next_network(db, networks)
    stats = await evm_replay.run_replay(
        rpc,
        db,
        networks=[network],
        limit=max(1, int(config.EVM_REPLAY_TOKENS_PER_CYCLE)),
        log=_log,
        budget_seconds=config.EVM_REPLAY_BUDGET_SECONDS_PER_CYCLE,
    )
    now = utcnow_iso()
    combined = {**live, **stats, "network": network}
    live_ok = not int(live.get("evm_errors") or 0) and not int(
        live.get("evm_backfill_errors") or 0
    )
    contract_ok = not int(live.get("evm_contract_errors") or 0)
    bsc_ok = "bsc_errors" in live and not int(live.get("bsc_errors") or 0)
    _stamp(db, {
        "evm_last_run_at": now,
        "evm_last_stats": str(live),
        "evm_replay_next_network": following,
        "evm_replay_last_run_at": now,
        "evm_replay_last_stats": str(combined),
        **({"evm_last_ok_at": now} if live_ok else {}),
        **({"evm_contract_last_ok_at": now} if contract_ok else {}),
        **({"bsc_nodereal_last_ok_at": now} if bsc_ok else {}),
        **({"evm_replay_last_ok_at": now} if not stats.get("errors") else {}),
    })
    return combined


def _check_config() -> int:
    import config
    from db import RecorderDB

    for name in (
        "EVM_REPLAY_NETWORKS", "EVM_REPLAY_INTERVAL_SECONDS",
        "EVM_REPLAY_TOKENS_PER_CYCLE", "EVM_REPLAY_BUDGET_SECONDS_PER_CYCLE",
        "EVM_REPLAY_RUN_LOG_PATH", "EVM_REPLAY_HEARTBEAT_SECONDS",
        "EVM_CREATION_BLOCK_NETWORKS",
        "EVM_BACKFILL_ASSIST_NETWORKS", "EVM_BACKFILL_ASSIST_BUDGET_SECONDS",
        "EVM_REPLAY_HEAD_GRACE_SECONDS",
    ):
        if not hasattr(config, name):
            print(f"config.{name} مفقود", file=sys.stderr)
            return 1
    if not config.EVM_REPLAY_NETWORKS:
        print("لا توجد شبكات إعادة EVM مفعَّلة", file=sys.stderr)
        return 1
    if not os.path.exists(config.DB_PATH):
        print(f"قاعدة البيانات غير موجودة: {config.DB_PATH}", file=sys.stderr)
        return 1
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        pending = sum(
            1 for row in db.evm_replay_targets(config.EVM_REPLAY_NETWORKS)
            if (row.get("replay_status") or "") not in
            evm_replay.FINAL_STATUSES
        )
    finally:
        db.close()
    # لا سطرَ مفاتيح هنا: المسارُ كلّه على العقد الرسميّة بلا مفتاح (انظر
    # `GOLDRUSH_REPLAY_CHAINS` المحذوف في config والمصيدة #29). فإن أُضيف
    # مزوّدٌ بمفتاح يوماً فليُضَف عدُّه هنا — **عدداً لا قيمة** (FR-013).
    print(
        f"ok · شبكات: {','.join(map(str, config.EVM_REPLAY_NETWORKS))}"
        f" · عملة/دورة: {config.EVM_REPLAY_TOKENS_PER_CYCLE}"
        f" · معلّق: {pending}"
        f" · نبضة السجلّ: {config.EVM_REPLAY_HEARTBEAT_SECONDS}ث"
    )
    return 0


async def _main(cycles: int | None = None) -> None:
    import config
    import evm_rpc
    from nodereal_rpc import NodeRealRPC
    from db import RecorderDB, utcnow_iso

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    rpc = evm_rpc.EVMRPC()
    nodereal = NodeRealRPC()
    count = 0
    # `None` لا `monotonic()`: أوّل دورة تسجّل دائماً مهما كانت خاملة، فسطرُ
    # الإقلاع هو الدليل الوحيد على أنّ العامل نهض بعد إعادة التشغيل.
    last_logged: float | None = None
    try:
        while cycles is None or count < cycles:
            started = time.monotonic()
            try:
                stats = await run_cycle(rpc, db, nodereal)
                if _worked(stats):
                    _log(f"cycle: {stats}")
                    last_logged = started
                elif last_logged is None or (
                    started - last_logged >= config.EVM_REPLAY_HEARTBEAT_SECONDS
                ):
                    # نبضة: «حيٌّ ولا عمل مستحقّ». الصمت التامّ يشبه الموت تماماً.
                    _log(f"idle: {stats}")
                    last_logged = started
            except Exception as exc:  # noqa: BLE001 - one cycle must not kill the worker
                import traceback

                _log("cycle crashed:\n" + traceback.format_exc())
                # الإنقاذُ **قبل** الختم: لقطةُ قراءةٍ سُبقت في WAL تردّ كلَّ
                # كتابةٍ من هذا الاتّصال بـ`database is locked` بلا أن تنفع
                # المهلة، فلو كُتب `note_error` أوّلاً سقط هو أيضاً وضاع السطر
                # الوحيد الذي تعرضه اللوحة. وهذا العامل أخطرُ الأربعة على هذا
                # الباب: `run_cycle` كلُّها اتّصالٌ واحد بأربع طبقاتٍ كاتبة.
                # التفصيل والحادثة المقيسة في `db.recover_connection`.
                try:
                    _log(f"connection recovery: {db.recover_connection()}")
                except Exception as rec_exc:  # noqa: BLE001 — يد إنقاذ لا تُسقط الحلقة
                    _log(f"connection recovery failed: {type(rec_exc).__name__}")
                # وفي `meta` أيضاً: السجلُّ ملفٌّ على القرص لا يقرأه أحد، واللوحة
                # كانت تعرض كل طابور إلّا هذا — فتعثّرٌ دائم هنا كان صامتاً
                # مرّتين. سطرٌ واحد بالنوع والرسالة، والأثر الكامل في السجلّ.
                db.note_error(
                    "last_error_evm_replay",
                    f"{utcnow_iso()}: {type(exc).__name__}: {exc}"[:400],
                )
                # التعثّر سطرٌ أيضاً ⇒ يؤجّل النبضة: أثرُ الانهيار أبلغ منها.
                last_logged = started
            # ولا تقريرَ أحواضٍ لهذا المالك: لا مفتاح على مسار الإعادة، وصفٌّ
            # فارغ في اللوحة أسوأ من غيابه. (كان `provider_keys_replay` يحمل
            # حوض GoldRush وحده، فحُذف الصفُّ مع المزوّد.)
            count += 1
            if cycles is not None and count >= cycles:
                break
            remaining = config.EVM_REPLAY_INTERVAL_SECONDS - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
    finally:
        await rpc.aclose()
        await nodereal.aclose()
        db.close()


def main() -> None:
    if "--check-config" in sys.argv:
        raise SystemExit(_check_config())
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    asyncio.run(_main(cycles))


if __name__ == "__main__":
    main()
