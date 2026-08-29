"""تسخينُ ذاكرة اللوحة عند الإقلاع — أوّلُ زائرٍ لا يدفع ثمن البرودة.

المشكلة المقيسة (2026-08-29): `serve_dashboard.py` يُقلع بذاكرة `cache.MEMO`
فارغة، فأوّلُ من يفتح الصفحة ينتظر الحساب البارد للمسارات الثقيلة الثلاثة
(3.4 ثانية للشبكات، 1.7 للتوسيم، 1.0 للأعداد — مقيسة على القاعدة الحيّة)،
لأنّ الصفحة تُطلق طلباتها الخمسة عشر متوازيةً وتنتظر أبطأها قبل رسم أيّ شيء.

والحلُّ جاهزٌ في `cache.py` منذ البداية: دالةُ `warm()` موثّقةٌ «للاستدعاء من
خيطٍ خلفيّ عند الإقلاع» لكن لا أحد يستدعيها. هذا الملفُ هو ذلك الاستدعاء.

عقدُّ التسخين:
- **خيطٌ واحدٌ خفيّ (daemon)** يُشعَل بعد بدء الاستماع، لا يمنع انطفاء العمليّة.
- **الترتيبُ بالثقل**: الأبطأ أوّلاً — من يفتح الصفحةَ قبل اكتمال التسخين ينتظر
  أطولَ مسارٍ بلا قيمة، فكلُّ ثانيةٍ تُقتطع من مقدّمة الخيط تُقتطع من انتظاره.
- **الفشلُ لا يوقف**: مفتاحٌ يفشل يُسجَّل ويُمرَّر — التسخينُ مساعاةٌ لا شرطُ
  إقلاع، والقاعدةُ قد تكون مشغولةً بالمسجّل لحظة الإقلاع.
- **لا كتابة ولا مساس بالقاعدة**: `warm()` يمرّ بنفس `get()` — قراءةٌ بـmode=ro
  كما كلّ اللوحة.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import cache
import config

# المفاتيح الثقيلة بالترتيب: أثقلُها أوّلاً (أزمنة مقيسة 2026-08-29).
# أضِف هنا كلَّ مفتاح `_cached` جديدٍ ثقيل — `test_every_heavy_key_is_covered`
# في test_warmup.py يفحص أنّ القائمة لا تنحرف.
HEAVY_KEYS: tuple[tuple[str, float], ...] = (
    ("network_summary", config.NETWORK_SUMMARY_TTL_SECONDS),
    ("labeling", config.LABELING_TTL_SECONDS),
    ("table_counts", config.TABLE_COUNTS_TTL_SECONDS),
)

# `ticks_summary` مسارٌ عامّ يكلّف 1.5 ثانية لكن الصفحة لا تناديه اليوم؛ لا يُسخَّن
# عمداً — تسخينُ ما لا يُعرض إهدارٌ لقرصٍ يتنافسُ عليه المسجّل.

_ComputeFor = Callable[[str], Callable[[], Any]]


def warm_heavy_keys(
    memo: cache.TTLMemo, compute_for: _ComputeFor
) -> dict[str, bool]:
    """يسخّن المفاتيح الثقيلة **بالتوازي** ويعيد خريطة {مفتاح: نجح؟}.

    `compute_for` يحوّل اسمَ المفتاح إلى callable بلا معامِلات يفتح اتصاله
    بنفسه — نفس عقد `cache.TTLMemo.get`. الاستثناءُ من مفتاحٍ يُلتقط ويُمرَّر
    (القيمةُ False في الخريطة) فلا يقتل خيط التسخين قبل البقيّة.

    ولماذا التوازي بعد أن كان الترتيب تتابعيًا؟ المقيس (2026-08-29): من يفتح
    الصفحةَ في أوّل ثواني الإقلاع ينتظر **مجموعَ** المفاتيح السابقة له —
    `labeling` كان يُجاب بعد 3.4+1.5 ثانية من الشبكات والتوسيم معًا. القراءُ
    في SQLite لا يتنازعون أقفالًا (كلُّ اتصالٍ mode=ro)، فالتوازيُّ يجعل كلَّ
    مسارٍ جاهزًا خلال زمنه الخاص، وأسوأَ انتظارٍ ممكن = أبطأُ مفتاحٍ واحد.
    """
    done: dict[str, bool] = {}
    threads: list[threading.Thread] = []

    def _warm_one(key: str, ttl: float) -> None:
        done[key] = memo.warm(key, ttl, compute_for(key))

    for key, ttl in HEAVY_KEYS:
        thread = threading.Thread(
            target=_warm_one, args=(key, ttl), daemon=True,
            name=f"dashboard-warmup-{key}",
        )
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    return done


def _dao_compute(key: str) -> Callable[[], Any]:
    """يبني دالة الحساب البارد للمفتاح — نفس نداءات المسارات في app.py."""
    import dao

    def _compute() -> Any:
        conn = dao.connect_ro(config.DB_PATH)
        try:
            if key == "network_summary":
                return dao.network_summary(conn)
            if key == "labeling":
                return dao.labeling_outcomes(
                    conn,
                    config.LIVE_START_TS,
                    design_version=config.CONTROL_DESIGN_VERSION,
                    gate_targets=(
                        config.CONTROL_PRELIMINARY_TARGET,
                        config.CONTROL_DECISION_TARGET,
                    ),
                )
            if key == "table_counts":
                return dao.table_counts(conn)
            raise KeyError(f"مفتاح تسخين غير معروف: {key}")
        finally:
            conn.close()

    return _compute


# قابلٌ للاستبدال في الاختبارات (test_warmup_thread_is_daemon_and_runs_after_start)
def _spawn_background(run: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=run, daemon=True, name="dashboard-warmup")
    thread.start()
    return thread


def start_warmup_thread(memo: cache.TTLMemo | None = None) -> threading.Thread:
    """يشعل خيط التسخين ويعيد الخيط. فشلُ تسخينٍ يُسجَّل ولا يُرفع."""
    target_memo = memo if memo is not None else cache.MEMO

    def _run() -> None:
        warm_heavy_keys(target_memo, _dao_compute)

    return _spawn_background(_run)
