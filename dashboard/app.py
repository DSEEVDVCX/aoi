"""خادم اللوحة: قراءةٌ من قاعدة المسجّل، وكتابةٌ في ملفّ المفاتيح وحدَه.

- **القاعدة للقراءة فقط**: `dao` يفتحها بـmode=ro ولا مسار كتابة إليها. هذا لا
  يُساوَم عليه — كاتبٌ ثانٍ يزاحم المسجّل على قفلٍ رأينا موتَه ثلاثَ ساعاتٍ مرّة.
- مسارُ الكتابة الوحيد `/api/provider-keys/{add,toggle,delete}` وهدفُه ملفُّ
  `recorder/chain_keys.json` عبر `keystore`. وهذا هو أوّلُ مسارٍ يغيّر الحالة في
  هذا الخادم — والحارس أدناه كان مكتوباً وجاهزاً قبله، فلم يُضَف على عجل.
- الاستماع على 127.0.0.1 فقط (محلّي).
- Host guard + CSRF: يمنعان DNS rebinding ويحرسان مسارات الكتابة تلك.
- **والتخزينُ المؤقّت لا يخرق شيئاً من ذلك**: `cache.MEMO` ذاكرةُ هذه العمليّة
  وحدها — لا جدولَ تخزينٍ في القاعدة ولا ختمَ في `meta` ولا فهرسَ يُبنى. ثلاثةُ
  مساراتٍ تعدّ التاريخَ كلَّه (`networks`/`counts`/`ticks-summary`) تُحسب مرّةً
  كلَّ مدّة بدل كلِّ عشر ثوانٍ؛ والسببُ والقياسُ في `cache.py`.
"""
from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import cache
import config
import dao
import keystore

app = FastAPI(title="Fomo Recorder Dashboard", docs_url=None, redoc_url=None)
_DASHBOARD_TOKEN = secrets.token_hex(32)


def _allowed_origins() -> tuple[str, str]:
    port = config.DASHBOARD_PORT
    return (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


@app.middleware("http")
async def local_security_guard(request: Request, call_next):
    """يمنع DNS rebinding وCSRF قبل وصول أي طلب يغيّر مخزن الأسرار."""
    allowed_origins = _allowed_origins()
    allowed_hosts = {origin.removeprefix("http://") for origin in allowed_origins}
    host = request.headers.get("host", "").lower()
    if host not in allowed_hosts:
        return JSONResponse(
            {"error": "مرفوض: Host ليس عنوان اللوحة المحلي"}, status_code=403
        )
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > 8192:
            return JSONResponse({"error": "الطلب كبير جداً"}, status_code=413)
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if origin and origin not in allowed_origins:
            return JSONResponse({"error": "أصل الطلب غير مسموح"}, status_code=403)
        if referer and not any(
            referer == allowed or referer.startswith(allowed + "/")
            for allowed in allowed_origins
        ):
            return JSONResponse({"error": "مرجع الطلب غير مسموح"}, status_code=403)
        sent = request.headers.get("x-dashboard-token", "")
        if not sent or not hmac.compare_digest(sent, _DASHBOARD_TOKEN):
            return JSONResponse({"error": "رمز حماية اللوحة مفقود أو خاطئ"}, status_code=403)
    response = await call_next(request)
    if request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


def _with_conn(fn):
    """يفتح اتصال read-only، ينفّذ الدالة، يغلق دوماً."""
    conn = dao.connect_ro(config.DB_PATH)
    try:
        return fn(conn)
    finally:
        conn.close()


def _cached(key: str, ttl: float, fn) -> tuple[Any, dict[str, Any]]:
    """قيمةٌ مخزَّنة مؤقّتاً لاستعلامٍ ثقيل، مع وصفِ طزاجتها للعرض.

    الاتصالُ يُفتح **داخل** المُغلَّف لا خارجَه: التجديدُ يجري في خيطٍ خلفيّ بعد
    أن يكون الطلبُ الذي أشعله قد أغلق اتصالَه، فتمريرُ اتصالٍ من هنا كان يعني
    استعمالَه بعد الإغلاق. انظر `cache.py` لسبب «تُقدَّم البائتةُ فوراً».
    """
    return cache.MEMO.get(
        key, ttl, lambda: _with_conn(fn),
        error_backoff=config.CACHE_ERROR_BACKOFF_SECONDS,
    )


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return _with_conn(
        lambda c: dao.recorder_status(
            c, config.RECORDER_ALIVE_WINDOW_SECONDS,
            labeler_window_seconds=config.LABELER_ALIVE_WINDOW_SECONDS,
        )
    )


@app.get("/api/api-health")
def api_health() -> dict[str, Any]:
    """يفحص /health للـ API المحلّي. فشل/مهلة → connected=false مع سبب واضح."""
    import time

    t0 = time.perf_counter()
    try:
        resp = httpx.get(config.API_HEALTH_URL, timeout=config.API_HEALTH_TIMEOUT)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        body: Any
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:200]}
        status_str = body.get("status") if isinstance(body, dict) else None
        # 200 + status=ok → متصل سليم. 503/degraded → متصل لكن معطوب.
        connected = resp.status_code == 200
        return {
            "connected": connected,
            "http_status": resp.status_code,
            "status": status_str,
            "degraded": resp.status_code != 200 or status_str not in (None, "ok"),
            "latency_ms": latency_ms,
            "detail": None if connected else f"HTTP {resp.status_code}",
        }
    except httpx.TimeoutException:
        return {
            "connected": False, "http_status": None, "status": None,
            "degraded": True, "latency_ms": None, "detail": "مهلة انتهت (لا استجابة)",
        }
    except httpx.ConnectError:
        return {
            "connected": False, "http_status": None, "status": None,
            "degraded": True, "latency_ms": None, "detail": "منقطع (الخادم لا يعمل؟)",
        }
    except Exception as e:  # noqa: BLE001 — نعرض السبب دون إسقاط اللوحة
        return {
            "connected": False, "http_status": None, "status": None,
            "degraded": True, "latency_ms": None, "detail": f"خطأ: {type(e).__name__}",
        }


@app.get("/api/signals")
def api_signals(limit: int = 50) -> dict[str, Any]:
    rows = _with_conn(lambda c: dao.recent_signals(c, limit))
    return {"signals": rows}


@app.get("/api/watchlist")
def api_watchlist() -> dict[str, Any]:
    rows = _with_conn(dao.active_watchlist)
    return {"watchlist": rows}


@app.get("/api/ticks-summary")
def api_ticks_summary() -> dict[str, Any]:
    summary, meta = _cached(
        "ticks_summary", config.TICKS_SUMMARY_TTL_SECONDS, dao.ticks_summary,
    )
    return {**summary, "cache": meta}


@app.get("/api/networks")
def api_networks() -> dict[str, Any]:
    """تغطيةُ الشبكات: أعدادٌ مخزَّنة مؤقّتاً، وطزاجةٌ حيّةٌ فوقها.

    الأعدادُ تلزمها مسحةٌ كاملة (1724 مللي ثانية) فتُخزَّن؛ أمّا «آخرُ لقطة» فهو
    الحقلُ الذي يُقرأ كنبضٍ ويُلاحظ تأخّرُه فوراً، وله طريقٌ يكلّف 0.4 مللي ثانية
    ⇒ يُحسب حيّاً في كلّ طلبٍ ويُدمج فوق المخزَّن بلا أن يتراجع.
    """
    rows, meta = _cached(
        "network_summary", config.NETWORK_SUMMARY_TTL_SECONDS, dao.network_summary,
    )
    live = _with_conn(dao.latest_tick_per_active_network)
    return {"networks": dao.with_live_latest_tick(rows, live), "cache": meta}


@app.get("/api/counts")
def api_counts() -> dict[str, Any]:
    counts, meta = _cached(
        "table_counts", config.TABLE_COUNTS_TTL_SECONDS, dao.table_counts,
    )
    return {**counts, "cache": meta}


@app.get("/api/control-progress")
def api_control_progress() -> dict[str, Any]:
    return _with_conn(
        lambda c: dao.control_maturity(
            c,
            config.CONTROL_PRELIMINARY_TARGET,
            config.CONTROL_DECISION_TARGET,
            config.CONTROL_DESIGN_VERSION,
        )
    )


@app.get("/api/performance")
def api_performance(
    limit: int = 12, sort: str = "peak_pct", dir: str = "desc"
) -> dict[str, Any]:
    """أداء كل عملة مراقَبة منذ لحظة إشارتها + حصيلة الأرباح/الخسائر.

    الترتيب يجري على الخادم فوق المجموعة كاملة ثمّ يُقتطع — الترتيب في المتصفّح
    بعد الاقتطاع كان سيعطي «أفضل N مقلوبة» لا الأسوأ فعلاً.
    الحصيلة (`summary`) تُحسب على **كل** العملات لا على الشريحة المعروضة.
    """
    def _load(conn) -> dict[str, Any]:
        rows = dao.watch_performance(
            conn,
            limit=max(1, min(limit, 100)),
            sort_key=sort,
            descending=dir.lower() != "asc",
        )
        for r in rows:
            entry_ts = int(datetime.fromisoformat(r["first_seen_at"]).timestamp())
            r["spark"] = dao.token_series(
                conn, r["token_address"], r["network_id"], entry_ts
            )
        return {
            "performance": rows,
            "summary": dao.performance_summary(conn),
            "comparison": dao.group_comparison(conn),
            "sort": {"key": sort, "dir": "asc" if dir.lower() == "asc" else "desc"},
        }

    return _with_conn(_load)


@app.get("/api/signal-timeline")
def api_signal_timeline(hours: int = 24) -> dict[str, Any]:
    """عدد الإشارات لكل ساعة حسب النوع — لعمود مكدّس."""
    return _with_conn(lambda c: dao.signal_timeline(c, hours=hours))


@app.get("/api/bars")
def api_bars() -> dict[str, Any]:
    """تغطية الشموع — المقياس الحاسم لجاهزية البيانات للتوسيم."""
    return _with_conn(lambda c: dao.bars_coverage(c, config.LIVE_START_TS))


@app.get("/api/storage")
def api_storage() -> dict[str, Any]:
    """حجم القاعدة ومعدّل نموّها — رقابة على الانفجار الصامت للأرشيف."""
    return _with_conn(
        lambda c: dao.storage_stats(
            config.DB_PATH,
            c,
            backup_dir=config.BACKUP_DIR,
            backup_max_age_hours=config.BACKUP_MAX_AGE_HOURS,
            disk_free_warn_bytes=int(config.DISK_FREE_WARN_GB * 1024**3),
        )
    )


@app.get("/api/errors")
def api_errors() -> dict[str, Any]:
    rows = _with_conn(lambda c: dao.recorder_errors(
        c,
        config.RECORDER_SOURCES,
        # لكل مصدرٍ ختمُ نجاحه من كاتبه؛ حدُّ المسجّل أساسٌ لمصادر دورته وحدها.
        ok_stamps=config.SOURCE_OK_STAMPS,
        recorder_stamps=config.RECORDER_OK_STAMPS,
    ))
    return {"errors": rows}


@app.get("/api/provider-keys")
def api_provider_keys() -> dict[str, Any]:
    """أحواضُ المزوّدين وأسطرُ مفاتيحهم — حالةٌ وأسماءُ حسابات، **بلا قيمة**.

    مصدران لا مصدرٌ واحد، وهذا مقصود: `pools` أسطرُ `meta` التي كتبتها العمليّات
    المالكة (أعدادٌ ومؤشّرات فقط، ولا تحمل مفتاحاً أصلاً)، و`keys` قراءةُ الملفّ
    نفسه — منها اسمُ الحساب وآخرُ أربعة أحرف (`key_file.tail`، خفضٌ مقصودٌ
    لِـ FR-013 طلبه المستخدم للتمييز). القيمةُ كاملةً لا تخرج من الخادم أبداً.

    ودمجُهما هنا لا في المتصفّح: حالةُ المفتاح = ملفٌّ (مفعّل؟) + حوضٌ (مبرَّد؟)
    + فحصٌ حيّ، وثلاثتُها لا تُقرأ من مكانٍ واحد.
    """
    pools = _with_conn(lambda c: dao.provider_keys(
        c,
        prefix=config.PROVIDER_KEY_META_PREFIX,
        min_keys=config.PROVIDER_KEY_MIN_KEYS,
        stale_seconds=config.PROVIDER_KEY_STALE_SECONDS,
    ))
    return {"pools": pools, "min_keys": config.PROVIDER_KEY_MIN_KEYS, **keystore.rows(pools)}


async def _body(request: Request) -> dict[str, Any]:
    """جسمُ الطلب كقاموس. **لا يُسجَّل ولا يُعاد في رسالة خطأ** — فيه المفتاح."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — أيُّ عطبِ تحليلٍ جوابُه واحد
        raise keystore.KeyStoreError("جسم الطلب ليس JSON صالحاً") from None
    if not isinstance(data, dict):
        raise keystore.KeyStoreError("جسم الطلب يجب أن يكون كائن JSON")
    return data


def _slot(data: dict[str, Any]) -> int:
    try:
        return int(data.get("slot"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise keystore.KeyStoreError("موضع المفتاح مفقود أو غير صحيح") from None


def _fail(exc: keystore.KeyStoreError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.post("/api/provider-keys/add")
async def api_provider_keys_add(request: Request) -> Any:
    """يضيف مفتاحاً إلى ملفّ المسجّل. لا إعادةَ تشغيلٍ لازمة.

    العملاء يقرأون الملفّ **عند كلّ نداء** (`solana_rpc._post`، `goldrush_rpc._key`،
    `nodereal_rpc._call` تنادي `refresh(_read_keys())`)، فالمفتاح الجديد يدخل
    الدورة التالية من نفسه — ولذلك لا تُوقف اللوحة مهمّةً ولا تلمس عمليّةً.
    """
    try:
        data = await _body(request)
        return keystore.add(
            str(data.get("provider") or ""),
            str(data.get("key") or ""),
            str(data.get("label") or ""),
        )
    except keystore.KeyStoreError as exc:
        return _fail(exc)


@app.post("/api/provider-keys/toggle")
async def api_provider_keys_toggle(request: Request) -> Any:
    """يوقف مفتاحاً مؤقّتاً أو يعيده. القيمة تبقى في الملفّ، ويخرج من الحوض."""
    try:
        data = await _body(request)
        return keystore.set_enabled(
            str(data.get("provider") or ""),
            _slot(data),
            str(data.get("tail") or ""),
            bool(data.get("enabled")),
            str(data.get("key_id") or ""),
        )
    except keystore.KeyStoreError as exc:
        return _fail(exc)


@app.post("/api/provider-keys/delete")
async def api_provider_keys_delete(request: Request) -> Any:
    """يحذف مفتاحاً نهائيّاً — لا تراجع، فالقيمة لا تُحفظ في مكانٍ آخر."""
    try:
        data = await _body(request)
        return keystore.remove(
            str(data.get("provider") or ""),
            _slot(data),
            str(data.get("tail") or ""),
            str(data.get("key_id") or ""),
        )
    except keystore.KeyStoreError as exc:
        return _fail(exc)


@app.post("/api/provider-keys/test")
async def api_provider_keys_test(request: Request) -> Any:
    """نداءٌ حقيقيّ واحد بهذا المفتاح: «مقبول» ليست «الخدمة تعمل».

    حالةُ العمّال وحدها لا تكفي: مفتاحٌ أُضيف قبل دقيقة لم يُنادَ به بعد، فيظهر
    أخضرَ بلا دليل. والزرُّ هو الدليل. وهو POST لا GET رغم أنّه قراءة: يخرج
    نداءً بمفتاحٍ سرّيّ إلى الخارج، فيمرّ بحارس الرمز مثل بقيّة ما يغيّر شيئاً.
    """
    try:
        data = await _body(request)
        return keystore.probe(
            str(data.get("provider") or ""),
            _slot(data),
            str(data.get("tail") or ""),
            str(data.get("key_id") or ""),
        )
    except keystore.KeyStoreError as exc:
        return _fail(exc)


@app.get("/")
def index() -> HTMLResponse:
    """صفحة اللوحة — **بلا تخزين مؤقّت**.

    اللوحة تُحدّث بياناتها كل 10 ثوانٍ، لكنّ هيكلها (HTML+JS) كان يُخدَّم من كاش
    المتصفّح إلى أجل غير مسمّى: بعد أي تحديث للوحة يبقى المستخدم على النسخة
    القديمة بلا أي إشارة — بما في ذلك بعد إصلاح عطب فيها.
    """
    html = Path(config.STATIC_DIR, "index.html").read_text(encoding="utf-8")
    html = html.replace("__DASHBOARD_TOKEN__", _DASHBOARD_TOKEN)
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; object-src 'none'"
            ),
        },
    )


# ملفات ثابتة إضافية إن لزم (لا شيء الآن، لكن يبقى المسار متاحاً).
if os.path.isdir(config.STATIC_DIR):
    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
