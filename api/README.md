# Fomo Family API

Unofficial, **read-only** programmatic API for [fomo.family](https://fomo.family),
a social crypto-trading platform that provides no official API.

> Part of the **aoi** project — see the [root README](../README.md) for the
> full architecture and the [collection plan](../docs/PLAN.md).

## Status

Running in production as the `FomoApiServer` scheduled task. It is the
**authentication and access layer** the recorder depends on: it keeps a fresh
Privy token on disk (`.privy_state.json`) which `FomoRecorder` re-reads every
cycle.

See the design documents under `specs/001-fomo-family-api/`:
- [spec.md](../specs/001-fomo-family-api/spec.md) — requirements
- [plan.md](../specs/001-fomo-family-api/plan.md) — technical plan
- [quickstart.md](../specs/001-fomo-family-api/quickstart.md) — validation guide

## Important — Terms of Service

fomo.family exposes no official API. Before any real use, confirm compliance
with fomo.family's Terms of Service and Privacy Policy. The API is strictly
read-only and uses each consumer's own fomo session.

## Quick start

```bash
cd api
python -m venv .venv && . .venv/Scripts/activate
pip install -e ".[dev]"
playwright install chromium
export FOMO_API_REDIS_URL=redis://localhost:6379/0
uvicorn fomo_api.main:app --reload --port 8000
```

Interactive docs: http://localhost:8000/docs

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `FOMO_API_REDIS_URL` | `redis://localhost:6379/0` | Redis (volatile sessions, rate limits, pub/sub) |
| `FOMO_API_UPSTREAM_BASE` | `https://prod-api.fomo.family` | fomo.family data API base URL |
| `FOMO_API_SESSION_TTL_SECONDS` | `900` | Session token TTL |
| `FOMO_API_RATE_LIMIT_PER_MINUTE` | `60` | Per-consumer request limit |
| `FOMO_API_ALERT_POLL_INTERVAL_SECONDS` | `10` | Alert poller cadence |

`https://fomo.family` is `FOMO_API_UPSTREAM_APP_BASE`, used only for the Privy
OAuth login step.

## Endpoints

All under `/v1`, all read-only, all requiring `Authorization: Bearer <consumer_key>`
(the key is in `api/.privy_state.json.consumer_key`). Every response is wrapped in
`{data, last_refreshed_at, page?}`.

| Route | Upstream |
|---|---|
| `GET /health` | — (liveness + Redis + Privy token readiness for renewal, with no secret exposed, no auth) |
| `POST /v1/auth/login` · `/logout` · `/dev-token` | Privy |
| `GET /v1/leaderboard?period=all\|24h\|7d\|30d` | `/v2/leaderboard[/{period}]` |
| `GET /v1/traders/{id}` | `/v2/users?userIds={id}` (repeated query parameter) |
| `GET /v1/traders/by-handle/{handle}` | `/v2/users/userHandle/{handle}` |
| `GET /v1/traders/{id}/activity` | `/v2/users/{id}/swaps` |
| `GET /v1/traders/{id}/trades` | `/trades?userId=` |
| `GET /v1/traders/{id}/balances` · `/positions` | `/v2/users/{id}/balances` |
| `GET /v1/traders/{id}/spotlight` | `/v2/users/{id}/spotlight` |
| `GET /v1/feed` · `/feed/social` · `/feed/token` · `/feed/token/thesis` | `/feed*` |
| `GET /v1/trades/{id}/comments` | `/trades/{id}/comments` |
| `GET /v1/signals/trending` · `/most-held` · `/verified` · `/top-tokens` | `/proxy/*` |
| `GET /v1/tokens/{symbol}/bars?networkId=&from=&to=` | `/proxy/getBarsNew` |
| `GET /v1/tokens/{id}/details?networkId=` | `/proxy/tokenDetails` |
| `GET /v1/tokens/{addr}/warnings?networkId=` | `/proxy/tokenWarnings` |
| `POST /v1/proxy/filterTokens` · `/v1/hodlers/friends` · `GET /v1/hodlers/top` | `/proxy/*`, `/hodlers/*` |
| `POST /v1/alerts/subscriptions` · `DELETE` · `GET /v1/alerts/stream` | SSE (see below) |

### ⚠️ `bars` and `details` need `networkId`

`getBarsNew` and `tokenDetails` take `"<address>:<networkId>"`, **not a bare
address** — a bare address makes fomo answer a Cloudflare **502** that looks
like an upstream outage but is really a malformed request. `_pair_id()` in
`fomo_client.py` builds the pair; pass `?networkId=`. `from`/`to` are also
mandatory on `bars` (a `400` otherwise).

## Auth — how it stays alive with no login

1. **Bootstrap** (startup): loads `.privy_state.json`, does one browserless
   refresh, mints/reuses a stable consumer key in `.privy_state.json.consumer_key`.
2. **`TokenRefresher`** renews every 60s against `auth.privy.io/api/v1/sessions`.
   Privy **rotates both** `refresh_token` and `pat` on every call, so both are
   persisted back to disk immediately.
3. When Privy replies `session_update_action: "ignore"` with `token: null`, the
   **current** access token is still valid and is kept. Never fall back to
   `privy_access_token` — that is the PAT (`aud=auth.privy.io`), which fomo rejects.
4. The fomo token lives **exactly 60 minutes**. Disk is the single source of
   truth; `FomoRecorder` re-reads it every cycle (it used to freeze the token at
   startup and threw 401 every hour).

`privy-client-id` is **required** for a 200 (omitting it returns 400 "Invalid
auth token") and defaults to a confirmed constant in `config.py`.

## Cloudflare

`upstream_impersonate=True` (default) routes upstream calls through **curl_cffi**
impersonating Chrome. Plain httpx gets a 403 HTML challenge. The test suite sets
`FOMO_API_UPSTREAM_IMPERSONATE=false` so `respx` can intercept.

## Alerts

`POST /v1/alerts/subscriptions` registers trader ids; `GET /v1/alerts/stream`
delivers them over SSE. A background `AlertPoller` (started in the app lifespan)
re-reads all live subscriptions every cycle, polls `/feed/tradingActivity`, and
publishes only events newer than each trader's stored watermark — so no
duplicates and no false positives.

Redis is optional in dev: when it is unreachable and `FOMO_API_DEV=true`, the app
falls back to an in-memory `FakeRedis` that implements the same surface,
including pub/sub — the alert path works either way. Subscriptions and watermarks
are then process-local and reset on restart.

## Logs

Under the `FomoApiServer` scheduled task the server runs via `pythonw`, so
`serve.py` installs a rotating file handler at `api/server.log` (5 MB × 3).
`uvicorn.run(log_config=None)` is required there (the default formatter calls
`isatty()` on a missing stdout), and it also means no handler is installed by
default — without `serve.py`'s handler, every application log line is discarded.
Boot failures additionally land in `api/server_boot.log`.

## Tests

```bash
cd api && pytest
```

Or run all three suites (api, recorder, dashboard) plus lint from the repo root:

```powershell
.\run_tests.ps1
```

## Scheduled task

Registered as `FomoApiServer`, running `pythonw.exe api\serve.py` at logon.

⚠️ **Do not** revert the task action to `-m uvicorn ...` arguments — it fails
silently with `LastTaskResult=1`. The single-argument `serve.py` wrapper is what
made it work, and `pythonw` (not a `.bat`) is what keeps a console window from
flashing.

```powershell
Get-ScheduledTask   -TaskName FomoApiServer | Select State
Get-ScheduledTaskInfo -TaskName FomoApiServer
```

Note that `Stop-ScheduledTask` returns the task to `Ready` while the `pythonw`
process may still be alive — kill by process when you need a guaranteed stop
(see root README §11).
