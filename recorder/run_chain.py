# -*- coding: utf-8 -*-
"""نقطة إطلاق طبقة السلسلة للمهمّة المجدولة FomoChain (pythonw، stderr مخفيّ).

عملية رابعة بجانب المسجّل والموسِّم وبناء الصفوف. لماذا منفصلة ولا خطوة في دورة
المسجّل — انظر مقدّمة `chain_layer.py`: الميزانية ممتلئة، والانفصال هو ما يجعل
إيقاع 5 دقائق ممكناً.

كاتبٌ ثالث على القاعدة، وهو آمن لنفس أسباب `run_build_rows.py`: WAL نشط،
و`RecorderDB` يضبط `timeout=30`، والإدراج `INSERT OR IGNORE` بمفتاح يحمل الوقت
فالتكرار لا يضرّ.

**المفتاح لا يُطبع ولا يُسجَّل** (FR-013): يُقرأ من `recorder/chain_keys.json`
عند كل نداء، وكل رسالة خطأ تمرّ عبر شطب داخل `solana_rpc`. و`--check-config`
يقول «موجود/غائب» ولا يقول قيمته. EVM يملكه `run_evm_replay.py` وحده كي لا
يتنافس كاتبان على SQLite.

الاستخدام:
  python run_chain.py                # حلقة لا نهائية (المهمّة المجدولة)
  python run_chain.py 1              # دورة واحدة (تحقّق يدويّ)
  python run_chain.py --check-config # تحقّق قبل التسجيل في الجدولة
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_BOOT_LOG = os.path.join(HERE, "chain_boot.log")


def _log_boot(msg: str) -> None:
    try:
        with open(_BOOT_LOG, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


def _log(msg: str) -> None:
    import config

    _write_log(config.CHAIN_LOG_PATH, msg)


def _write_log(path: str, msg: str) -> None:
    import config
    from db import utcnow_iso

    line = f"{utcnow_iso()} {msg}\n"
    try:
        if (
            config.LOG_MAX_BYTES > 0
            and os.path.exists(path)
            and os.path.getsize(path) > config.LOG_MAX_BYTES
        ):
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def _stamp(db, pairs: dict[str, str]) -> None:
    """أختامٌ دفتريّة تُكتب إن أمكن ولا تُسقط دورةً نجحت.

    كانت `set_meta` عاريةً في جسم الدورة: قفلُ القاعدة عندها يقفز إلى الحرس
    فيُسجَّل «تعثّرت» ويُكتب `last_error_*` عن دورةٍ **تمّت فعلاً** — كذبٌ في
    اللوحة فوق فقدِ الختم. وبنفس هذا الطريق خرج المسجّل بالرمز 1 يوم
    2026-08-17 (انظر `db.note_error`).
    """
    for key, value in pairs.items():
        db.note_error(key, value)


def _ok_stamps(stats: dict, at: str) -> dict[str, str]:
    """ختمُ «آخر نجاح» لكل طابور على حِدة — لا ختمٌ واحد للعمليّة.

    اللوحة تُعلّم خطأً بأنّه متعافٍ إن سبق آخرَ نجاحٍ لمصدره. وكان الحدُّ
    المستعمل حدَّ **المسجّل** (`last_ok_cycle_at`) وهو لا يكتبه إلّا
    `recorder.py`؛ فموتُ المسجّل جمّد الحدَّ فبقيت شارات chain/evm حمراء إلى
    الأبد مهما أتمّت `FomoChain` دوراتٍ نظيفة. فلكلّ طابورٍ ختمُه من كاتبه.

    القاعدة: صفرُ أخطاءٍ في ذلك الطابور ⇒ ختم — بصرف النظر عن حجم ما استحقّ
    العمل، وهي نفس قاعدة `last_ok_cycle_at` عند المسجّل. اشتراطُ عملٍ فعليّ
    كان سيُبقي طابوراً خامداً (لا عملات على شبكته) أحمرَ للأبد بلا سبيل تعافٍ.
    """
    def _clean(*counters: str) -> bool:
        return not any(int(stats.get(name) or 0) for name in counters)

    out: dict[str, str] = {}
    if "chain_errors" in stats and _clean("chain_errors"):
        out["chain_last_ok_at"] = at
    if "auth_errors" in stats and _clean("auth_errors"):
        out["chain_auth_last_ok_at"] = at
    return out


async def main_loop(cycles: int | None = None) -> None:
    import time

    import asyncio

    import config
    from chain_layer import run_chain_auth_cycle, run_chain_cycle
    from db import RecorderDB, utcnow_iso
    from provider_keys import write_pool_report
    from solana_rpc import ChainKeyMissing, SolanaRPC

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    rpc = SolanaRPC()
    n = 0
    try:
        while cycles is None or n < cycles:
            started = time.time()
            try:
                stats = await run_chain_cycle(rpc, db, utcnow_iso())
                # الطبقة البطيئة في نفس العملية ونفس الاتّصال: طابورها الساعيّ
                # يكتفي بستّ عملات في الدقيقة، فمهمّة مجدولة خامسة لأجلها
                # عمليّةٌ كاملة مقابل ~8 ثوانٍ من دقيقة فارغة أصلاً.
                stats.update(await run_chain_auth_cycle(rpc, db, utcnow_iso()))
                stats["seconds"] = round(time.time() - started, 1)
                now = utcnow_iso()
                _stamp(db, {
                    "chain_last_run_at": now, "chain_last_stats": str(stats),
                    **_ok_stamps(stats, now),
                })
                # نسجّل حين يجري عمل فقط — سطر كل دقيقة إلى الأبد ضجيج.
                if stats["chain_due"] or stats["auth_due"]:
                    _log(f"chain: {stats}")
            except ChainKeyMissing as exc:
                # سطر واحد واضح ثم انتظار: الحلقة لا تموت (المفتاح قد يوضع
                # على القرص لاحقاً بلا إعادة تشغيل المهمّة) ولا تضجّ كل دقيقة.
                _log(f"chain key missing: {exc}")
                db.note_error("last_error_chain", f"{utcnow_iso()}: مفتاح السلسلة غائب")
                await asyncio.sleep(max(config.CHAIN_INTERVAL_SECONDS, 60))
            except Exception:  # noqa: BLE001 — درع الدورة؛ الحلقة لا تموت
                import traceback

                _log("chain cycle crashed:\n" + traceback.format_exc())
            # تقرير الأحواض **خارج** حرس السلسلة: حالة المفاتيح أهمّ ما يُقرأ حين
            # تتعثّر الدورة، فلا يصحّ أن يسقط مع الفرع الذي تعثّر.
            try:
                # وطبقةُ EVM ليست في التقرير: عقدٌ رسميّة بلا مفتاح، فلا حوض.
                write_pool_report(db, "chain", {
                    "helius": rpc.key_stats(),
                }, utcnow_iso())
            except Exception:  # noqa: BLE001 — تقريرٌ لا قياس
                pass
            n += 1
            if cycles is not None and n >= cycles:
                break
            # النوم بقيّة الفترة لا الفترة كاملة: دورة استهلكت 20ث لا تنتظر 60
            # أخرى، وإلّا انزلق الإيقاع الحقيقيّ بعيداً عن المُعلَن.
            rest = config.CHAIN_INTERVAL_SECONDS - (time.time() - started)
            if rest > 0:
                await asyncio.sleep(rest)
    finally:
        await rpc.aclose()
        db.close()


def _check_config() -> int:
    """تحقّق قبل التسجيل في جدولة المهامّ. **لا يطبع المفتاح** (FR-013)."""
    import config
    from db import RecorderDB
    from provider_keys import read_keys

    for name in (
        "CHAIN_INTERVAL_SECONDS", "CHAIN_PER_CYCLE", "CHAIN_REFRESH_SECONDS",
        "CHAIN_ERROR_RETRY_SECONDS", "CHAIN_PACING_SECONDS",
        "CHAIN_TIMEOUT_SECONDS", "CHAIN_NETWORKS", "SOLANA_RPC_URL",
        "CHAIN_TRANSIENT_RETRIES", "CHAIN_TRANSIENT_BACKOFF_SECONDS",
        "CHAIN_MAX_SLOT_LAG",
        "CHAIN_LOG_PATH", "CHAIN_AUTH_PER_CYCLE", "CHAIN_AUTH_REFRESH_SECONDS",
        "CHAIN_AUTH_ERROR_RETRY_SECONDS",
    ):
        if not hasattr(config, name):
            print(f"config.{name} مفقود", file=sys.stderr)
            return 1
    if not os.path.exists(config.DB_PATH):
        print(f"قاعدة البيانات غير موجودة: {config.DB_PATH}", file=sys.stderr)
        return 1

    key_path = config.chain_keys_path()
    helius_keys = read_keys("helius_api_keys", "helius_api_key", "HELIUS_API_KEY")
    key_ok = bool(helius_keys)
    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        row = db._conn.execute(
            "SELECT COUNT(*) n FROM watchlist WHERE active=1 AND network_id IN "
            f"({', '.join('?' for _ in config.CHAIN_NETWORKS)})",
            tuple(config.CHAIN_NETWORKS),
        ).fetchone()
        active = int(row["n"])
    finally:
        db.close()

    sweep = (
        active / config.CHAIN_PER_CYCLE * (config.CHAIN_INTERVAL_SECONDS / 60.0)
        if config.CHAIN_PER_CYCLE else 0.0
    )
    auth_sweep = (
        active / config.CHAIN_AUTH_PER_CYCLE * (config.CHAIN_INTERVAL_SECONDS / 60.0)
        if config.CHAIN_AUTH_PER_CYCLE else 0.0
    )
    print(
        f"{'ok' if key_ok else 'تحذير: لا مفتاح'} · مفتاح: "
        f"{str(len(helius_keys)) + ' موجود' if key_ok else 'غائب — ' + key_path} "
        f"· شبكات: {', '.join(config.CHAIN_NETWORKS)} "
        f"· نشطة عليها: {active} "
        f"· كل {config.CHAIN_INTERVAL_SECONDS}ث × {config.CHAIN_PER_CYCLE} "
        f"-> مسح كامل كل ~{sweep:.1f}د (الهدف "
        f"{config.CHAIN_REFRESH_SECONDS / 60:.0f}د)"
        f" · البطيئة: ×{config.CHAIN_AUTH_PER_CYCLE} -> ~{auth_sweep:.1f}د "
        f"(الهدف {config.CHAIN_AUTH_REFRESH_SECONDS / 60:.0f}د)"
    )
    # **أعداد فقط، بلا أي قيمة** (FR-013). ومفتاحٌ واحد يعني «لا بديل عند الرفض»
    # فيُقال صراحةً: التعدّد موجود في الكود ولا ينفع بحوضٍ من واحد.
    def _count(keys: list[str], *, required: bool) -> str:
        if not keys:
            return "غائب" if required else "غائب (اختياري)"
        return f"{len(keys)}" + (" — بلا بديل عند الرفض" if len(keys) == 1 else "")

    print(
        f"مفاتيح: helius {_count(helius_keys, required=True)} · الملف: {key_path}"
    )
    return 0 if key_ok else 1


def main() -> None:
    import asyncio

    import config  # يضيف api/src إلى sys.path عند الاستيراد

    cycles = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        cycles = int(sys.argv[1])

    _log_boot(f"boot ok, db={config.DB_PATH}, cycles={cycles}")
    asyncio.run(main_loop(cycles=cycles))


if __name__ == "__main__":
    try:
        if "--check-config" in sys.argv:
            raise SystemExit(_check_config())
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — أخطاء الإقلاع قبل بدء الحلقة
        import traceback

        _log_boot("BOOT FAILURE:\n" + traceback.format_exc())
        raise
