"""الموسِّم (labeler): يحسب النتائج (labels) من الشموع بعد اكتمال النافذة.

الفصل عن المسجّل مبدئيّ لا تنظيميّ: المسجّل يسجّل خاماً بختم زمنيّ فقط، وأي
حساب لقيمة مشتقّة من المستقبل وقت التسجيل تسرّبٌ. الموسِّم يعمل في عملية
منفصلة (FomoLabeler) ولا يلمس إشارة إلّا بعد `entry + 48h + هامش` — عندها
"المستقبل" صار ماضياً مؤرشفاً.

ضمانات عدم التسرّب في الحساب نفسه:
- سعر الدخول = إغلاق **أوّل شمعة عند أو بعد** لحظة الإشارة (لا قبلها — وإلّا
  تسرّبٌ معكوس)، وبتأخّر أقصاه 30 دقيقة وإلّا `status=no_entry`.
- القمم والقيعان من الشموع **التالية بعد شمعة الدخول حصراً**: قمّة شمعة الدخول
  نفسها قد تكون حدثت قبل تنفيذنا الافتراضيّ فلا تُحسب مكسباً.
- `bars_truncated` يعلّم السلسلة التي انتهت مبكراً بدل إسقاطها: العملة الميّتة
  **إشارة لا نقص** — استبعادها يُدخل انحياز البقاء (متوثّق في fomo-getbars).

التقسيم (split) حتميّ بتجزئة عنوان العملة — كل إشارات العملة الواحدة في نفس
القسم دائماً، وإلّا حفظ النموذجُ العملات (14.2 إشارة/عملة) وصار التحقّق وهماً.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import config
from db import RecorderDB, utcnow_iso

# نوافذ المكاسب الجزئية (بالساعات) — أعمدة max_gain_*
_GAIN_WINDOWS = (1, 4, 24, 48)


def assign_split(token_address: str) -> str:
    """train/val/test حتمياً من تجزئة العنوان (70/10/20).

    - **بالعملة لا بالصفّ**: كل إشارات العملة في قسم واحد — منع تسرّب الحفظ.
    - **حتميّ بلا حالة**: نفس العنوان نفس القسم في أي تشغيل وعلى أي جهاز،
      بلا جدول يُفقد أو يُعاد توليده.
    - lower() لتوحيد عناوين EVM المتفاوتة الحالة؛ عناوين سولانا حسّاسة للحالة
      لكنّ توحيدها هنا لا يضرّ (أسوأ حالة: عملتان مختلفتان في قسم واحد).
    """
    digest = hashlib.sha1(token_address.strip().lower().encode()).hexdigest()
    bucket = int(digest[:8], 16) % 10
    if bucket <= 6:
        return "train"
    if bucket == 7:
        return "val"
    return "test"


def compute_labels(
    bars: Sequence[Mapping[str, Any]],
    entry_ts: int,
    window_h: int | None = None,
) -> dict[str, Any]:
    """شموع (مرتّبة تصاعدياً) + لحظة دخول → قاموس النتائج.

    دالة خالصة: لا قاعدة ولا شبكة ولا ساعة نظام — قابلة للاختبار حتمياً.
    تعيد دائماً قاموساً فيه `status`:
      ok       — دخول وشموع بعده؛ كل الحقول محسوبة.
      no_entry — لا شمعة دخول خلال المهلة (فجوة سحب لحظة الإشارة).
      no_bars  — شمعة دخول بلا أي شمعة بعدها (العملة ماتت فوراً).
    """
    window_h = window_h or config.LABEL_WINDOW_HOURS
    window_end = entry_ts + window_h * 3600
    out: dict[str, Any] = {
        "entry_px": None, "entry_lag_s": None,
        **{f"max_gain_{h}h": None for h in _GAIN_WINDOWS},
        "max_drawdown_48h": None, "final_return_48h": None,
        "time_to_peak_h": None, "candles_48h": 0,
        "last_bar_lag_h": None, "bars_truncated": None, "is_rug": None,
    }

    # شمعة الدخول: أولى الشموع عند/بعد اللحظة، ضمن مهلة قصوى.
    entry_bar = next((b for b in bars if b["ts"] >= entry_ts), None)
    if entry_bar is None or entry_bar["ts"] - entry_ts > config.LABEL_ENTRY_MAX_LAG_SECONDS:
        out["status"] = "no_entry"
        return out
    entry_px = float(entry_bar["c"])
    if entry_px <= 0:  # سعر صفريّ يجعل كل النسب لا-نهائية — لا نفبرك
        out["status"] = "no_entry"
        return out
    out["entry_px"] = entry_px
    out["entry_lag_s"] = int(entry_bar["ts"] - entry_ts)

    # النافذة: بعد شمعة الدخول حصراً وحتى نهاية 48 ساعة.
    window = [b for b in bars if entry_bar["ts"] < b["ts"] <= window_end]
    out["candles_48h"] = len(window)
    if not window:
        out["status"] = "no_bars"
        return out

    for h in _GAIN_WINDOWS:
        sub = [b for b in window if b["ts"] <= entry_ts + h * 3600]
        if sub:
            out[f"max_gain_{h}h"] = max(float(b["h"]) for b in sub) / entry_px - 1

    out["max_drawdown_48h"] = min(float(b["l"]) for b in window) / entry_px - 1
    peak_bar = max(window, key=lambda b: float(b["h"]))
    out["time_to_peak_h"] = (peak_bar["ts"] - entry_ts) / 3600
    last_bar = window[-1]
    out["final_return_48h"] = float(last_bar["c"]) / entry_px - 1
    out["last_bar_lag_h"] = (window_end - last_bar["ts"]) / 3600
    out["bars_truncated"] = 1 if out["last_bar_lag_h"] > 1.0 else 0
    out["is_rug"] = 1 if out["final_return_48h"] <= config.LABEL_RUG_THRESHOLD else 0
    out["status"] = "ok"
    return out


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def label_pending(
    db: RecorderDB, now_epoch: int, batch: int | None = None
) -> dict[str, int]:
    """يوسم كل ما نضجت نافذته ولم يُوسَم — الإشارات ثمّ دخولات المراقبة.

    تزايديّ و idempotent: كل صفّ يُوسَم مرّة واحدة إلى الأبد؛ إعادة التشغيل
    تلتقط من حيث توقّفت. غير الناضج يُتخطّى بصمت ويُلتقط في دورة لاحقة.
    """
    batch = batch or config.LABEL_BATCH
    mature_before = (
        now_epoch - config.LABEL_WINDOW_HOURS * 3600 - config.LABEL_MARGIN_SECONDS
    )
    stats = {"signals": 0, "watches": 0, "ok": 0, "no_entry": 0, "no_bars": 0}
    labeled_at = utcnow_iso()

    for s in db.signals_pending_label(mature_before, batch):
        entry = s["entry_epoch"]
        bars = db.bars_for(
            s["token_address"], str(s["network_id"] or ""),
            entry, entry + config.LABEL_WINDOW_HOURS * 3600,
        )
        labels = compute_labels(bars, entry)
        gap_ok = (
            s["prev_ts"] is None
            or entry - _epoch(s["prev_ts"]) >= config.LABEL_INDEPENDENCE_GAP_SECONDS
        )
        db.insert_outcome({
            "kind": "signal", "key": s["id"],
            "token_address": s["token_address"],
            "network_id": str(s["network_id"] or ""),
            "signal_type": s["signal_type"], "is_control": 0,
            "is_independent": 1 if gap_ok else 0,
            "entry_ts": entry,
            "split": assign_split(s["token_address"]),
            "labeled_at": labeled_at, **labels,
        })
        stats["signals"] += 1
        stats[labels["status"]] += 1

    for w in db.watches_pending_label(mature_before, batch):
        entry = w["entry_epoch"]
        bars = db.bars_for(
            w["token_address"], str(w["network_id"] or ""),
            entry, entry + config.LABEL_WINDOW_HOURS * 3600,
        )
        labels = compute_labels(bars, entry)
        db.insert_outcome({
            "kind": "watch", "key": w["key"],
            "token_address": w["token_address"],
            "network_id": str(w["network_id"] or ""),
            "signal_type": w["source"], "is_control": w["is_control"],
            "is_independent": None,  # مفهوم الاستقلال يخصّ الإشارات المتتالية
            "entry_ts": entry,
            "split": assign_split(w["token_address"]),
            "labeled_at": labeled_at, **labels,
        })
        stats["watches"] += 1
        stats[labels["status"]] += 1

    return stats
