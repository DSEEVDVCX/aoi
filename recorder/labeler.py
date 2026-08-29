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
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import config
from db import RecorderDB, utcnow_iso
from features import epoch_of

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
    admission_price_usd: float | None = None,
) -> dict[str, Any]:
    """شموع (مرتّبة تصاعدياً) + لحظة دخول → قاموس النتائج.

    دالة خالصة: لا قاعدة ولا شبكة ولا ساعة نظام — قابلة للاختبار حتمياً.
    تعيد دائماً قاموساً فيه `status`:
      ok         — دخول وشموع بعده؛ كل الحقول المطلوبة محسوبة.
      no_entry   — لا شمعة دخول خلال المهلة (فجوة سحب لحظة الإشارة).
      no_bars    — شمعة دخول بلا أي شمعة بعدها (العملة ماتت فوراً).
      incomplete — توجد شموع، لكن مقياس مطلوب لا يمكن اشتقاقه منها.
    """
    window_h = window_h or config.LABEL_WINDOW_HOURS
    window_end = entry_ts + window_h * 3600
    out: dict[str, Any] = {
        "entry_px": None, "entry_lag_s": None,
        **{f"max_gain_{h}h": None for h in _GAIN_WINDOWS},
        "max_drawdown_48h": None, "final_return_48h": None,
        "time_to_peak_h": None, "candles_48h": 0, "suspect_bars": 0,
        "last_bar_lag_h": None, "bars_truncated": None, "is_rug": None,
        # (fv15) ليبل الانفجار: قمة ≥2x **و** +50% منها متاحة خلال أول 24س —
        # قياس 2026-08-28 على 2,378 انفجارًا: 82-96% من الانفجارات الحقيقية
        # تبلغ نصف قمتها في يومها الأول (كلما ضخم الانفجار زادت النسبة)،
        # والفائتة (345) وسيط ما كان متاحًا منها خلال 24س هو +26% فقط —
        # صيد هامشي، فالليبل يفرّز «قابلًا للالتقاط» من «قمة متأخرة».
        "is_explosive": None,
        # (fv15) سنارة إعلان القوة: دقائق حتى أول إغلاق ≥ +20% — **فلتر
        # إسقاط مرشحين فقط، لا قاعدة دخول أبدًا** (قرار المالك 2026-08-28
        # بعد قياس: كل الدخول بعد +20% خاسر صافيًا -1% إلى -11% لأنك تشتري
        # بسعر أعلى فيبقى جزء صغير من الصعود وكامل الهبوط). الاستخدام
        # الوحيد الصالح: عملة لم تبلغ +20% خلال ~4 ساعات ⇒ احتمال انفجارها
        # ضعيف (وسيط المنفجرة 259د مقابل 816د لغيرها) — تُسقط من المراقبة.
        "time_to_plus20_min": None,
    }

    # شمعة الدخول: أولى الشموع عند/بعد اللحظة، ضمن مهلة قصوى.
    # الإغلاق المشوّه لا يصلح سعر دخول (شوهد إغلاق 12,052.5 بين إغلاقين ~0.0004)
    # فنتخطّاه إلى أوّل إغلاق سليم — ما زال ضمن نفس شرط المهلة.
    entry_bar = next(
        (b for b in bars if b["ts"] >= entry_ts and not b.get("c_suspect")), None
    )
    use_admission_price = admission_price_usd is not None
    entry_too_late = entry_bar is None or (
        entry_bar["ts"] - entry_ts > config.LABEL_ENTRY_MAX_LAG_SECONDS
    )
    if not use_admission_price and entry_too_late:
        out["status"] = "no_entry"
        return out
    entry_px = float(admission_price_usd) if use_admission_price else float(entry_bar["c"])
    if not math.isfinite(entry_px) or entry_px <= 0:
        # سعر صفري/غير محدود يجعل كل النسب لا-نهائية — لا نفبرك.
        out["status"] = "no_entry"
        return out
    out["entry_px"] = entry_px
    out["entry_lag_s"] = 0 if use_admission_price else int(entry_bar["ts"] - entry_ts)

    # النافذة: بعد شمعة الدخول حصراً وحتى نهاية 48 ساعة.
    entry_bar_ts = entry_ts if use_admission_price else entry_bar["ts"]
    window = [b for b in bars if entry_bar_ts < b["ts"] <= window_end]
    out["candles_48h"] = len(window)
    if not window:
        out["status"] = "no_bars"
        return out

    # الذيول المستحيلة من المنبع تُستبعد من القمّة/القاع فقط — لا تُصلَح ولا
    # تُحذف شمعتها (o/c سليمان ويُستعملان). بلا هذا الاستبعاد كان ذيل واحد
    # (h = 119 مليون ضعف إغلاقه) يجعل max_gain مليارات النسب المئوية.
    hi_ok = [b for b in window if not b.get("h_suspect")]
    lo_ok = [b for b in window if not b.get("l_suspect")]
    close_ok = [b for b in window if not b.get("c_suspect")]
    out["suspect_bars"] = sum(
        1 for b in window
        if b.get("h_suspect") or b.get("l_suspect") or b.get("c_suspect")
    )

    for h in _GAIN_WINDOWS:
        sub = [b for b in hi_ok if b["ts"] <= entry_ts + h * 3600]
        if sub:
            out[f"max_gain_{h}h"] = max(float(b["h"]) for b in sub) / entry_px - 1

    if lo_ok:
        out["max_drawdown_48h"] = min(float(b["l"]) for b in lo_ok) / entry_px - 1
    if hi_ok:
        peak_bar = max(hi_ok, key=lambda b: float(b["h"]))
        out["time_to_peak_h"] = (peak_bar["ts"] - entry_ts) / 3600
    # العائد النهائي من آخر إغلاق **سليم**؛ وطزاجة السلسلة من آخر شمعة مرصودة
    # (وجود الشمعة دليل حياة حتى لو تشوّه إغلاقها).
    last_bar = window[-1]
    if close_ok:
        out["final_return_48h"] = float(close_ok[-1]["c"]) / entry_px - 1
        out["is_rug"] = 1 if out["final_return_48h"] <= config.LABEL_RUG_THRESHOLD else 0

    # (fv15) الليبلان الجديدان — من نفس الشموع المحسوبة أعلاه، بلا كلفة:
    # الانفجار يستعمل الذرى السليمة (hi_ok) لا الإغلاقات، والسنارة عكسه.
    # ⚠️ time_to_plus20_min ليس قاعدة دخول أبدًا (قياس المالك 2026-08-28):
    # الدخول بعد +20% خاسر صافيًا في كل النوافذ — استخدامه الوحيد فلترُ
    # إسقاط مرشحين (من لم تبلغ +20% خلال ~4س ⇒ انفجارها غير مرجّح).
    peak_48 = out["max_gain_48h"]
    if peak_48 is not None and out["max_gain_24h"] is not None:
        out["is_explosive"] = 1 if (
            peak_48 >= config.EXPLOSIVE_MIN_PEAK
            and out["max_gain_24h"] >= config.EXPLOSIVE_HALF_AT_24H
        ) else 0
    for b in close_ok:
        if float(b["c"]) >= entry_px * (1.0 + config.PLUS20_THRESHOLD):
            out["time_to_plus20_min"] = (b["ts"] - entry_ts) / 60
            break
    out["last_bar_lag_h"] = (window_end - last_bar["ts"]) / 3600
    out["bars_truncated"] = 1 if out["last_bar_lag_h"] > 1.0 else 0
    required = ("final_return_48h", "max_gain_24h", "is_rug")
    out["status"] = (
        "ok" if all(out[field] is not None for field in required) else "incomplete"
    )
    return out


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _iso_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def token_age_days(
    created_at: Any, now_iso: str, observed_at: Any = None,
) -> float | None:
    if observed_at is not None:
        observed = epoch_of(observed_at)
        now = epoch_of(now_iso)
        if observed is None or now is None or observed > now:
            return None
    created = epoch_of(created_at)
    now = epoch_of(now_iso)
    if created is None or now is None:
        return None
    age = (now - created) / 86400.0
    return age if age >= 0 else None


def label_pending(
    db: RecorderDB, now_epoch: int, batch: int | None = None
) -> dict[str, int]:
    """يوسم كل ما نضجت نافذته ولم يُوسَم — الإشارات ثمّ دخولات المراقبة ثمّ
    أحداث النشاط الرجعية (المسدَّاة شموعها فقط — انظر activities_pending_label).

    تزايديّ و idempotent: كل صفّ يُوسَم مرّة واحدة إلى الأبد؛ إعادة التشغيل
    تلتقط من حيث توقّفت. غير الناضج يُتخطّى بصمت ويُلتقط في دورة لاحقة.
    """
    batch = batch or config.LABEL_BATCH
    mature_before = (
        now_epoch - config.LABEL_WINDOW_HOURS * 3600 - config.LABEL_MARGIN_SECONDS
    )
    stats = {"signals": 0, "watches": 0, "activities": 0,
             "ok": 0, "no_entry": 0, "no_bars": 0, "incomplete": 0}
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
            "labeled_at": labeled_at, "design_version": 1,
            "analysis_eligible": 0,
            "exclusion_reason": "signal_outcome_not_phase1_watch",
            **labels,
        })
        stats["signals"] += 1
        stats[labels["status"]] += 1

    for w in db.watches_pending_label(mature_before, batch):
        entry = w["entry_epoch"]
        bars = db.bars_for(
            w["token_address"], str(w["network_id"] or ""),
            entry, entry + config.LABEL_WINDOW_HOURS * 3600,
        )
        labels = compute_labels(
            bars, entry, admission_price_usd=w.get("admission_price_usd")
        )
        age_at_entry = token_age_days(
            w.get("token_created_at"), _iso_from_epoch(w["entry_epoch"]),
            w.get("token_created_at_observed_at"),
        )
        gate_applies = (
            w["design_version"] >= 3
            and _epoch(w["first_seen_at"]) >= _epoch(config.AGE_GATE_ENABLED_AT)
        )
        age_observed_at_entry = (
            not gate_applies
            or (
                epoch_of(w.get("token_created_at_observed_at")) is not None
                and epoch_of(w.get("token_created_at_observed_at")) <= entry
            )
        )
        age_ok = (
            True if not gate_applies or not config.MIN_TOKEN_AGE_DAYS
            else (
                age_observed_at_entry
                and age_at_entry is not None
                and age_at_entry >= config.MIN_TOKEN_AGE_DAYS
            )
        )
        analysis_eligible = (
            w["design_version"] >= 3
            and labels["status"] != "incomplete"
            and age_ok
        )
        db.insert_outcome({
            "kind": "watch", "key": w["key"],
            "token_address": w["token_address"],
            "network_id": str(w["network_id"] or ""),
            "signal_type": w["source"], "is_control": w["is_control"],
            "is_independent": None,  # مفهوم الاستقلال يخصّ الإشارات المتتالية
            "entry_ts": entry,
            "split": assign_split(w["token_address"]),
            "labeled_at": labeled_at, "design_version": w["design_version"],
            "analysis_eligible": 1 if analysis_eligible else 0,
            "exclusion_reason": (
                ("age_gate_at_entry" if (
                    age_observed_at_entry and age_at_entry is not None
                ) else "age_unknown_at_entry")
                if gate_applies and not age_ok else (
                    None if analysis_eligible else (
                        "incomplete_metrics" if labels["status"] == "incomplete"
                        else "superseded_comparison_design"
                    )
                )
            ),
            **labels,
        })
        stats["watches"] += 1
        stats[labels["status"]] += 1

    # أحداث النشاط الرجعية (backfill_activity): نفس الحساب ونفس علم الاستقلال،
    # لكن is_control=0 دائماً — لا ضابطة رجعية ممكنة (لا نعرف الكون غير المُشار
    # إليه تاريخياً)، وهذا قيد موثّق على أي مقارنة ضابطة تشملها.
    for a in db.activities_pending_label(mature_before, batch):
        entry = a["entry_epoch"]
        bars = db.bars_for(
            a["token_address"], str(a["network_id"] or ""),
            entry, entry + config.LABEL_WINDOW_HOURS * 3600,
        )
        labels = compute_labels(bars, entry)
        gap_ok = (
            a["prev_ts"] is None
            or entry - _epoch(a["prev_ts"]) >= config.LABEL_INDEPENDENCE_GAP_SECONDS
        )
        db.insert_outcome({
            "kind": "activity", "key": a["id"],
            "token_address": a["token_address"],
            "network_id": str(a["network_id"] or ""),
            "signal_type": a["event_type"], "is_control": 0,
            "is_independent": 1 if gap_ok else 0,
            "entry_ts": entry,
            "split": assign_split(a["token_address"]),
            "labeled_at": labeled_at, **labels,
        })
        stats["activities"] += 1
        stats[labels["status"]] += 1

    return stats
