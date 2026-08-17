"""Read and rotate local provider keys without exposing their values.

ورصدُها كذلك: الحوض يعيش في ذاكرة العمليّة المالكة، واللوحة عمليّةٌ أخرى تقرأ
القاعدة — فبلا ختمٍ مكتوب لا سبيل لمعرفة «هل هناك مفتاح ثانٍ أصلاً؟» ولا «هل
واحدٌ منها مرفوض الآن؟» إلّا بقراءة سجلٍّ نصّيّ. ولذلك `KeyPool.stats()`
و`pool_report()`: **أعدادٌ ومؤشّرات فقط، بلا أي قيمة مفتاح ولا كسرٍ منها**
(FR-013) — العدد لا يُعاد بناؤه إلى مفتاح، والبصمة تُعاد مطابقتها فلا نصدرها.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from typing import Any

import config


def _values(data: Mapping[str, object], plural: str, singular: str) -> list[str]:
    raw = data.get(plural)
    candidates = raw if isinstance(raw, list) else [data.get(singular)]
    return [value.strip() for value in candidates if isinstance(value, str) and value.strip()]


def read_keys(plural: str, singular: str, env_name: str | None = None) -> list[str]:
    if env_name:
        env = os.environ.get(env_name, "").strip()
        if env:
            return list(dict.fromkeys(value.strip() for value in env.split(",") if value.strip()))
    try:
        with open(config.chain_keys_path(), encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (OSError, ValueError, TypeError):
        return []
    return list(dict.fromkeys(_values(data, plural, singular)))


class KeyPool:
    """Round-robin pool with temporary cooldown for rejected keys."""

    def __init__(self, keys: list[str], cooldown_seconds: float = 60.0) -> None:
        self.keys = list(dict.fromkeys(keys))
        self.cooldown_seconds = cooldown_seconds
        self._index = 0
        self._blocked_until: dict[str, float] = {}
        self._rotations = 0

    def current(self) -> str:
        if not self.keys:
            raise KeyError("no provider keys")
        return self.keys[self._index % len(self.keys)]

    def refresh(self, keys: list[str]) -> None:
        updated = list(dict.fromkeys(keys))
        if updated == self.keys:
            return
        current = self.current() if self.keys else None
        self.keys = updated
        self._blocked_until = {
            key: until for key, until in self._blocked_until.items() if key in updated
        }
        self._index = updated.index(current) if current in updated else 0

    def rotate(self, *, block_current: bool = False) -> str:
        """ينتقل إلى أوّل مفتاح غير مبرَّد، ويعدّ الانتقال.

        الملجأ الأخير مقصود: إن كانت **كلّها** مبرَّدة نعيد التاليَ رغم تبريده
        بدل أن نرفع. المفتاح المبرَّد قد يكون تجاوز حصّته للدقيقة فحسب، ومحاولةٌ
        بمفتاح مرفوضٍ مؤقّتاً أنفعُ من إسقاط الدورة بيقين — والمنادي عليه مهلةٌ
        وحدٌّ للمحاولات يمنعان الدوران بلا نهاية.
        """
        current = self.current()
        if block_current:
            self._blocked_until[current] = time.monotonic() + self.cooldown_seconds
        self._rotations += 1
        for step in range(1, len(self.keys) + 1):
            index = (self._index + step) % len(self.keys)
            key = self.keys[index]
            if time.monotonic() >= self._blocked_until.get(key, 0.0):
                self._index = index
                return key
        self._index = (self._index + 1) % len(self.keys)
        return self.current()

    def blocked_count(self, *, now: float | None = None) -> int:
        """عدد المفاتيح المبرَّدة الآن — لا التي بُرِّدت يوماً (التبريد ينتهي)."""
        moment = time.monotonic() if now is None else now
        return sum(
            1 for key in self.keys if moment < self._blocked_until.get(key, 0.0)
        )

    def stats(self, *, now: float | None = None) -> dict[str, Any]:
        """صورةُ الحوض للكتابة في `meta` — **أعدادٌ ومؤشّرات لا قيم** (FR-013).

        `index` مؤشّرٌ في قائمة، لا يدلّ على قيمةٍ ولا على طولها. و`available`
        محسوبٌ لا مستقلّ كي لا يتناقض الرقمان في العرض.
        """
        blocked = self.blocked_count(now=now)
        return {
            "keys": len(self.keys),
            "blocked": blocked,
            "available": max(0, len(self.keys) - blocked),
            "index": (self._index % len(self.keys)) if self.keys else 0,
            "rotations": self._rotations,
            "cooldown_seconds": round(float(self.cooldown_seconds), 1),
        }


def pool_report(
    pools: Mapping[str, Mapping[str, Any]], *, at: str, owner: str,
) -> str:
    """سطر JSON واحد لصفٍّ في `meta`. المالك في القيمة لا في المفتاح وحده.

    يأخذ صوراً محسوبة (`client.key_stats()`) لا أحواضاً: الحوض خاصّيّة داخليّة
    في العميل، وتمريره خارجاً كان سيفتح طريقاً ثانياً إلى `keys` نفسها.

    كل عمليّة تكتب صفَّها الخاصّ (`provider_keys_<owner>`) فلا تسابُقَ على صفٍّ
    مشترك: حوضُ GoldRush مثلاً يوجد في `FomoChain` و`FomoEVMReplay` معاً بحالتين
    مختلفتين، وصفٌّ واحد لهما كان سيُظهر آخرَ كاتبٍ فقط ويسمّيه الحقيقة.
    """
    return json.dumps(
        {"at": at, "owner": owner, "pools": {name: dict(s) for name, s in pools.items()}},
        ensure_ascii=False,
        sort_keys=True,
    )


def write_pool_report(
    db: Any, owner: str, pools: Mapping[str, Mapping[str, Any]], at: str,
) -> bool:
    """يختم تقرير الأحواض في `meta`. لا يرفع أبداً: تقريرٌ لا قياس.

    يمرّ عبر `note_error` عن قصد — ليست رسالةَ خطأ، لكنّ دلالتها واحدة: كتابةٌ
    دفتريّة لا يجوز أن تُسقط مَن يكتبها إن كانت القاعدة هي المورد المتعطّل
    (انظر `db.note_error`). ولو غاب التابع (قاعدةٌ وهميّة في اختبار) لا نتعثّر.
    """
    try:
        value = pool_report(pools, at=at, owner=owner)
    except Exception:  # noqa: BLE001 — تسلسلٌ فاشل لا يُسقط دورة
        return False
    note = getattr(db, "note_error", None)
    if note is None:
        return False
    return bool(note(f"provider_keys_{owner}", value))
