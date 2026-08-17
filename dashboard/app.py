"""خادم اللوحة: قراءة فقط من قاعدة بيانات المسجّل.

- لا كتابة إطلاقاً: `dao` يفتح القاعدة بـmode=ro ولا مسار كتابة في الخادم.
- الاستماع على 127.0.0.1 فقط (محلّي).
- Host guard + CSRF يبقيان: يمنعان DNS rebinding ويؤمّنان أي طلب يغيّر الحالة
  لو أُضيف لاحقاً — الحارس أرخص من تذكّر إعادته عند أوّل مسار كتابة.
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

import config
import dao

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
    return _with_conn(dao.ticks_summary)


@app.get("/api/networks")
def api_networks() -> dict[str, Any]:
    return _with_conn(lambda c: {"networks": dao.network_summary(c)})


@app.get("/api/counts")
def api_counts() -> dict[str, Any]:
    return _with_conn(dao.table_counts)


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
    rows = _with_conn(lambda c: dao.recorder_errors(c, config.RECORDER_SOURCES))
    return {"errors": rows}


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
