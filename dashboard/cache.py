"""ذاكرةٌ مؤقّتة للاستعلامات الثقيلة — تُقدَّم البائتةُ فوراً وتُجدَّد في الخلف.

المشكلة المقيسة: `/api/networks` كان يستهلك 2.2 ثانية من أصل 2.2 ثانية لكلّ
تحديثٍ للوحة، لأنّ عقدةً واحدة فيه تمسح 3,073,284 سطراً من `market_ticks` لتُخرج
**خمسة أسطر**. والصفحة تسأل كلَّ عشر ثوانٍ، فذلك 22٪ من نواةٍ محجوزةً دائماً
لرقمٍ مضغوطٍ لا يتغيّر معناه في دقيقتين.

ولماذا «تُقدَّم البائتة» ولا يُكتفى بعُمرٍ محدّد؟ لأنّ ذاكرةً ساذجةً بعُمر 120
ثانية تنقل العطب لا تُزيله: أحدَ عشر تحديثاً يُجاب من الذاكرة، والثاني عشر يدفع
الثانيتين كاملةً وينتظر المتصفّح. أمّا هنا فالطلبُ لا ينتظر أبداً بعد أوّل حساب:
يأخذ آخرَ قيمةٍ معروفة ويرجع، ويجري التجديدُ في خيطٍ خلفيّ. الثمنُ المقبول أنّ
الرقم قد يتأخّر ثانيةً أو ثانيتين عن التجديد — وهو رقمٌ عمرُه دقيقتان أصلاً.

وثلاثة حدودٍ مقصودة:

- **حسابٌ واحد لا ثلاثة عشر.** الصفحة تُطلق طلباتها كلَّها متوازيةً، فبلا قفلٍ
  لكلّ مفتاح كان أوّلُ تحديثٍ بعد الإقلاع يُشعل نفسَ المسح مرّاتٍ في آنٍ واحد.
- **فشلُ التجديد لا يُفقد القيمة.** قاعدةٌ مشغولة تُسقط تجديداً، فنُبقي القديمةَ
  ونسجّل الخطأ ونتمهّل قبل إعادة المحاولة — وإلّا صار كلُّ طلبٍ محاولةً فاشلة.
- **لا كتابة.** هذه ذاكرةُ عمليّة اللوحة وحدها: لا جدولَ تخزينٍ مؤقّت، ولا ختمَ
  في `meta`، ولا فهرسَ يُبنى. القاعدةُ تبقى `mode=ro` كما هي.

والمُدخَلة لا تُعدَّل بعد نشرها (`frozen`): الفشلُ يستبدلها بنسخةٍ لا يُغيّرها في
مكانها، فقارئٌ يحمل مرجعاً إليها لا يرى نصفَ تحديث.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class _Entry:
    """قيمةٌ محسوبة وزمنُها. لا تُعدَّل بعد النشر — تُستبدل بنسخةٍ معدّلة."""

    value: Any
    computed_at: float          # ساعةٌ رتيبة (monotonic) — للعمر
    computed_wall: float        # ساعةُ الحائط — للعرض فقط
    error: str | None = None
    failed_at: float | None = None


class _State:
    __slots__ = ("lock", "entry", "refreshing")

    def __init__(self) -> None:
        self.lock = threading.Lock()      # يُسلسل الحسابَ الباردَ وحدَه
        self.entry: _Entry | None = None
        self.refreshing = False


def _spawn_thread(run: Callable[[], None]) -> None:
    threading.Thread(target=run, daemon=True).start()


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat()


class TTLMemo:
    """ذاكرةٌ بمفاتيح، لكلّ مفتاحٍ عمرُه ومنطقُ تجديدِه.

    الساعاتُ والمُشعِلُ قابلةٌ للحقن كي تُختبر البياتةُ والتجديدُ بلا انتظارٍ
    حقيقيّ ولا سباقِ خيوط: اختبارٌ ينتظر ثانيتين ليرى انتهاءَ العمر اختبارٌ
    هشّ، وآخرُ يعتمد على جدولة الخيوط اختبارٌ يفشل مرّةً من عشر.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        spawn: Callable[[Callable[[], None]], None] = _spawn_thread,
        error_backoff: float = 15.0,
    ) -> None:
        self._clock = clock
        self._wall = wall_clock
        self._spawn = spawn
        self._error_backoff = float(error_backoff)
        self._lock = threading.Lock()
        self._states: dict[str, _State] = {}

    # --- الواجهة ---
    def get(
        self,
        key: str,
        ttl: float,
        compute: Callable[[], Any],
        *,
        error_backoff: float | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """يعيد (القيمة، وصفَ طزاجتها). لا ينتظر إلّا إن لم يكن هناك قيمةٌ بعد.

        `compute` بلا مُعامِلات وتفتح اتصالَها بنفسها: التجديدُ يجري في خيطٍ
        خلفيّ بعد أن يُغلق الطلبُ الذي أشعله اتصالَه، فتمريرُ اتصالِ الطلب كان
        سيُستعمل بعد إغلاقه.
        """
        backoff = self._error_backoff if error_backoff is None else float(error_backoff)
        now = self._clock()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _State()
                self._states[key] = state
            entry = state.entry
            spawn_refresh = False
            meta: dict[str, Any] | None = None
            if entry is not None:
                age = now - entry.computed_at
                if age >= ttl and not state.refreshing and (
                    entry.failed_at is None or now - entry.failed_at >= backoff
                ):
                    spawn_refresh = True
                    state.refreshing = True
                meta = self._describe(entry, age, ttl, refreshing=state.refreshing)

        if entry is not None and meta is not None:
            if spawn_refresh:
                self._start_refresh(key, compute)
            return entry.value, meta

        # بارد: لا شيء يُقدَّم، فلا مفرّ من الانتظار — وواحدٌ فقط يحسب.
        with state.lock:
            with self._lock:
                existing = state.entry
                refreshing = state.refreshing
            if existing is not None:
                age = self._clock() - existing.computed_at
                return existing.value, self._describe(
                    existing, age, ttl, refreshing=refreshing
                )
            fresh = self._store(key, compute())
        return fresh.value, self._describe(fresh, 0.0, ttl, refreshing=False)

    def warm(self, key: str, ttl: float, compute: Callable[[], Any]) -> bool:
        """يُسخّن مفتاحاً بلا إسقاط منادٍ. للاستدعاء من خيطٍ خلفيّ عند الإقلاع."""
        try:
            self.get(key, ttl, compute)
        except Exception:  # noqa: BLE001 — تسخينٌ فاشل ليس عطباً في اللوحة
            return False
        return True

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._states.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    # --- الداخل ---
    def _describe(
        self, entry: _Entry, age: float, ttl: float, *, refreshing: bool
    ) -> dict[str, Any]:
        return {
            "computed_at": _iso(entry.computed_wall),
            "age_seconds": round(max(0.0, age), 1),
            "ttl_seconds": round(float(ttl), 1),
            "stale": age >= ttl,
            "refreshing": refreshing,
            "error": entry.error,
        }

    def _store(self, key: str, value: Any) -> _Entry:
        entry = _Entry(value=value, computed_at=self._clock(), computed_wall=self._wall())
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _State()
                self._states[key] = state
            state.entry = entry
        return entry

    def _note_failure(self, key: str, exc: BaseException) -> None:
        """يُبقي القيمةَ القديمة ويختم الفشل — فلا تصير كلُّ طلبيّةٍ محاولةً فاشلة."""
        detail = f"{type(exc).__name__}: {exc}"[:200]
        with self._lock:
            state = self._states.get(key)
            if state is None or state.entry is None:
                return
            state.entry = replace(state.entry, error=detail, failed_at=self._clock())

    def _start_refresh(self, key: str, compute: Callable[[], Any]) -> None:
        def run() -> None:
            try:
                value = compute()
            except Exception as exc:  # noqa: BLE001 — تجديدٌ فاشل يُسجَّل لا يُرفع
                self._note_failure(key, exc)
            else:
                self._store(key, value)
            finally:
                with self._lock:
                    state = self._states.get(key)
                    if state is not None:
                        state.refreshing = False

        self._spawn(run)


# ذاكرةُ عمليّة اللوحة. واحدةٌ لأنّ العمليّة واحدة، ومُصفّاةٌ في الاختبارات.
MEMO = TTLMemo()
