"""مستخرج الميزات — الأرشيف المبعثر ⇒ جدول تدريب واحد (المرحلة 2).

**القانون الواحد الذي يحكم هذا الملفّ**: كل رقم في الصفّ يجب أن يكون قابلاً
للمعرفة **عند t=0 أو قبلها**. كل استعلام هنا مقيَّد زمنياً صراحةً، والاختبارات
تزرع بيانات بعد t0 وتتأكّد أنّها لا تظهر. خطأ واحد هنا يسمّم النموذج بصمت
ويعطي دقّة وهمية لا تُكتشف إلّا بالمال.

**قطعة واحدة للتدريب والحيّ**: نفس `build_row` يُستدعى لبناء صفّ تاريخيّ وصفّ
إشارة حيّة لاحقاً — اختلاف الحساب بين التدريب والتشغيل (train/serve skew) يهدم
النموذج بلا أن يظهر في أيّ مقياس.

الأعمدة الغائبة تبقى None (FR-007: لا فبركة صفر — الغياب نفسه معلومة
يتعلّمها LightGBM).
"""
from __future__ import annotations

import json
import math
import statistics as st
from datetime import UTC, datetime
from typing import Any

import config
from db import RecorderDB
from extract import classify_asset

# سقف عيّنة الأطروحات المخزّنة: الترقيم الرجعيّ يجلب صفحات بحدّ أقصى (100/صفحة)
# فيتوقّف العدّ عند ~400 لعملة قد تحمل 29,595 أطروحة حقيقية. الصفوف عند السقف
# تُعلَّم بـ`thesis_counted_capped` كي لا يقرأ النموذج عدداً مشبَّعاً كأنّه قياس.
_THESIS_PAGE_CAP = 380
# 4: عائلة الملكية (تركيز السلسلة + تموضع حشد المنصّة) وشرعية خارجية
#    (منصّات/CMC/وصف/بانر) — استعلامات التدريب تشترط feature_version >= 2،
#    والصفوف القديمة تبقى صالحة مع NULL في الجديد (غائب ≠ صفر).
# 5: تصحيح mintable/freezable. كانا NULL في 100% من الصفوف: المصدر يعيد
#    **عنوان** سلطة السكّ/التجميد لا قيمة منطقية، والمحوِّل السابق كان يُسقط
#    كل نصّ إلى None. بعد التصحيح: سولانا 312/312 مقيسة (56 بسلطة سكّ قائمة،
#    19 بسلطة تجميد)، وEVM تبقى NULL بحقّ — لا سلطة بهذا المعنى هناك، فالصفر
#    كان سيفبرك «آمن» لعملة لم تُقَس (FR-007).
# 6: تصحيح holder_authors ⇒ social_holder_authors و social_holder_ratio. كانا
#    ثابتين على صفر في 46,040 صفّاً لأنّ المستخرِج قرأ `equity` وهو صفر صحيح في
#    28,186/28,186 أطروحة مقيسة (حقل ميت من المنبع). المركز الحقيقي في
#    `authorTrade.humanTokenAmount`: بعد التعبئة الرجعية 99 قيمة مميّزة على مدى
#    0-100، و95.3% من اللقطات فيها حائز واحد على الأقل. عمودان بلا معلومة صارا
#    قياساً — والنسبة موزّعة فعلاً (قمّة 30-60%، وذيل 906 لقطة كلّ كتّابها مالكون).
# 7: عائلة صدارات المدد. سقف المصدر 50 متصدّراً في الصدارة الأساسيّة وكل صيغ
#    الترقيم مُهمَلة بصمت (مقيس: تسع صيغ، قوائم متطابقة بايتاً)، لكنّ /24h و/7d
#    و/30d تعيد كلٌّ **100** فاتّحاد الأربع 214 متداولاً (164 لا تعرفهم
#    الأساسيّة) — على 7,200 حدث شراء حقيقيّ من ثلاثة أيام: مطابقة 265 (3.68%)
#    ← 1,099 (15.26%)، أي ×4.15. الرتب **منفصلة بالمدّة** لا مدموجة (رتبة 7 في
#    24h ليست رتبة 7 في totalPnL)، ومداها هنا 1-100 لا 1-50 فـ`rank_le_50`
#    يستعيد تباينه على `best_rank_any_period`. الماضي لا يُعبّأ: الأرشيف حفظ
#    totalPnL وحدها فلا تاريخ لرتب المدد، والصفوف القديمة تبقى NULL بحقّ —
#    وهذا بالضبط نمط الحِقبة الذي يكشفه prune_dead_features، فالعائلة تُسقَط
#    تلقائياً من التدريب حتى تُقاس في نصفَي المجموعة (أسابيع).
FEATURE_VERSION = 7


# ---------------------------------------------------------------------------
# أدوات
# ---------------------------------------------------------------------------
def epoch_of(value: Any) -> int | None:
    """ختم زمنيّ → epoch. يقبل ISO (بصيغة fomo Z أو المسجّل +00:00) **و**
    epoch رقمياً (ثوانٍ أو مللي).

    ضروريّ لأنّ fomo لا توحّد الصيغ: `token_static.token_created_at` مخزَّن
    epoch رقمياً (1784983617) بينما `signal_events.ts` نصّ ISO — وافتراض ISO
    وحده جعل `token_age_h` فارغاً 100% حتى كشفه تقرير التغطية.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value).strip()
        if text.replace(".", "", 1).isdigit():
            num = float(text)
        else:
            try:
                return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
            except (ValueError, TypeError):
                return None
    # مللي ثانية (13 خانة) أو ثوانٍ (10) — الحدّ يفصلهما بلا لبس
    if num > 1e11:
        num /= 1000.0
    return int(num)


def _log1p(v: Any) -> float | None:
    """لوغاريتم آمن للحجوم (مدى مقيس $10⁵→$10¹³ — الخام يطغى على التقسيم)."""
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    return math.log1p(float(v))


def _div(a: Any, b: Any) -> float | None:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or not b:
        return None
    return float(a) / float(b)


def _min_or_none(*values: Any) -> int | float | None:
    """أصغر قيمة رقمية موجودة، أو None إن غابت كلّها.

    لازمة لتجميع الرتب عبر المدد: `min()` على قائمة فيها None يرفع TypeError،
    و`min(x or inf ...)` يفبرك رقماً حيث لم نقس (FR-007).
    """
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return min(nums) if nums else None


def _ret(new: Any, old: Any) -> float | None:
    if not isinstance(new, (int, float)) or not isinstance(old, (int, float)) or old <= 0:
        return None
    return float(new) / float(old) - 1.0


def _json_list(text: Any) -> list[Any] | None:
    """نصّ JSON لمصفوفة → قائمة. التشوّه/الغياب → None (لا فبركة قائمة فارغة)."""
    if not text:
        return None
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, list) else None


# ---------------------------------------------------------------------------
# عائلة أ — الحدث نفسه (signal_events أو activity_events)
# ---------------------------------------------------------------------------
def event_features(row: dict[str, Any], t0: int) -> dict[str, Any]:
    """ميزات الحدث المُشغِّل. يعمل على الشكلين (الأماميّ والرجعيّ) بلا فبركة:
    الحقول غير الموجودة في المصدر الرجعيّ تبقى None."""
    dt = datetime.fromtimestamp(t0, UTC)
    total_volume = row.get("total_volume")
    unique_traders = row.get("unique_traders")
    rank = row.get("buyers_best_rank")
    # الصفر قيمة **مقيسة** لا غياب: 1,640 حدثاً حجمه 0.0 (خروج كامل للمركز).
    # `a or b` كان يحوّلها إلى None فيخسر النموذج معلومة صحيحة — لهذا الفحص
    # الصريح لا الـ falsy.
    size = row.get("size_usd")
    if size is None:
        size = row.get("usd_amount")
    top_ids = _json_list(row.get("top_trader_ids_json"))
    ticker = row.get("ticker")
    # عرض الحضور عبر الصدارات. مخزَّن في الحدث (يحسبه المستخرِج وقت الالتقاط
    # حيث الخرائط حاضرة)؛ الصفوف الأقدم من العائلة تبقى None بلا فبركة.
    periods_matched = row.get("top_trader_periods_matched")
    return {
        "signal_type": row.get("signal_type") or row.get("event_type"),
        "size_usd": size,
        "in_amount": row.get("in_amount"),
        "out_amount": row.get("out_amount"),
        "token_amount": row.get("token_amount"),
        "avg_cost": row.get("avg_cost"),
        # هل يشتري المشتري أعلى من متوسّط تكلفته (ثقة) أم أدنى (تعديل متوسّط)؟
        "price_to_avg_cost": _div(row.get("price_usd"), row.get("avg_cost")),
        "realized_pnl_usd": row.get("realized_pnl_usd"),
        "num_swaps": row.get("num_swaps"),
        "is_first_buy": row.get("is_first_buy"),
        "buyer_pnl_pct": row.get("buyer_pnl_pct"),
        "market_cap": row.get("market_cap"),
        "fdv": row.get("fdv"),
        "price_usd": row.get("price_usd"),
        "log_market_cap": _log1p(row.get("market_cap")),
        "log_size_usd": _log1p(size),
        "size_to_mcap": _div(size, row.get("market_cap")),
        "unique_traders": unique_traders,
        "num_trades": row.get("num_trades"),
        "minutes": row.get("minutes"),
        "price_change_pct": row.get("price_change_pct"),
        "total_volume": total_volume,
        # التركيبة: أقوى أثر مقيس (نخبة كبار مقابل حشد صغار — README §8)
        "volume_per_trader": _div(total_volume, unique_traders),
        "are_top_traders": row.get("are_top_traders"),
        "top_trader_match_count": row.get("top_trader_match_count"),
        # عدد المتصدّرين الذين أعلنتهم fomo مقابل من طابقناهم بصدارتنا:
        # النسبة تميّز «كتلة نخبة» من «كتلة اسمية».
        "top_traders_listed": len(top_ids) if top_ids is not None else None,
        "top_trader_match_ratio": _div(row.get("top_trader_match_count"),
                                       len(top_ids) if top_ids else None),
        "buyers_best_rank": rank,
        "rank_le_10": (1 if rank <= 10 else 0) if isinstance(rank, int) else None,
        "rank_le_50": (1 if rank <= 50 else 0) if isinstance(rank, int) else None,
        # صدارات المدد: كل رتبة قياس مستقلّ — الرابع في 24h ليس الرابع في
        # totalPnL. تُمرّر كما هي، والصفوف المبنيّة قبل تشغيل الجمع تبقى NULL.
        "top_trader_match_count_24h": row.get("top_trader_match_count_24h"),
        "buyers_best_rank_24h": row.get("buyers_best_rank_24h"),
        "top_trader_match_count_7d": row.get("top_trader_match_count_7d"),
        "buyers_best_rank_7d": row.get("buyers_best_rank_7d"),
        "top_trader_match_count_30d": row.get("top_trader_match_count_30d"),
        "buyers_best_rank_30d": row.get("buyers_best_rank_30d"),
        # عرض الحضور: في كم صدارة (0-4) ظهر مشترٍ. متصدّر الأربع كلّها حيوان
        # آخر عن متصدّر 24h وحدها — الأوّل سِجلّ، والثاني قد يكون ضربة حظّ.
        "top_trader_periods_matched": periods_matched,
        "top_trader_any_period": (
            (1 if periods_matched > 0 else 0)
            if isinstance(periods_matched, int) else None
        ),
        # أفضل رتبة عبر المدد كلّها: أقوى دليل «نخبة اشترت» بأوسع تغطية،
        # مع بقاء الرتب المفصّلة للنموذج كي يفصل المدد إن شاء.
        "best_rank_any_period": _min_or_none(
            rank,
            row.get("buyers_best_rank_24h"),
            row.get("buyers_best_rank_7d"),
            row.get("buyers_best_rank_30d"),
        ),
        # نصّ الرمز: مقيس في مشاريع مشابهة كإشارة جودة/انتحال
        "ticker_len": len(ticker) if isinstance(ticker, str) else None,
        "ticker_has_digit": (
            1 if isinstance(ticker, str) and any(ch.isdigit() for ch in ticker) else
            (0 if isinstance(ticker, str) else None)
        ),
        "ticker_non_ascii": (
            1 if isinstance(ticker, str) and any(ord(ch) > 127 for ch in ticker) else
            (0 if isinstance(ticker, str) else None)
        ),
        "hour_utc": dt.hour,
        "dow": dt.weekday(),
    }


# ---------------------------------------------------------------------------
# عائلة ب — ثوابت العملة + بصمة المُنشئ
# ---------------------------------------------------------------------------
def static_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    row = db._conn.execute(
        """SELECT * FROM token_static
            WHERE token_address=? AND network_id=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY recorded_at DESC LIMIT 1""",
        (token, network, t0),
    ).fetchone()
    out: dict[str, Any] = {
        "token_age_h": None, "launchpad_name": None, "migrated": None,
        "graduation_percent": None, "is_scam": None, "mintable": None,
        "freezable": None, "socials_count": None, "has_twitter": None,
        "creator_prior_tokens": None, "decimals": None, "name_len": None,
        "name_non_ascii": None,
        # شرعية خارجية — كانت في الخام منذ اليوم الأول ولا تُستخرج
        "exchanges_count": None, "listed_on_exchange": None, "has_cmc_id": None,
        "description_len": None, "has_banner": None,
    }
    if row is None:
        return out
    created = epoch_of(row["token_created_at"])
    socials = [row["twitter"], row["telegram"], row["website"], row["discord"]]
    name = row["name"]
    # عمر سالب مستحيل فيزيائياً (إشارة قبل إنشاء العملة): مقيس صفٌّ واحد بـ−177
    # ساعة — ختم fomo يخصّ إدراجاً/إعادة إدراج لا الإنشاء. القيمة غير موثوقة
    # ⇒ None لا رقم سالب يتعلّمه النموذج كأنّه معنى.
    age_h = (t0 - created) / 3600 if created else None
    if age_h is not None and age_h < 0:
        age_h = None
    out.update({
        "token_age_h": age_h,
        "launchpad_name": row["launchpad_name"],
        "migrated": row["migrated"],
        "graduation_percent": row["graduation_percent"],
        "is_scam": row["is_scam"],
        "mintable": row["mintable"],
        "freezable": row["freezable"],
        "socials_count": sum(1 for s in socials if s),
        "has_twitter": 1 if row["twitter"] else 0,
        "decimals": row["decimals"],
        # نصّ الاسم: محارف مخادعة/طول شاذّ إشارة جودة مقيسة في مشاريع مشابهة
        "name_len": len(name) if isinstance(name, str) else None,
        "name_non_ascii": (
            1 if isinstance(name, str) and any(ord(ch) > 127 for ch in name) else
            (0 if isinstance(name, str) else None)
        ),
        # شرعية خارجية: إدراج في منصّة مركزية أو معرّف CoinMarketCap لا يمنحهما
        # مطلقُ عملةٍ لنفسه بضغطة زرّ — بعكس تويتر وموقع الويب. مقيس على 516
        # عملة: exchanges_count متاح في 516 (حتى 8 منصّات)، وcmc_id في 123،
        # ووصف في 217، وبانر في 181. كلّها كانت في raw_json ولا تُقرأ.
        "exchanges_count": row["exchanges_count"],
        "listed_on_exchange": (
            1 if (row["exchanges_count"] or 0) > 0
            else (0 if row["exchanges_count"] is not None else None)
        ),
        "has_cmc_id": 1 if row["cmc_id"] else 0,
        # طول الوصف: صفر يعني مشروعاً لم يكتب سطراً عن نفسه (بذل الجهد إشارة)
        "description_len": row["description_len"],
        "has_banner": row["has_banner"],
    })
    # بصمة المُنشئ: كم عملة أخرى له **ظهرت قبل t0** (منشئ متسلسل = نمط rug).
    # القيد الزمنيّ على أوّل ظهور للعملة الأخرى، وإلّا تسرّب المستقبل.
    # المقارنة في بايثون لا SQL لأنّ `token_created_at` قد يكون epoch رقمياً أو
    # ISO حسب ما أعادته fomo — strftime يفشل صامتاً على الأوّل.
    creator = row["creator_address"]
    if creator:
        others = db._conn.execute(
            """SELECT token_created_at FROM token_static
                WHERE creator_address = ? AND token_address != ?
                  AND network_id = ?
                  AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?""",
            (creator, token, network, t0),
        ).fetchall()
        out["creator_prior_tokens"] = sum(
            1 for o in others
            if (e := epoch_of(o["token_created_at"])) is not None and e <= t0
        )
    return out


# ---------------------------------------------------------------------------
# عائلة ج — الزخم الاجتماعيّ التاريخيّ (created_at <= t0 حصراً)
# ---------------------------------------------------------------------------
def social_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    """الزخم الاجتماعيّ من مصدرين مكمّلين:

    1. `token_thesis` — أطروحات فردية بختم كتابتها ⇒ **عدّ تاريخيّ** لأيّ لحظة،
       لكنّه **عيّنة لا حقيقة**: الترقيم توقّف عند ~400/عملة، فـ30% من الصفوف
       مشبَّعة عند السقف (`thesis_counted_capped` يعلّمها).
    2. `token_social` — لقطة كل ~30 دقيقة تحمل `thesis_total` **الحقيقيّ** من
       المغلّف (شوهد 29,595 مقابل 400 مخزَّنة) و`thesis_authors` و
       **`holder_authors`** (كتّاب يملكون كمية موجبة فعلاً = جلد في اللعبة؛
       مصدره `authorTrade` لا `equity` — انظر تعليق FEATURE_VERSION 6).

    اللقطة تُقرأ **عند/قبل t0 حصراً**، فليست تسرّباً: المنع كان لاستعمال قيمة
    اليوم لحدث الماضي (README §9)، لا لقيمة قِيست قبل القرار.

    ⚠️ `num_likes` من `token_thesis` ممنوعة أبداً (قيمتها وقت السحب لا الكتابة).
    """
    q = """SELECT COUNT(*) n, COUNT(DISTINCT user_id) authors, MAX(e) last_e, MIN(e) first_e
             FROM (SELECT user_id, CAST(strftime('%s', created_at) AS INTEGER) e
                     FROM token_thesis
                    WHERE token_address = ? AND network_id = ?)
             WHERE e <= ?"""
    r = db._conn.execute(q, (token, network, t0)).fetchone()
    n_all, authors, last_e, first_e = r["n"], r["authors"], r["last_e"], r["first_e"]

    def _count(since: int) -> int:
        return db._conn.execute(
            """SELECT COUNT(*) FROM token_thesis
                WHERE token_address = ?
                  AND network_id = ?
                  AND CAST(strftime('%s', created_at) AS INTEGER) <= ?
                  AND CAST(strftime('%s', created_at) AS INTEGER) > ?""",
            (token, network, t0, since),
        ).fetchone()[0]

    n_1h = _count(t0 - 3600) if n_all else 0
    n_24h = _count(t0 - 86400) if n_all else 0
    out: dict[str, Any] = {
        "thesis_counted": n_all,
        "thesis_counted_capped": 1 if n_all >= _THESIS_PAGE_CAP else 0,
        "thesis_authors_before": authors,
        "thesis_1h": n_1h,
        "thesis_24h": n_24h,
        # تسارع النقاش: نصيب الساعة الأخيرة من اليوم (قريب من 1 = اشتعال الآن)
        "thesis_accel": _div(n_1h, n_24h),
        "hours_since_last_thesis": (t0 - last_e) / 3600 if last_e else None,
        # «اكتُشفت متأخّرة»: عمر النقاش قبل الإشارة (PLAN §2.3-و-ج)
        "thesis_history_days": (t0 - first_e) / 86400 if first_e else None,
        # من token_social (تبقى None حين لا لقطة قبل t0)
        "social_thesis_total": None, "social_thesis_authors": None,
        "social_holder_authors": None, "social_holder_ratio": None,
        "social_replies": None, "social_snapshot_age_min": None,
        "social_total_delta_1h": None, "social_total_growth_1h": None,
        "social_authors_delta_1h": None,
    }

    snap = db._conn.execute(
        """SELECT thesis_total, thesis_authors, holder_authors, thesis_replies,
                  CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM token_social
            WHERE token_address = ? AND network_id = ?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1""",
        (token, network, t0),
    ).fetchone()
    if snap is None:
        return out
    out.update({
        "social_thesis_total": snap["thesis_total"],
        "social_thesis_authors": snap["thesis_authors"],
        "social_holder_authors": snap["holder_authors"],
        # نصيب الكتّاب المالكين: حماس يملك مقابل حماس يتكلّم فقط
        "social_holder_ratio": _div(snap["holder_authors"], snap["thesis_authors"]),
        "social_replies": snap["thesis_replies"],
        "social_snapshot_age_min": (t0 - snap["e"]) / 60,
    })
    # فرق الزخم: لقطة أقدم بساعة على الأقلّ ⇒ تسارع مقيس لا مستنبط
    prev = db._conn.execute(
        """SELECT thesis_total, thesis_authors,
                  CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM token_social
            WHERE token_address = ? AND network_id = ?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1""",
        (token, network, snap["e"] - 3600),
    ).fetchone()
    if prev is not None:
        out["social_total_delta_1h"] = (
            snap["thesis_total"] - prev["thesis_total"]
            if snap["thesis_total"] is not None and prev["thesis_total"] is not None
            else None
        )
        out["social_total_growth_1h"] = _ret(snap["thesis_total"], prev["thesis_total"])
        out["social_authors_delta_1h"] = (
            snap["thesis_authors"] - prev["thesis_authors"]
            if snap["thesis_authors"] is not None and prev["thesis_authors"] is not None
            else None
        )
    return out


# ---------------------------------------------------------------------------
# عائلة د — مسار السعر **قبل** الإشارة (الشموع السليمة وحدها)
# ---------------------------------------------------------------------------
def price_history_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    bars = db._conn.execute(
        """SELECT ts, h, l, c, v, h_suspect FROM token_bars
            WHERE token_address=? AND network_id=? AND resolution=?
              AND ts + CAST(resolution AS INTEGER) * 60 <= ?
              AND c_suspect = 0
            ORDER BY ts""",
        (token, network, config.BARS_RESOLUTION, t0),
    ).fetchall()
    out: dict[str, Any] = {
        "ret_1h_before": None, "ret_4h_before": None, "ret_24h_before": None,
        "ret_7d_before": None, "vol_24h_before": None, "flat_ratio_24h": None,
        "dist_from_ath": None, "ath_history_complete": 0,
        "ath_history_days": None, "bars_history_h": None, "bars_count_24h": None,
        "bar_vol_1h": None, "bar_vol_24h": None, "vol_surge_1h": None,
        "up_candle_ratio_24h": None,
    }
    if not bars:
        return out
    last_c = bars[-1]["c"]
    first_ts = bars[0]["ts"]
    out["bars_history_h"] = (t0 - first_ts) / 3600

    def _close_at_or_before(target: int) -> float | None:
        prev = None
        for b in bars:
            if b["ts"] <= target:
                prev = b["c"]
            else:
                break
        return prev

    for label, secs in (("1h", 3600), ("4h", 14400), ("24h", 86400), ("7d", 604800)):
        ref = _close_at_or_before(t0 - secs)
        out[f"ret_{label}_before"] = _ret(last_c, ref)

    window = [b for b in bars if b["ts"] > t0 - 86400]
    out["bars_count_24h"] = len(window)
    if len(window) >= 3:
        rets = [
            _ret(window[i]["c"], window[i - 1]["c"]) for i in range(1, len(window))
        ]
        rets = [r for r in rets if r is not None]
        if len(rets) >= 2:
            out["vol_24h_before"] = st.pstdev(rets)
            # «الهدوء قبل الانفجار»: نصيب الشموع شبه الساكنة (PLAN §2.3-ز)
            out["flat_ratio_24h"] = sum(1 for r in rets if abs(r) < 0.005) / len(rets)
            out["up_candle_ratio_24h"] = sum(1 for r in rets if r > 0) / len(rets)
    # حجم التداول قبل الإشارة: انفجار الحجم يسبق السعر عادةً. `v` مغطّاة 100%
    # وكانت مهملة كليّاً.
    vols_24h = [b["v"] for b in window if isinstance(b["v"], (int, float))]
    hour = [b for b in bars if b["ts"] > t0 - 3600]
    vols_1h = [b["v"] for b in hour if isinstance(b["v"], (int, float))]
    if vols_24h:
        out["bar_vol_24h"] = sum(vols_24h)
    if vols_1h:
        out["bar_vol_1h"] = sum(vols_1h)
    # نصيب الساعة الأخيرة من حجم اليوم: 1/24 = عاديّ، ←1 = اشتعال الآن
    out["vol_surge_1h"] = _div(out["bar_vol_1h"], out["bar_vol_24h"])
    # القمّة التاريخية: **الذيول المشوّهة تُستبعد** — شمعة إغلاقها سليم وقمّتها
    # فاسدة (39 شمعة مقيسة) كانت تجعل dist_from_ath = −0.99999997 على 49 صفّاً.
    highs = [
        b["h"] for b in bars
        if isinstance(b["h"], (int, float)) and not b["h_suspect"]
    ]
    # شموع 1D تُسحب رجوعاً حتى نفاد تاريخ المنبع. لا نستعمل الصفحة الجزئية:
    # غياب القسم الأقدم قد يخفض ATH بصمت ويحوّل الميزة إلى «قمّة محلية».
    daily = db._conn.execute(
        """SELECT MIN(b.ts) AS first_ts, MAX(b.h) AS ath
             FROM token_bars b
             JOIN historical_bars_state s
               ON s.token_address=b.token_address
              AND s.network_id=b.network_id
              AND s.resolution=b.resolution
              AND s.last_status='ok'
            WHERE b.token_address=? AND b.network_id=? AND b.resolution='1D'
              AND b.ts + 86400 <= ? AND b.h_suspect=0""",
        (token, network, t0),
    ).fetchone()
    if daily and isinstance(daily["ath"], (int, float)):
        highs.append(daily["ath"])
        out["ath_history_complete"] = 1
        if daily["first_ts"] is not None:
            out["ath_history_days"] = (t0 - daily["first_ts"]) / 86400
    if highs and last_c:
        ath = max(highs)
        out["dist_from_ath"] = _ret(last_c, ath)  # سالبة = دون القمّة
    return out


# ---------------------------------------------------------------------------
# عائلة هـ — لقطة السوق الأخيرة قبل t0 (تغطية جزئية ⇒ NULLs مقصودة)
# ---------------------------------------------------------------------------
def market_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    """أحدث لقطة سوق **عند/قبل t0**. `tick_age_min` يُعلم النموذج بطزاجتها.

    نستعمل النوافذ **الكثيفة** (change/volume/txn بتغطية 100% على 588 ألف لقطة)
    لا الأعمدة النادرة وحدها: عدّادات الشراء/البيع مغطّاة 24% فقط، فالاعتماد
    عليها كان يهدر أغنى ما في الجدول.
    """
    empty = {
        "liquidity": None, "holders": None, "top10_holders_pct": None,
        "volume_24h": None, "buy_count_24h": None, "sell_count_24h": None,
        "buy_sell_ratio_24h": None, "unique_buys_24h": None,
        "unique_sells_24h": None, "tick_age_min": None,
        "tick_change_1h": None, "tick_change_4h": None, "tick_change_24h": None,
        "tick_volume_1h": None, "tick_volume_4h": None,
        "tick_txn_1h": None, "tick_txn_24h": None,
        "volume_to_liquidity": None, "liquidity_to_mcap": None,
        "float_ratio": None,
    }
    r = db._conn.execute(
        """SELECT *, CAST(strftime('%s', recorded_at) AS INTEGER) e FROM market_ticks
            WHERE token_address=? AND network_id=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1""",
        (token, network, t0),
    ).fetchone()
    if r is None:
        return empty
    return {
        "liquidity": r["liquidity"],
        "holders": r["holders"],
        "top10_holders_pct": r["top10_holders_pct"],
        "volume_24h": r["volume_24h"],
        "buy_count_24h": r["buy_count_24h"],
        "sell_count_24h": r["sell_count_24h"],
        "buy_sell_ratio_24h": _div(r["buy_count_24h"], r["sell_count_24h"]),
        "unique_buys_24h": r["unique_buys_24h"],
        "unique_sells_24h": r["unique_sells_24h"],
        "tick_age_min": (t0 - r["e"]) / 60,
        # النوافذ القصيرة: زخم أقرب إلى لحظة القرار من نافذة 24 ساعة
        "tick_change_1h": r["change_1h"],
        "tick_change_4h": r["change_4h"],
        "tick_change_24h": r["change_24h"],
        "tick_volume_1h": r["volume_1h"],
        "tick_volume_4h": r["volume_4h"],
        "tick_txn_1h": r["txn_count_1h"],
        "tick_txn_24h": r["txn_count_24h"],
        # دوران الحوض: حجم كبير على سيولة ضحلة = ضخّ سريع وانزلاق قاتل
        "volume_to_liquidity": _div(r["volume_24h"], r["liquidity"]),
        "liquidity_to_mcap": _div(r["liquidity"], r["market_cap"]),
        # نسبة التعويم: معروض متداول ÷ الكلّي — تعويم ضئيل = خطر تصريف
        "float_ratio": _div(r["circulating_supply"], r["total_supply"]),
    }


# ---------------------------------------------------------------------------
# عائلة هـ٢ — الملكية: تركيز السلسلة + تموضع حشد المنصّة
# ---------------------------------------------------------------------------
def holders_features(
    db: RecorderDB, token: str, network: str, t0: int
) -> dict[str, Any]:
    """أحدث قياس حيازة **عند/قبل t0** من مصدرين يقيسان شيئين مختلفين.

    `market_ticks.top10_holders_pct` أعلاه ميّت (صفر من 1,430,475): قوائم
    trending/verified لا تحمل المفتاح إطلاقاً. وفحص السلسلة يغطّي Solana وحدها
    ويصمت عن EVM كلّه. دورة الحائزين تُصلح الاثنين عبر:

    - `token_details` ⇒ **تركيز السلسلة**: أكبر 10 % من المعروض + عدد الحائزين
      الكلّي، على EVM وSolana معاً (مقيس حيّاً: 83.2% لعملة و21.9% لأخرى).
      أقوى مؤشّر rug منفرد.
    - `hodlers/top` ⇒ **تموضع الحشد**: مستخدمو fomo الحائزون فعلاً (274 من 937،
      و118 من 14,371) بتكلفة كل مركز وربحه غير المحقّق ومدّة حمله. لا نسب
      معروض هنا إطلاقاً، فلا نشتقّ تركيزاً منه ولا نخمّنه.

    `platform_penetration` هو الاشتقاق الذي لا يعطيه أي مصدر منفرداً: نصيب
    المنصّة من حائزي السلسلة. عالٍ = حركة يقودها حشد fomo (قابلة للانعكاس حين
    يخرج)، منخفض = طلب خارجيّ أوسع.

    `platform_underwater_ratio` عرضٌ زائد محتمل: حاملون خاسرون يبيعون عند أوّل
    تعافٍ. مقيس حيّاً 49 من 49 خاسراً في عملة، مقابل 6 من 50 في أخرى.

    كل الصفوف قبل 2026-08-09 ستكون None هنا (الدورة جديدة) — وهذا مقصود:
    NULL يعني «لم نقس» لا «صفر» (FR-007).
    """
    out: dict[str, Any] = {
        "chain_top10_pct": None, "chain_holder_count": None,
        "holders_age_min": None, "platform_holders": None,
        "platform_penetration": None, "platform_underwater_ratio": None,
        "platform_value_usd": None, "platform_median_hold_h": None,
        "platform_dev_holding": None,
    }
    q = """SELECT *, CAST(strftime('%s', recorded_at) AS INTEGER) e
             FROM token_holders
            WHERE token_address=? AND network_id=? AND source=?
              AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
            ORDER BY e DESC LIMIT 1"""
    det = db._conn.execute(q, (token, network, "token_details", t0)).fetchone()
    plat = db._conn.execute(q, (token, network, "hodlers_top", t0)).fetchone()
    if det is None and plat is None:
        return out

    if det is not None:
        out["chain_top10_pct"] = det["top10_pct"]
        out["chain_holder_count"] = det["holder_count"]
    if plat is not None:
        out["platform_holders"] = plat["platform_holders"]
        out["platform_value_usd"] = plat["platform_value_usd"]
        out["platform_dev_holding"] = plat["platform_dev_holding"]
        out["platform_underwater_ratio"] = _div(
            plat["platform_underwater"], plat["platform_holders_listed"]
        )
        secs = plat["platform_median_hold_seconds"]
        out["platform_median_hold_h"] = secs / 3600 if secs is not None else None
        if det is not None:
            out["platform_penetration"] = _div(
                plat["platform_holders"], det["holder_count"]
            )
    # طزاجة القياس: أحدث ختم من المصدرين (كلٌّ يُجدَّد بدورته)
    stamps = [r["e"] for r in (det, plat) if r is not None]
    out["holders_age_min"] = (t0 - max(stamps)) / 60 if stamps else None
    return out


# ---------------------------------------------------------------------------
# عائلة و — النظام السوقيّ (شموع الماكرو الساعية)
# ---------------------------------------------------------------------------
def macro_features(db: RecorderDB, t0: int) -> dict[str, Any]:
    out: dict[str, Any] = {"sol_ret_4h": None, "sol_ret_24h": None, "eth_ret_24h": None}
    by_label = {label: (addr, net) for label, addr, net in config.MACRO_BARS}
    for key, label, secs in (("sol_ret_4h", "SOL", 14400),
                             ("sol_ret_24h", "SOL", 86400),
                             ("eth_ret_24h", "WETH", 86400)):
        pair = by_label.get(label)
        if not pair:
            continue
        addr, net = pair
        rows = db._conn.execute(
            """SELECT ts, c FROM token_bars
                WHERE token_address=? AND network_id=? AND resolution=?
                  AND ts + CAST(resolution AS INTEGER) * 60 <= ?
                  AND c_suspect = 0 ORDER BY ts""",
            (addr, net, config.MACRO_BARS_RESOLUTION, t0),
        ).fetchall()
        if not rows:
            continue
        last = rows[-1]["c"]
        ref = None
        for b in rows:
            if b["ts"] <= t0 - secs:
                ref = b["c"]
            else:
                break
        out[key] = _ret(last, ref)
    return out


# ---------------------------------------------------------------------------
# عائلة ز — كثافة الإشارات (سياق «هل السوق مشتعل؟»)
# ---------------------------------------------------------------------------
def density_features(
    db: RecorderDB, token: str, network: str, t0: int, exclude_key: str | None
) -> dict[str, Any]:
    """كثافة الأحداث قبل t0 — **من المصدرين معاً**.

    الحساب من `signal_events` وحده كان يعطي أصفاراً بنيويّة للصفوف الرجعية
    (مقيس: 88% من صفوف 2025 صفر مقابل 4% للأمامية) — فيصير العمود دالّاً على
    «من أين جاء الصفّ» لا على نشاط العملة، والنموذج يتعلّم الأثر الزائف.
    ضمّ `activity_events` يوحّد المعنى على كل الصفوف.
    """
    prior = db._conn.execute(
        """SELECT COUNT(*) n, MAX(e) last_e FROM (
               SELECT CAST(strftime('%s', ts) AS INTEGER) e, id FROM signal_events
                WHERE token_address = ? AND network_id = ? AND ts IS NOT NULL
               UNION
               SELECT CAST(strftime('%s', ts) AS INTEGER) e, id FROM activity_events
                WHERE token_address = ? AND network_id = ? AND ts IS NOT NULL)
            WHERE e < ? AND (? IS NULL OR id != ?)""",
        (token, network, token, network, t0, exclude_key, exclude_key),
    ).fetchone()
    glob = db._conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT CAST(strftime('%s', ts) AS INTEGER) e, id FROM signal_events
                WHERE ts IS NOT NULL
               UNION
               SELECT CAST(strftime('%s', ts) AS INTEGER) e, id FROM activity_events
                WHERE ts IS NOT NULL)
            WHERE e < ? AND e >= ?""",
        (t0, t0 - 3600),
    ).fetchone()[0]
    return {
        "prior_signals_token": prior["n"],
        "minutes_since_prior_signal": (
            (t0 - prior["last_e"]) / 60 if prior["last_e"] else None
        ),
        "global_signals_1h": glob,
    }


# ---------------------------------------------------------------------------
# الصفّ الكامل
# ---------------------------------------------------------------------------
FEATURE_COLUMNS: tuple[str, ...] = (
    # أ — الحدث
    "signal_type", "size_usd", "in_amount", "out_amount", "token_amount",
    "avg_cost", "price_to_avg_cost", "realized_pnl_usd", "num_swaps",
    "is_first_buy", "buyer_pnl_pct", "market_cap", "fdv", "price_usd",
    "log_market_cap", "log_size_usd", "size_to_mcap", "unique_traders",
    "num_trades", "minutes", "price_change_pct", "total_volume",
    "volume_per_trader", "are_top_traders", "top_trader_match_count",
    "top_traders_listed", "top_trader_match_ratio",
    "buyers_best_rank", "rank_le_10", "rank_le_50",
    "top_trader_match_count_24h", "buyers_best_rank_24h",
    "top_trader_match_count_7d", "buyers_best_rank_7d",
    "top_trader_match_count_30d", "buyers_best_rank_30d",
    "top_trader_periods_matched", "top_trader_any_period", "best_rank_any_period",
    "ticker_len", "ticker_has_digit", "ticker_non_ascii", "hour_utc", "dow",
    # ب — الثوابت والمُنشئ
    "token_age_h", "launchpad_name", "migrated", "graduation_percent", "is_scam",
    "mintable", "freezable", "socials_count", "has_twitter", "creator_prior_tokens",
    "name_len", "name_non_ascii", "decimals",
    "exchanges_count", "listed_on_exchange", "has_cmc_id", "description_len",
    "has_banner",
    # ج — الاجتماعيّ (عدّ تاريخيّ + لقطة حقيقية قبل t0)
    "thesis_counted", "thesis_counted_capped", "thesis_authors_before",
    "thesis_1h", "thesis_24h", "thesis_accel", "hours_since_last_thesis",
    "thesis_history_days",
    "social_thesis_total", "social_thesis_authors", "social_holder_authors",
    "social_holder_ratio", "social_replies", "social_snapshot_age_min",
    "social_total_delta_1h", "social_total_growth_1h", "social_authors_delta_1h",
    # د — مسار السعر والحجم قبل الإشارة
    "ret_1h_before", "ret_4h_before", "ret_24h_before", "ret_7d_before",
    "vol_24h_before", "flat_ratio_24h", "up_candle_ratio_24h", "dist_from_ath",
    "ath_history_complete", "ath_history_days",
    "bars_history_h", "bars_count_24h", "bar_vol_1h", "bar_vol_24h",
    "vol_surge_1h",
    # هـ — لقطة السوق
    "liquidity", "holders", "top10_holders_pct", "volume_24h", "buy_count_24h",
    "sell_count_24h", "buy_sell_ratio_24h", "unique_buys_24h", "unique_sells_24h",
    "tick_age_min", "tick_change_1h", "tick_change_4h", "tick_change_24h",
    "tick_volume_1h", "tick_volume_4h", "tick_txn_1h", "tick_txn_24h",
    "volume_to_liquidity", "liquidity_to_mcap", "float_ratio",
    # هـ٢ — الملكية: تركيز السلسلة (يُصلح top10_holders_pct الميّت) وتموضع الحشد
    "chain_top10_pct", "chain_holder_count", "holders_age_min",
    "platform_holders", "platform_penetration", "platform_underwater_ratio",
    "platform_value_usd", "platform_median_hold_h", "platform_dev_holding",
    # و — الماكرو
    "sol_ret_4h", "sol_ret_24h", "eth_ret_24h",
    # ز — الكثافة
    "prior_signals_token", "minutes_since_prior_signal", "global_signals_1h",
)

META_COLUMNS: tuple[str, ...] = (
    "kind", "key", "token_address", "network_id", "entry_ts", "asset_class",
    "split", "is_independent", "is_live", "status", "suspect_bars",
    "feature_version", "built_at",
)

LABEL_COLUMNS: tuple[str, ...] = (
    "final_return_48h", "max_gain_1h", "max_gain_4h", "max_gain_24h",
    "max_gain_48h", "max_drawdown_48h", "time_to_peak_h", "is_rug",
)

ROW_COLUMNS: tuple[str, ...] = META_COLUMNS + FEATURE_COLUMNS + LABEL_COLUMNS


def build_features(
    db: RecorderDB, event: dict[str, Any], token: str, network: str, t0: int,
    exclude_key: str | None = None,
) -> dict[str, Any]:
    """كل الميزات لقرار واحد عند t0. **قطعة واحدة للتدريب والحيّ.**"""
    out: dict[str, Any] = {}
    out.update(event_features(event, t0))
    out.update(static_features(db, token, network, t0))
    out.update(social_features(db, token, network, t0))
    out.update(price_history_features(db, token, network, t0))
    out.update(market_features(db, token, network, t0))
    out.update(holders_features(db, token, network, t0))
    out.update(macro_features(db, t0))
    out.update(density_features(db, token, network, t0, exclude_key))
    return {k: out.get(k) for k in FEATURE_COLUMNS}


def source_event(db: RecorderDB, kind: str, key: str) -> dict[str, Any] | None:
    """الحدث المُشغِّل لصفّ نتيجة: إشارة أمامية أو حدث رجعيّ أو دخول مراقبة."""
    if kind == "signal":
        r = db._conn.execute("SELECT * FROM signal_events WHERE id=?", (key,)).fetchone()
    elif kind == "activity":
        r = db._conn.execute("SELECT * FROM activity_events WHERE id=?", (key,)).fetchone()
    else:  # watch/control — لا حدث مُشغِّل (الضابطة عشوائية بالتصميم)
        return {}
    return dict(r) if r else None


def build_training_row(db: RecorderDB, outcome: dict[str, Any]) -> dict[str, Any] | None:
    """صفّ نتيجة موسوم → صفّ تدريب كامل (ميزات + ليبل + وسم)."""
    kind, key = outcome["kind"], outcome["key"]
    event = source_event(db, kind, key)
    if event is None:
        return None  # حدث مفقود: لا نفبرك صفّاً
    token = outcome["token_address"]
    network = str(outcome["network_id"] or "")
    t0 = int(outcome["entry_ts"])
    price = event.get("price_usd")
    market_cap = event.get("market_cap")
    symbol = event.get("ticker") or event.get("symbol")
    asset_class = None
    if symbol or price is not None or market_cap is not None:
        asset_class = classify_asset(symbol, price, price, market_cap)[0]
    row: dict[str, Any] = {
        "kind": kind, "key": key, "token_address": token, "network_id": network,
        "entry_ts": t0, "asset_class": asset_class,
        "split": outcome["split"], "is_independent": outcome["is_independent"],
        # `activity` مجمّعة بأثر رجعي حتى إن كان ختم الحدث حديثاً؛ الحي يعني أن
        # القرار جاء من signal_events الأمامي، لا مجرد أن entry_ts بعد الحد.
        "is_live": 1 if kind == "signal" and t0 >= config.LIVE_START_TS else 0,
        "status": outcome["status"], "suspect_bars": outcome["suspect_bars"],
        "feature_version": FEATURE_VERSION,
        "built_at": datetime.now(UTC).isoformat(),
    }
    row.update(build_features(
        db, event, token, network, t0, exclude_key=key if kind == "signal" else None
    ))
    for c in LABEL_COLUMNS:
        row[c] = outcome[c]
    return row


def json_safe(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, default=str)
