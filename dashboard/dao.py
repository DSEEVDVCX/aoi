"""طبقة قراءة (read-only) لِـ recorder.db.

يفتح القاعدة عبر URI بـ mode=ro فلا يمكنه الكتابة إطلاقاً — لا يعطّل كتابات
المسجّل (WAL). كل استعلام دالة خالصة تأخذ اتصالاً، قابلة للاختبار على قاعدة مؤقّتة.

ملاحظة: نفتح اتصالاً جديداً لكل طلب (رخيص لـ SQLite المحلّي) ونغلقه — أبسط من
مشاركة اتصال عبر خيوط FastAPI.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any


def connect_ro(db_path: str) -> sqlite3.Connection:
    """اتصال للقراءة فقط. mode=ro يمنع أي كتابة على مستوى SQLite نفسه."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """اللوحة تقرأ قاعدة يكتبها المسجّل؛ قد تسبق نسخةُ اللوحة ترحيلَ المسجّل
    (أو العكس). الفحص يجعل العمود الجديد اختيارياً بدل أن يُسقط اللوحة."""
    if not _table_exists(conn, table):
        return False
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


# --- meta ---
def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    if not _table_exists(conn, "meta"):
        return None
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def all_meta(conn: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(conn, "meta"):
        return {}
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}


# --- counts ---
_TABLES = ("signal_events", "market_ticks", "token_static", "watchlist", "snapshots",
           "outcomes", "token_bars", "activity_events")


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in _TABLES:
        if _table_exists(conn, t):
            out[t] = conn.execute("SELECT COUNT(*) AS n FROM " + t).fetchone()["n"]
        else:
            out[t] = 0
    return out


def active_watch_count(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "watchlist"):
        return 0
    return conn.execute("SELECT COUNT(*) AS n FROM watchlist WHERE active=1").fetchone()["n"]


def control_maturity(
    conn: sqlite3.Connection,
    preliminary_target: int,
    decision_target: int,
    design_version: int = 3,
) -> dict[str, Any]:
    """تقدّم الضابطة المؤهلة؛ `ok` فقط هو نافذة 48س مكتملة قابلة للمقارنة."""
    row = None
    if _table_exists(conn, "outcomes"):
        required = ("design_version", "analysis_eligible", "is_control", "entry_ts")
        if all(_has_column(conn, "outcomes", column) for column in required):
            row = conn.execute(
                """SELECT COUNT(*) AS completed, MIN(entry_ts) AS first_entry_ts,
                          MAX(entry_ts) AS last_entry_ts
                     FROM outcomes
                    WHERE kind='watch' AND is_control=1 AND status='ok'
                      AND design_version>=? AND analysis_eligible=1""",
                (design_version,),
            ).fetchone()
    completed = int(row["completed"]) if row else 0
    preliminary_target = max(1, int(preliminary_target))
    decision_target = max(preliminary_target, int(decision_target))
    return {
        "completed": completed,
        "preliminary_target": preliminary_target,
        "decision_target": decision_target,
        "preliminary_remaining": max(0, preliminary_target - completed),
        "decision_remaining": max(0, decision_target - completed),
        "preliminary_pct": round(min(100, completed / preliminary_target * 100), 1),
        "decision_pct": round(min(100, completed / decision_target * 100), 1),
        "preliminary_ready": completed >= preliminary_target,
        "decision_ready": completed >= decision_target,
        "design_version": design_version,
        "first_entry_ts": row["first_entry_ts"] if row else None,
        "last_entry_ts": row["last_entry_ts"] if row else None,
    }


# --- recorder status ---
def recorder_status(
    conn: sqlite3.Connection,
    alive_window_seconds: int,
    labeler_window_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """حالة المسجّل: حيّ؟ عدد الدورات، آخر دورة، إحصاؤها، الأخطاء.

    `labeler_window_seconds` إلزاميّ بلا افتراضي: القيمة تعيش في config وحده،
    وافتراضيّ مكرّر هنا ينجرف عنها بصمت عند أي ضبط لاحق.
    """
    now = now or datetime.now(UTC)
    meta = all_meta(conn)
    last_cycle_at = meta.get("last_cycle_at")
    last_dt = _parse_iso(last_cycle_at)
    seconds_since = None
    alive = False
    if last_dt is not None:
        seconds_since = (now - last_dt).total_seconds()
        alive = 0 <= seconds_since <= alive_window_seconds

    def _int(v: str | None) -> int:
        return int(v) if v and v.lstrip("-").isdigit() else 0

    # طزاجة الـ feed المصدر — منفصلة تماماً عن حياة المسجّل: قد يعمل المسجّل
    # بلا خطأ بينما feed فomo متجمّد ساعات، فيبدو الأرشيف "سوقاً هادئاً" وهو
    # في الحقيقة انقطاع مصدر. شوهد متجمّداً 3 ساعات.
    feed_dt = _parse_iso(meta.get("last_feed_event_at"))
    feed_age = (now - feed_dt).total_seconds() if feed_dt else None

    # حياة الموسِّم (FomoLabeler): يكتب labeler_last_run_at كل دورة (15 دقيقة)
    # حتى حين لا يوسم شيئاً. موته صامت تماماً — لا أخطاء ولا انهيار دورات —
    # بينما تتوقّف النتائج عن التراكم، ولا ينكشف ذلك إلّا عند بوّابة النضج.
    labeler_dt = _parse_iso(meta.get("labeler_last_run_at"))
    labeler_age = (now - labeler_dt).total_seconds() if labeler_dt else None

    return {
        "alive": alive,
        "seconds_since_last_cycle": seconds_since,
        "last_feed_event_at": meta.get("last_feed_event_at"),
        "feed_age_seconds": feed_age,
        # متجمّد = آخر حدث أقدم من ضعف نافذة الحياة بكثير (15 دقيقة)
        "feed_stale": bool(feed_age is not None and feed_age > 900),
        "last_cycle_at": last_cycle_at,
        "cycles_total": _int(meta.get("cycles_total")),
        "errors_total": _int(meta.get("errors_total")),
        "cycle_crashes": _int(meta.get("cycle_crashes")),
        "last_cycle_stats": meta.get("last_cycle_stats"),
        "started_at": meta.get("started_at"),
        "schema_version": meta.get("schema_version"),
        "active_watch_count": active_watch_count(conn),
        # الموسِّم: None = لم يعمل قطّ — يُعامَل كمتوقّف (stale) في العرض.
        "labeler_last_run_at": meta.get("labeler_last_run_at"),
        "labeler_age_seconds": labeler_age,
        "labeler_stale": bool(labeler_age is None or labeler_age > labeler_window_seconds),
        "labeler_last_stats": meta.get("labeler_last_stats"),
    }


def recorder_errors(conn: sqlite3.Connection, sources: tuple[str, ...]) -> list[dict[str, Any]]:
    """آخر خطأ لكل مصدر من meta (last_error_<src>). غياب = لا خطأ لذلك المصدر.

    ملاحظة: `last_error_<src>` قيمة meta ثابتة — تُكتب عند كل فشل ولا تُمسح عند
    النجاح، فتبقى تعرض آخر خطأ حتى لو تعافى المصدر. لذلك نُعلّم الخطأ بأنّه
    **قديم (stale)** إن كان ختمه الزمني أقدم من `started_at` (أي من عملية مسجّل
    ماتت) أو أقدم من آخر دورة ناجحة — فلا يُعرض خطأ متعافٍ كأنّه حالي.
    """
    meta = all_meta(conn)
    started_dt = _parse_iso(meta.get("started_at"))
    # آخر دورة نجحت كلياً (errors==0) تُبطل كل الأخطاء الأقدم منها.
    last_ok_dt = _parse_iso(meta.get("last_ok_cycle_at"))
    boundary = max((d for d in (started_dt, last_ok_dt) if d is not None), default=None)
    out = []
    for src in sources:
        val = meta.get(f"last_error_{src}")
        # الختم في القيمة بصيغة "<iso>: <msg>" — نفصله على أول ": ".
        err_dt = _parse_iso(val.split(": ", 1)[0]) if val else None
        stale = bool(val) and boundary is not None and err_dt is not None and err_dt < boundary
        out.append({"source": src, "last_error": val, "stale": stale})
    return out


# --- signals ---
def recent_signals(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    if not _table_exists(conn, "signal_events"):
        return []
    limit = max(1, min(limit, 500))
    rows = conn.execute(
        """SELECT id, token_address, network_id, ts, recorded_at, signal_type, ticker,
                  price_usd, fdv, market_cap, num_trades, are_top_traders,
                  top_trader_match_count, buyers_best_rank, buyer_handle,
                  num_swaps, is_first_buy, buyer_pnl_pct
           FROM signal_events
           ORDER BY recorded_at DESC, rowid DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- watchlist ---
def active_watchlist(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "watchlist"):
        return []
    # `watchlist.first_seen_at` اسمه يكذب: `upsert_watch` يكتب فوقه عند كل
    # إعادة قبول بعد انتهاء النافذة (db.py:781)، فمعناه الحقيقي «بداية الدورة
    # الحالية» لا «أوّل التقاط». عرضه وحده جعل عملة مُلتقطة منذ 7 أيام تبدو
    # عمرها 22 ساعة. السجلّ غير القابل للكتابة فوقه هو `watch_windows`، فمنه
    # نأخذ أوّل التقاط ونعرض الاثنين معاً: العمر الحقيقي وعمر الدورة.
    first_ever = (
        "(SELECT MIN(ww.first_seen_at) FROM watch_windows ww "
        "  WHERE ww.token_address = w.token_address"
        "    AND ww.network_id = w.network_id) AS first_ever_at"
        if _table_exists(conn, "watch_windows")
        else "w.first_seen_at AS first_ever_at"
    )
    rows = conn.execute(
        f"""SELECT w.token_address, w.network_id, w.source, w.first_seen_at, w.watch_until,
                   {first_ever},
                   (SELECT COUNT(*) FROM market_ticks m
                      WHERE m.token_address = w.token_address) AS tick_count
              FROM watchlist w
             WHERE w.active = 1
             ORDER BY w.first_seen_at DESC""",
    ).fetchall()
    return [dict(r) for r in rows]


# --- OHLCV bars ---
def bars_coverage(conn: sqlite3.Connection, live_start_ts: int) -> dict[str, Any]:
    """تقدّم التقاط الشموع: كم عملة مراقَبة لها سلسلة سعرية فعلاً.

    هذا المقياس الحاسم للتوسيم: العملة بلا شموع لا يمكن حساب نتيجتها، فتُهدر
    عيّنتها. قبل جدول token_bars كان ربع المراقَبات بلا أي سعر إطلاقاً.

    `live_start_ts` إلزاميّ بلا افتراضي: الحدّ يعيش في config وحده، وافتراضيّ
    مكرّر هنا ينجرف عنه بصمت. عدّ الشموع يُقصر على الحِقبة الحيّة (`ts >= live`):
    token_bars يحمل تاريخ سعر رجعيّاً سابقاً للإشارة (٤٦٦ ألف شمعة رجعيّة)، وهو
    بيانات ليست من جمع البوت اللحظيّ فلا تُعرض كي لا تختلط ببيانات البوت.
    """
    if not _table_exists(conn, "token_bars") or not _table_exists(conn, "watchlist"):
        return {"active": 0, "with_bars": 0, "pending": 0, "no_data": 0,
                "candles": 0, "coverage_pct": None}

    active = active_watch_count(conn)
    with_bars = conn.execute(
        """SELECT COUNT(*) AS n FROM watchlist w WHERE w.active = 1
             AND EXISTS (SELECT 1 FROM token_bars b
                          WHERE b.token_address = w.token_address
                            AND b.network_id = w.network_id)"""
    ).fetchone()["n"]
    # الحِقبة الحيّة فقط — الشموع الرجعيّة (ts < live) تاريخ سعر لا جمعه البوت.
    candles = conn.execute(
        "SELECT COUNT(*) AS n FROM token_bars WHERE ts >= ?", (live_start_ts,)
    ).fetchone()["n"]

    no_data = 0
    if _table_exists(conn, "bars_fetch_state"):
        no_data = conn.execute(
            """SELECT COUNT(*) AS n FROM watchlist w
                 JOIN bars_fetch_state s
                   ON s.token_address = w.token_address AND s.network_id = w.network_id
                WHERE w.active = 1 AND s.last_status = 'no_data'"""
        ).fetchone()["n"]

    return {
        "active": active,
        "with_bars": with_bars,
        # لم يصلها الدور بعد (المسح دوّار عبر عدّة دورات) — ليست فشلاً.
        "pending": max(0, active - with_bars - no_data),
        "no_data": no_data,
        "candles": candles,
        "coverage_pct": round(100 * with_bars / active, 1) if active else None,
    }


# --- أداء الإشارات منذ الدخول ---
def _performance_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """لكل عملة مراقَبة: كيف تحرّك سعرها **منذ لحظة الإشارة**.

    هذا هو السؤال الذي بُني المشروع لأجله. سعر الدخول = إغلاق أوّل شمعة عند
    `first_seen_at` أو بعدها (لا قبلها — وإلّا تسرّب المستقبل بالعكس). القمّة
    والقاع من `h`/`l` داخل النافذة نفسها.

    ملاحظة صدق: هذه **نافذة جارية لا نتيجة نهائية** — أغلب المراقَبات لم تُكمل
    48 ساعة بعد، و`peak_pct` يرتفع أبداً ولا ينخفض بمرور الوقت. ليست labels.
    """
    if not _table_exists(conn, "token_bars") or not _table_exists(conn, "watchlist"):
        return []
    has_control = _has_column(conn, "watchlist", "is_control")
    control_sel = "w.is_control AS is_control," if has_control else "0 AS is_control,"
    has_windows = _table_exists(conn, "watch_windows")
    design_join = (
        "LEFT JOIN watch_windows ww ON ww.token_address=w.token_address "
        "AND ww.network_id=w.network_id AND ww.first_seen_at=w.first_seen_at"
        if has_windows else ""
    )
    design_sel = "COALESCE(ww.design_version, 1) AS design_version," if has_windows else (
        "1 AS design_version,"
    )
    # الذيول المستحيلة من المنبع تُستبعد من القمّة/القاع: شوهد h = 2,626,092
    # لشمعة إغلاقها 0.0219 فعرضت اللوحة +62,570,743,609%. العلم يُحسب عند السحب
    # (h_suspect/l_suspect) وهنا نحترمه فقط؛ القيمة الخام تبقى في القاعدة.
    has_flags = _has_column(conn, "token_bars", "h_suspect")
    peak_expr = ("MAX(CASE WHEN b.h_suspect = 1 THEN NULL ELSE b.h END)"
                 if has_flags else "MAX(b.h)")
    trough_expr = ("MIN(CASE WHEN b.l_suspect = 1 THEN NULL ELSE b.l END)"
                   if has_flags else "MIN(b.l)")
    # سعر الدخول/الأخير من إغلاق **سليم**: الإغلاق نفسه يتشوّه أحياناً (12,052.5)
    # فيصير مقام النسبة فاسداً.
    clean_c = "AND c_suspect = 0" if _has_column(conn, "token_bars", "c_suspect") else ""
    t_expr = ("MIN(CASE WHEN b.c_suspect = 1 THEN NULL ELSE b.ts END)"
              if has_flags else "MIN(b.ts)")
    t1_expr = ("MAX(CASE WHEN b.c_suspect = 1 THEN NULL ELSE b.ts END)"
               if has_flags else "MAX(b.ts)")
    rows = conn.execute(
        f"""WITH w AS (
               SELECT token_address, network_id, source, first_seen_at, watch_until,
                      {"is_control," if has_control else ""}
                      CAST(strftime('%s', first_seen_at) AS INTEGER) AS entry_ts
                 FROM watchlist WHERE active = 1
           ),
           agg AS (
               SELECT w.token_address AS a, w.network_id AS n, w.source AS src,
                      w.first_seen_at AS fs, w.watch_until AS wu, w.entry_ts AS ets,
                       {control_sel}
                       {design_sel}
                      COUNT(*) AS candles, {t_expr} AS t0, {t1_expr} AS t1,
                      {peak_expr} AS peak, {trough_expr} AS trough
                 FROM w {design_join} JOIN token_bars b
                   ON b.token_address = w.token_address
                  AND b.network_id = w.network_id
                  AND b.ts >= w.entry_ts
                GROUP BY w.token_address, w.network_id
           )
           SELECT agg.*,
                  (SELECT c FROM token_bars
                    WHERE token_address = agg.a AND network_id = agg.n AND ts = agg.t0
                    {clean_c} LIMIT 1) AS entry_px,
                  (SELECT c FROM token_bars
                    WHERE token_address = agg.a AND network_id = agg.n AND ts = agg.t1
                    {clean_c} LIMIT 1) AS last_px,
                  (SELECT symbol FROM token_static
                    WHERE token_address = agg.a LIMIT 1) AS symbol
             FROM agg"""
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        entry, last, peak = r["entry_px"], r["last_px"], r["peak"]
        if not entry:  # صفر أو None → النِّسَب غير معرّفة، لا نفبركها
            continue
        out.append({
            "token_address": r["a"],
            "network_id": r["n"],
            "symbol": r["symbol"],
            "source": r["src"],
            "is_control": bool(r["is_control"]),
            "design_version": r["design_version"],
            "first_seen_at": r["fs"],
            "watch_until": r["wu"],
            "candles": r["candles"],
            "entry_px": entry,
            "last_px": last,
            "peak_px": peak,
            "trough_px": r["trough"],
            "change_pct": (last / entry - 1) * 100 if last else None,
            "peak_pct": (peak / entry - 1) * 100 if peak else None,
            # كم انخفض عن قمّته الآن — مؤشّر "فاتك البيع"
            "from_peak_pct": (last / peak - 1) * 100 if last and peak else None,
        })
    return out


# مفاتيح الترتيب المسموحة — قائمة بيضاء تمنع أي تعبير عشوائي من الواجهة.
_SORT_KEYS = (
    "peak_pct", "change_pct", "from_peak_pct",
    "first_seen_at", "candles", "entry_px", "last_px", "symbol",
)


def watch_performance(
    conn: sqlite3.Connection,
    limit: int = 12,
    sort_key: str = "peak_pct",
    descending: bool = True,
) -> list[dict[str, Any]]:
    """أفضل/أسوأ العملات حسب مفتاح مختار.

    الترتيب يجري هنا على **المجموعة كاملة** قبل الاقتطاع. لو رُتِّبت في المتصفّح
    بعد اقتطاع أوّل 12، لأعطى العكسُ «أفضل 12 مقلوبة» لا الأسوأ فعلاً — وهو خطأ
    صامت يبدو صحيحاً.
    """
    # الجدول عنوانه «أداء الإشارات» — الضابطة مرجع للمقارنة لا صفوف فيه.
    rows = [r for r in _performance_rows(conn) if not r.get("is_control")]
    key = sort_key if sort_key in _SORT_KEYS else "peak_pct"

    def _sort_value(r: dict[str, Any]) -> Any:
        v = r.get(key)
        if isinstance(v, str):
            return v.lower()
        return v

    # الغائب يبقى في الذيل في الاتجاهين — لا يتصدّر الترتيب التصاعدي بلا معنى.
    present = [r for r in rows if _sort_value(r) is not None]
    missing = [r for r in rows if _sort_value(r) is None]
    present.sort(key=_sort_value, reverse=descending)
    return (present + missing)[: max(1, limit)]


def group_comparison(conn: sqlite3.Connection) -> dict[str, Any]:
    """يقارن عملات **الإشارة** بعملات **المجموعة الضابطة** على نفس المقاييس.

    هذا هو السؤال الذي لا يمكن لبقيّة اللوحة الإجابة عنه: ليس «أيّ عملة مُشار
    إليها ترتفع أكثر» بل **«هل الإشارة تعني شيئاً أصلاً»**. بلا مجموعة ضابطة
    قد تجد 41% من إشاراتك رابحة ثمّ يتّضح أنّ 41% من السوق رابح في تلك المدّة.

    `delta` = فرق الإشارة عن الضابطة. موجب = الإشارة تتفوّق.
    `sufficient` = هل حجم العيّنتين يكفي لأخذ الفرق على محمل الجدّ (لا اختبار
    إحصائيّ هنا؛ عتبة خام تمنع قراءة الضجيج كنتيجة).
    """
    rows = [
        r for r in _performance_rows(conn)
        if r.get("change_pct") is not None and r.get("design_version", 1) >= 2
    ]
    signal = [r for r in rows if not r.get("is_control")]
    control = [r for r in rows if r.get("is_control")]

    def _stats(group: list[dict[str, Any]]) -> dict[str, Any]:
        if not group:
            return {"count": 0, "win_rate_pct": None, "avg_pct": None,
                    "median_pct": None, "avg_peak_pct": None}
        ch = sorted(r["change_pct"] for r in group)
        peaks = [r["peak_pct"] for r in group if r.get("peak_pct") is not None]
        n = len(ch)
        return {
            "count": n,
            "win_rate_pct": sum(1 for c in ch if c > 0) / n * 100,
            "avg_pct": sum(ch) / n,
            "median_pct": ch[n // 2] if n % 2 else (ch[n // 2 - 1] + ch[n // 2]) / 2,
            "avg_peak_pct": sum(peaks) / len(peaks) if peaks else None,
        }

    sig, ctl = _stats(signal), _stats(control)
    delta = {
        k: (sig[k] - ctl[k])
        if sig.get(k) is not None and ctl.get(k) is not None else None
        for k in ("win_rate_pct", "avg_pct", "median_pct", "avg_peak_pct")
    }
    # عتبة خام: أقل من 20 لكل جانب والفرق ضجيج على الأرجح.
    return {"signal": sig, "control": ctl, "delta": delta,
            "sufficient": sig["count"] >= 20 and ctl["count"] >= 20}


def performance_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """حصيلة الأرباح والخسائر عبر **كل** العملات المراقَبة، لا الشريحة المعروضة.

    `change_pct` (السعر الآن مقابل الدخول) هو الأساس — لا `peak_pct`، لأنّ القمّة
    لا تُحقَّق إلّا ببيع في لحظتها. المتوسّط يفترض وزناً متساوياً لكل إشارة.

    تحذير صدق مقصود في `is_open`: هذه **مراكز مفتوحة في نافذة جارية**، لا نتائج
    محقّقة — ولا إشارة أكملت 48 ساعة بعد. الوسيط معروض بجانب المتوسّط لأنّ
    رابحاً واحداً بـ +789% يسحب المتوسّط وحده.
    """
    # الحصيلة تخصّ عملات **الإشارة**؛ الضابطة مرجع للمقارنة لا جزء من الأداء.
    rows = [
        r for r in _performance_rows(conn)
        if r.get("change_pct") is not None and not r.get("is_control")
    ]
    if not rows:
        return {"count": 0, "winners": 0, "losers": 0, "flat": 0, "is_open": True,
                "avg_pct": None, "median_pct": None, "gross_gain_pct": None,
                "gross_loss_pct": None, "net_pct": None, "best": None, "worst": None,
                "win_rate_pct": None}

    changes = sorted(r["change_pct"] for r in rows)
    n = len(changes)
    gains = [c for c in changes if c > 0]
    losses = [c for c in changes if c < 0]
    median = (
        changes[n // 2] if n % 2 else (changes[n // 2 - 1] + changes[n // 2]) / 2
    )
    best = max(rows, key=lambda r: r["change_pct"])
    worst = min(rows, key=lambda r: r["change_pct"])

    def _brief(r: dict[str, Any]) -> dict[str, Any]:
        # network_id مُدرج ليتمكّن العميل من بناء رابط صفحة العملة على fomo
        return {
            "symbol": r.get("symbol"),
            "token_address": r["token_address"],
            "network_id": r.get("network_id"),
            "change_pct": r["change_pct"],
        }

    return {
        "count": n,
        "winners": len(gains),
        "losers": len(losses),
        "flat": n - len(gains) - len(losses),
        "win_rate_pct": len(gains) / n * 100,
        "avg_pct": sum(changes) / n,
        "median_pct": median,
        "gross_gain_pct": sum(gains),
        "gross_loss_pct": sum(losses),        # سالب
        "net_pct": sum(changes),
        "best": _brief(best),
        "worst": _brief(worst),
        # نافذة جارية لا نتائج محقّقة — الواجهة تعرض هذا صراحةً
        "is_open": True,
    }


def token_series(
    conn: sqlite3.Connection, token_address: str, network_id: str,
    since_ts: int, points: int = 40,
) -> list[float]:
    """سلسلة إغلاق مُخفَّضة العيّنات لرسم sparkline. أقل من نقطتين → []."""
    if not _table_exists(conn, "token_bars"):
        return []
    rows = conn.execute(
        """SELECT c FROM token_bars
            WHERE token_address = ? AND network_id = ? AND ts >= ? AND c IS NOT NULL
            ORDER BY ts""",
        (token_address, network_id, since_ts),
    ).fetchall()
    closes = [float(r["c"]) for r in rows]
    if len(closes) < 2:
        return []
    if len(closes) <= points:
        return closes
    # تخفيض بخطوة ثابتة مع ضمان بقاء آخر نقطة (السعر الحالي).
    step = len(closes) / points
    sampled = [closes[int(i * step)] for i in range(points)]
    sampled[-1] = closes[-1]
    return sampled


# --- تدفّق الإشارات عبر الزمن ---
def signal_timeline(conn: sqlite3.Connection, hours: int = 24) -> dict[str, Any]:
    """عدد الإشارات لكل ساعة مقسّمة حسب النوع — لعمود مكدّس.

    نُعيد كل الساعات في المدى حتى الفارغة، وإلّا بدا الرسم متّصلاً عبر فجوة
    توقّف فيها المسجّل.
    """
    if not _table_exists(conn, "signal_events"):
        return {"hours": [], "types": [], "series": {}}
    hours = max(1, min(hours, 168))
    rows = conn.execute(
        """SELECT strftime('%Y-%m-%dT%H:00:00', recorded_at) AS hour,
                  signal_type, COUNT(*) AS n
             FROM signal_events
            WHERE recorded_at >= datetime('now', ?)
            GROUP BY hour, signal_type""",
        (f"-{hours} hours",),
    ).fetchall()
    if not rows:
        return {"hours": [], "types": [], "series": {}}

    counts: dict[str, dict[str, int]] = {}
    types: set[str] = set()
    for r in rows:
        counts.setdefault(r["hour"], {})[r["signal_type"]] = r["n"]
        types.add(r["signal_type"])

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    buckets = [
        (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00")
        for h in range(hours - 1, -1, -1)
    ]
    # ترتيب ثابت للأنواع: اللون يتبع النوع لا رتبته (وإلّا تبدّلت الألوان مع الفلترة)
    ordered = [t for t in _SIGNAL_TYPE_ORDER if t in types]
    ordered += sorted(t for t in types if t not in _SIGNAL_TYPE_ORDER)
    return {
        "hours": buckets,
        "types": ordered,
        "series": {t: [counts.get(b, {}).get(t, 0) for b in buckets] for t in ordered},
    }


# ترتيب ثابت يضمن أنّ كل نوع إشارة يحتفظ بلونه مهما تغيّرت البيانات.
_SIGNAL_TYPE_ORDER = ("multi_user_buy", "large_buy", "multi_user_sell", "large_sell")


# --- storage ---
def storage_stats(
    db_path: str,
    conn: sqlite3.Connection,
    *,
    backup_dir: str | None = None,
    backup_max_age_hours: float = 36.0,
    disk_free_warn_bytes: int = 25 * 1024**3,
    now: datetime | None = None,
) -> dict[str, Any]:
    """حجم القاعدة على القرص + معدّل النموّ اليوميّ المُقدَّر.

    أُضيف لأنّ النموّ غير المحدود بقي خفيّاً 20 ساعة حتى بلغت القاعدة 720 MB:
    اللوحة كانت تعرض عدد الصفوف لا حجمها. المعدّل يُقدَّر من (الحجم / عمر
    الأرشيف) لأنّ الصفوف كلّها بمعدّل ثابت (دورة/دقيقة).
    """
    total_bytes = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total_bytes += os.path.getsize(db_path + suffix)
        except OSError:
            pass

    # عمر الأرشيف من أقدم لقطة — لا من started_at (الذي يُعاد ضبطه كل تشغيل).
    span_days = None
    if _table_exists(conn, "snapshots"):
        row = conn.execute(
            "SELECT MIN(recorded_at) AS lo, MAX(recorded_at) AS hi FROM snapshots"
        ).fetchone()
        lo, hi = _parse_iso(row["lo"]), _parse_iso(row["hi"])
        if lo and hi and hi > lo:
            span_days = (hi - lo).total_seconds() / 86400

    meta = all_meta(conn)
    free_bytes = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free

    latest_backup = None
    backup_age_hours = None
    if backup_dir:
        backup_path = os.path.abspath(os.path.expanduser(backup_dir))
        try:
            candidates = [
                path for path in (
                    os.path.join(backup_path, name)
                    for name in os.listdir(backup_path)
                    if name.startswith("recorder-") and name.endswith(".db")
                )
                if os.path.isfile(path)
            ]
        except OSError:
            candidates = []
        if candidates:
            latest_backup = max(candidates, key=os.path.getmtime)
            backup_dt = datetime.fromtimestamp(os.path.getmtime(latest_backup), UTC)
            backup_age_hours = max(
                0.0, ((now or datetime.now(UTC)) - backup_dt).total_seconds() / 3600
            )

    return {
        "bytes": total_bytes,
        "mb": round(total_bytes / 1e6, 1),
        "span_days": round(span_days, 2) if span_days else None,
        "mb_per_day": round(total_bytes / 1e6 / span_days, 1) if span_days else None,
        "raw_encoding": meta.get("raw_encoding", "plain"),
        "disk_free_bytes": free_bytes,
        "disk_free_gb": round(free_bytes / 1024**3, 1),
        "disk_warning": free_bytes < disk_free_warn_bytes,
        "backup_configured": bool(backup_dir),
        "backup_dir": os.path.abspath(os.path.expanduser(backup_dir)) if backup_dir else None,
        "latest_backup": os.path.basename(latest_backup) if latest_backup else None,
        "backup_age_hours": round(backup_age_hours, 1) if backup_age_hours is not None else None,
        "backup_warning": bool(
            backup_dir
            and (backup_age_hours is None or backup_age_hours > backup_max_age_hours)
        ),
    }


# --- market ticks summary ---
def ticks_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """العدد الكلّي + آخر لقطة سوق لكل عملة مراقَبة نشطة."""
    if not _table_exists(conn, "market_ticks"):
        return {"total": 0, "per_token": []}
    total = conn.execute("SELECT COUNT(*) AS n FROM market_ticks").fetchone()["n"]
    # آخر tick لكل عملة نشطة (أحدث recorded_at).
    rows = conn.execute(
        """SELECT m.token_address, m.recorded_at, m.price_usd, m.holders,
                  m.change_24h, m.volume_24h, m.buy_count_24h, m.sell_count_24h
           FROM market_ticks m
           JOIN watchlist w
             ON w.token_address = m.token_address AND w.active = 1
           JOIN (SELECT token_address, MAX(recorded_at) AS mx
                   FROM market_ticks GROUP BY token_address) last
             ON last.token_address = m.token_address AND last.mx = m.recorded_at
           GROUP BY m.token_address
           ORDER BY m.volume_24h DESC NULLS LAST""",
    ).fetchall()
    return {"total": total, "per_token": [dict(r) for r in rows]}
