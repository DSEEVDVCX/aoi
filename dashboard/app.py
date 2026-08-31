"""The dashboard server: reads from the recorder database, writes only to the key file.

- **The database is read-only**: `dao` opens it with mode=ro and there is no
  write path to it. This is non-negotiable — a second writer would contend
  with the recorder for a lock whose death we once watched for three hours.
- The only write path `/api/provider-keys/{add,toggle,delete}` targets the
  `recorder/chain_keys.json` file via `keystore`. And this is the first
  state-changing route in this server — the guard below was written and ready
  before it, not bolted on in a hurry.
- And the second write path `/api/fomo-account/{switch,restore}` targets the
  `api/.privy_state.json` file via `account`: switching the fomo account when
  an identity gets blocked. It runs no shell command and stops no scheduled
  task — a single file write, because the recorder reads that file every
  cycle and picks up the switch on its own.
- Listening on 127.0.0.1 only (localhost).
- Host guard + CSRF: they block DNS rebinding and guard those write paths.
- **And the cache breaks none of that**: `cache.MEMO` is this process's own
  memory — no staging table in the database, no stamp in `meta`, no index
  being built. Three paths that re-count the whole history
  (`networks`/`counts`/`ticks-summary`) are computed once per period instead
  of every ten seconds; the reason and the measurements are in `cache.py`.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

import account
import cache
import config
import dao
import httpx
import keystore
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Fomo Recorder Dashboard", docs_url=None, redoc_url=None)
_DASHBOARD_TOKEN = secrets.token_hex(32)


def _allowed_origins() -> tuple[str, str]:
    port = config.DASHBOARD_PORT
    return (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


# The request body cap. The default is deliberately narrow — a provider key is
# one line. But the account switch receives a pasted browser store containing
# tokens hundreds of characters long plus other keys that aren't ours, easily
# exceeding eight thousand — and it used to be rejected with "request too
# large", a rejection the user couldn't understand the reason for.
_BODY_LIMIT_DEFAULT = 8192
_BODY_LIMITS = {"/api/fomo-account/switch": 262144}


@app.middleware("http")
async def local_security_guard(request: Request, call_next):
    """Blocks DNS rebinding and CSRF before any request that changes the secret store arrives."""
    allowed_origins = _allowed_origins()
    allowed_hosts = {origin.removeprefix("http://") for origin in allowed_origins}
    host = request.headers.get("host", "").lower()
    if host not in allowed_hosts:
        return JSONResponse(
            {"error": "rejected: Host is not the local dashboard address"}, status_code=403
        )
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        length = request.headers.get("content-length")
        cap = _BODY_LIMITS.get(request.url.path, _BODY_LIMIT_DEFAULT)
        if length and length.isdigit() and int(length) > cap:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if origin and origin not in allowed_origins:
            return JSONResponse({"error": "request origin not allowed"}, status_code=403)
        if referer and not any(
            referer == allowed or referer.startswith(allowed + "/")
            for allowed in allowed_origins
        ):
            return JSONResponse({"error": "request referer not allowed"}, status_code=403)
        sent = request.headers.get("x-dashboard-token", "")
        if not sent or not hmac.compare_digest(sent, _DASHBOARD_TOKEN):
            return JSONResponse({"error": "dashboard token missing or invalid"}, status_code=403)
    response = await call_next(request)
    if request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


def _with_conn(fn):
    """Opens a read-only connection, runs the function, always closes."""
    conn = dao.connect_ro(config.DB_PATH)
    try:
        return fn(conn)
    finally:
        conn.close()


def _cached(key: str, ttl: float, fn) -> tuple[Any, dict[str, Any]]:
    """A cached value for a heavy query, with a freshness description for display.

    The connection is opened **inside** the wrapper, not outside it: the
    refresh runs in a background thread after the request that triggered it
    has closed its connection, so passing a connection from here would have
    meant using it after it was closed. See `cache.py` for why "the stale
    value is served instantly".
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
    """Checks /health on the local API. Failure/timeout → connected=false with a clear reason."""
    import time

    t0 = time.perf_counter()
    try:
        resp = httpx.get(config.API_HEALTH_URL, timeout=config.API_HEALTH_TIMEOUT)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        body: Any
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — a non-JSON answer is shown raw
            body = {"raw": resp.text[:200]}
        status_str = body.get("status") if isinstance(body, dict) else None
        # 200 + status=ok → connected and healthy. 503/degraded → connected but broken.
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
            "degraded": True, "latency_ms": None, "detail": "timed out (no response)",
        }
    except httpx.ConnectError:
        return {
            "connected": False, "http_status": None, "status": None,
            "degraded": True, "latency_ms": None, "detail": "disconnected (server not running?)",
        }
    except Exception as e:  # noqa: BLE001 — show the reason without dropping the dashboard
        return {
            "connected": False, "http_status": None, "status": None,
            "degraded": True, "latency_ms": None, "detail": f"error: {type(e).__name__}",
        }


@app.get("/api/signals")
def api_signals(limit: int = 50) -> dict[str, Any]:
    rows = _with_conn(lambda c: dao.recent_signals(c, limit))
    return {"signals": rows}


@app.get("/api/watchlist")
def api_watchlist() -> dict[str, Any]:
    rows = _with_conn(dao.active_watchlist)
    return {"watchlist": rows}


@app.get("/api/watchlist-market")
def api_watchlist_market() -> dict[str, Any]:
    """Price movement + sparkline per active watch — the heavy half of the watchlist page.

    The 48-hour countdown must tick every 10 s, but recomputing entry/peak/
    sparkline for ~150 coins on that cadence would hammer the bars table for
    numbers that barely move. So the light `/api/watchlist` carries the window
    and this cached route carries the market half; the page merges them by
    `address|network`. Same split as `/api/networks`: cached heavy body, live
    freshness layered on top.
    """
    rows, meta = _cached(
        "watchlist_market", config.WATCHLIST_MARKET_TTL_SECONDS, dao.watchlist_market,
    )
    return {"market": rows, "cache": meta}


@app.get("/api/ticks-summary")
def api_ticks_summary() -> dict[str, Any]:
    summary, meta = _cached(
        "ticks_summary", config.TICKS_SUMMARY_TTL_SECONDS, dao.ticks_summary,
    )
    return {**summary, "cache": meta}


@app.get("/api/networks")
def api_networks() -> dict[str, Any]:
    """Network coverage: cached counts, with live freshness layered on top.

    The counts need a full sweep (1724 ms) so they're cached; but the "latest
    snapshot" is the field read as the pulse whose lag is noticed immediately,
    and it has a route costing 0.4 ms ⇒ it's computed live on every request
    and merged on top of the cached value without regressing it.
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
    """Performance of every watched coin since its signal + the profit/loss tally.

    Sorting happens on the server over the full set and is then truncated —
    sorting in the browser after truncation would have given "top N inverted"
    rather than the actual worst.
    The tally (`summary`) is computed over **all** coins, not the displayed slice.
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
    """Signal count per hour by type — for a stacked column."""
    return _with_conn(lambda c: dao.signal_timeline(c, hours=hours))


@app.get("/api/bars")
def api_bars() -> dict[str, Any]:
    """Candle coverage — the decisive measure of data readiness for labeling."""
    return _with_conn(lambda c: dao.bars_coverage(c, config.LIVE_START_TS))


@app.get("/api/labeling")
def api_labeling() -> dict[str, Any]:
    """Labeling and outcomes after 48 hours — the baseline's output, cached.

    The heaviest query in the dashboard after the network summary (1.2 seconds
    measured), and its numbers are final, changing only at the labeler's
    cadence (every 15 minutes), so it's cached with a five-minute TTL. And the
    last labeling activity is read live on top of it — without waiting for the
    refresh — because it's the labeler's pulse from its output, and the
    difference between "five minutes old" and "fresh" is visible.
    """
    summary, meta = _cached(
        "labeling",
        config.LABELING_TTL_SECONDS,
        lambda c: dao.labeling_outcomes(
            c,
            config.LIVE_START_TS,
            design_version=config.CONTROL_DESIGN_VERSION,
            gate_targets=(
                config.CONTROL_PRELIMINARY_TARGET,
                config.CONTROL_DECISION_TARGET,
            ),
        ),
    )
    live_last = _with_conn(dao.last_labeled_at)
    out = dict(summary)
    out["last_labeled_at"] = live_last or out.get("last_labeled_at")
    out["cache"] = meta
    return out


@app.get("/api/storage")
def api_storage() -> dict[str, Any]:
    """Database size and growth rate — monitoring the archive's silent explosion."""
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
        # Each source has its own success stamp from its own writer; the recorder's
        # boundary is the basis only for the sources of its cycle.
        ok_stamps=config.SOURCE_OK_STAMPS,
        recorder_stamps=config.RECORDER_OK_STAMPS,
    ))
    return {"errors": rows}


@app.get("/api/provider-keys")
def api_provider_keys() -> dict[str, Any]:
    """Provider pools and their key rows — status and account names, **no values**.

    Two sources, not one, and that's deliberate: `pools` is the `meta` rows the
    owning processes wrote (counts and gauges only, carrying no key at all),
    and `keys` is a read of the file itself — from which come the account name
    and the last four characters (`key_file.tail`, a deliberate relaxation of
    FR-013 the user requested for telling keys apart). The full value never
    leaves the server.

    And they're merged here, not in the browser: a key's status = file
    (enabled?) + pool (thawed?) + live probe, and the three are not read from
    one place.
    """
    pools = _with_conn(lambda c: dao.provider_keys(
        c,
        prefix=config.PROVIDER_KEY_META_PREFIX,
        min_keys=config.PROVIDER_KEY_MIN_KEYS,
        stale_seconds=config.PROVIDER_KEY_STALE_SECONDS,
    ))
    return {"pools": pools, "min_keys": config.PROVIDER_KEY_MIN_KEYS, **keystore.rows(pools)}


async def _body(request: Request) -> dict[str, Any]:
    """The request body as a dict. **Never logged and never echoed in an error message** — it contains the key."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — any parse failure gets one answer
        raise keystore.KeyStoreError("request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise keystore.KeyStoreError("request body must be a JSON object")
    return data


def _slot(data: dict[str, Any]) -> int:
    try:
        return int(data.get("slot"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise keystore.KeyStoreError("key slot missing or invalid") from None


def _fail(exc: keystore.KeyStoreError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.post("/api/provider-keys/add")
async def api_provider_keys_add(request: Request) -> Any:
    """Adds a key to the recorder's file. No restart needed.

    The clients read the file **on every call** (`solana_rpc._post` and
    `nodereal_rpc._call` invoke `refresh(_read_keys())`), so the new key joins
    the next cycle on its own — which is why the dashboard stops no task and
    touches no process.
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
    """Suspends a key temporarily or restores it. The value stays in the file; it just leaves the pool."""
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
    """Deletes a key permanently — no undo, since the value is stored nowhere else."""
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
    """One real call with this key: "accepted" is not "the service works".

    The workers' state alone isn't enough: a key added a minute ago hasn't been
    called yet, so it shows green with no evidence. The button is the evidence.
    And it's a POST, not a GET, even though it's a read: it sends a call with a
    secret key to the outside, so it passes the token guard like everything
    else that changes something.
    """
    try:
        data = await _body(request)
        return await asyncio.to_thread(
            keystore.probe,
            str(data.get("provider") or ""),
            _slot(data),
            str(data.get("tail") or ""),
            str(data.get("key_id") or ""),
        )
    except keystore.KeyStoreError as exc:
        return _fail(exc)


def _fail_account(exc: account.AccountError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


async def _account_body(request: Request) -> dict[str, Any]:
    """The account request body. **Never logged and never echoed in an error message** — it contains the token."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — any parse failure gets one answer
        raise account.AccountError("request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise account.AccountError("request body must be a JSON object")
    return data


@app.get("/api/fomo-account")
def api_fomo_account() -> dict[str, Any]:
    """The current fomo account's status — identity fingerprint and expiry time, **no value**.

    And no live probe here: this route is called with every dashboard refresh
    every ten seconds, and its upstream call would have hit fomo six times a
    minute with nobody asking for it — which is exactly the kind of load that
    triggered the block. The check is an explicit button.
    """
    return account.status()


@app.post("/api/fomo-account/probe")
async def api_fomo_account_probe() -> Any:
    """Probes the current account live: 200 works, 403 blocked, 401 expired token.

    In a separate thread: `curl_cffi` is synchronous and three calls can hit
    their timeout, which would freeze the event loop and stall the whole
    dashboard for everyone during the probe.
    """
    try:
        return await asyncio.to_thread(account.probe)
    except account.AccountError as exc:
        return _fail_account(exc)


@app.post("/api/fomo-account/switch")
async def api_fomo_account_switch(request: Request) -> Any:
    """Switches the account from a pasted browser store, after an automatic backup.

    And no restart is needed: the recorder reads the file every cycle
    (`_reload_token`), and the api server adopts the new identity on its next
    refresh because the file's fingerprint differs from the one it is
    refreshing (`TokenRefresher._adopt_disk_identity`).
    """
    try:
        return account.switch(await _account_body(request))
    except account.AccountError as exc:
        return _fail_account(exc)


@app.post("/api/fomo-account/restore")
async def api_fomo_account_restore(request: Request) -> Any:
    """Reverts to a saved backup — the emergency exit from a bad paste."""
    try:
        data = await _account_body(request)
        return account.restore(str(data.get("name") or ""))
    except account.AccountError as exc:
        return _fail_account(exc)


@app.get("/")
def index() -> HTMLResponse:
    """The dashboard page — **not cached**.

    The dashboard refreshes its data every 10 seconds, but its skeleton
    (HTML+JS) used to be served from the browser cache indefinitely: after any
    dashboard update the user stayed on the old version with no signal at all
    — including after a bug in it was fixed.
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


# Extra static files if ever needed (nothing now, but the route stays available).
if os.path.isdir(config.STATIC_DIR):
    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
