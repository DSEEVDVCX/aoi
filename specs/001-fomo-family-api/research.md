# Phase 0 Research: Fomo Family API

**Feature**: 001-fomo-family-api
**Date**: 2026-07-23
**Status**: Complete — all NEEDS CLARIFICATION items resolved

This document records the technical decisions made during planning, with
rationale and alternatives considered. It resolves the unknowns identified in
the plan's Technical Context section.

## Target System Reconnaissance

fomo.family's public web app (`https://fomo.family/leaderboard` and friends)
was inspected. Findings:

- **Stack**: React Router v7 in SPA mode (`window.__reactRouterContext`:
  `ssr:false`, `isSpaMode:true`). Data is fetched client-side via XHR/fetch to
  internal JSON endpoints (React Query `QueryClientProvider` is loaded).
- **Authentication**: **Privy** is used for login. Evidence: `privy_oauth_code`,
  `privy_oauth_state`, `privy_oauth_provider` query params on OAuth callback;
  `privy:refresh_token` in localStorage; `privy-session` cookie. Privy is a
  Web3/social auth provider — login is via wallet signature or social OAuth,
  not a simple username/password POST.
- **Analytics**: PostHog. **Fonts**: Aeonik. No official public API is
  documented or advertised.

**Implication**: The leaderboard/profile/feed data the SPA renders comes from
internal HTTP endpoints the browser calls after Privy login. The unofficial
API must (a) obtain an authenticated Privy session and (b) replay that session
against the reverse-engineered internal endpoints.

## Decision 1 — Language / Runtime

**Decision**: Python 3.12.

**Rationale**: The feature is an async reverse-engineering proxy with
real-time streaming. Python 3.12 + FastAPI provides first-class async I/O
(httpx), native WebSocket/SSE support, Pydantic validation, simple
cookie/bearer replay, and a mature ecosystem for HTTP automation and
headless-browser auth (Playwright). For an exploratory, unofficial client
where the upstream surface must be discovered iteratively, Python's
exploration/automation ecosystem yields the highest development velocity.

**Alternatives considered**:
- TypeScript/Node.js (Fastify/Hono + undici): strong async, matches fomo's
  own stack, excellent for proxying. Rejected as primary because Python's
  scraping/automation tooling (httpx + Playwright + rich JSON wrangling) is
  faster to iterate on for an unknown upstream; a TS client SDK can follow
  later.
- Go: best raw concurrency for a proxy, but weaker ergonomics for iterative
  JSON reverse-engineering. Rejected for an exploratory first cut.

## Decision 2 — Data Acquisition Strategy

**Decision**: Hybrid — reverse-engineer fomo's internal HTTP JSON endpoints
for all data reads, replaying the consumer's authenticated Privy session; use
a headless browser (Playwright) **only** to complete the Privy login flow and
capture the resulting session token.

**Rationale**: Because the app is a SPA with no SSR data, every leaderboard,
profile, and feed value is served by internal JSON endpoints the browser calls
after login. Replaying those endpoints with the session token is cheap, fast,
and cacheable — far better than rendering pages. However, Privy's OAuth +
wallet/social login cannot be reproduced with plain HTTP requests, so a
headless browser is used once (per login) to run the OAuth flow and harvest the
session cookie/bearer. Subsequent reads are lightweight httpx calls.

**Alternatives considered**:
- Full headless-browser scraping for every request: rejected — heavy, slow,
  brittle, and wasteful; violates the freshness success criteria (SC-002).
- Pure HTTP including the login step: rejected — Privy OAuth/wallet flow is
  impractical to replay without a browser.

**Risk**: fomo's internal endpoints are undocumented and may change. The
client layer MUST be isolated behind a single adapter so endpoint changes
require edits in one place (see Constitution Check, Principle V). A
health/error path (FR-007) MUST surface upstream breakage clearly rather than
fabricate data.

## Decision 3 — Real-Time Alert Delivery

**Decision**: Two-tier. **Upstream**: poll fomo's activity/alert endpoints per
tracked trader at a short interval (≈5–15s) using the relevant session. 
**Downstream to consumers**: Server-Sent Events (SSE) for one-way alert push,
fanned out via a Redis pub/sub channel that decouples ingestion from
consumer streams.

**Rationale**: Polling is the simplest reliable upstream mechanism given
fomo's WebSocket protocol is unknown and undocumented; it satisfies the
<5s alert-latency target (SC-003) with a short poll cadence. SSE is simpler
than WebSocket for one-way alerts and is HTTP-native (no extra protocol,
trivial reconnection). Redis pub/sub lets one ingestion worker fan events out
to many consumers and survive consumer reconnects. This is the YAGNI choice
(Principle V).

**Alternatives considered**:
- Intercept fomo's own WebSocket: potentially lower latency, but the
  protocol is unknown and reverse-engineering it is higher risk. **Deferred**
  — revisit only if polling cannot meet SC-003.
- WebSocket to consumers (bidirectional): overkill for one-way alerts.
  Rejected.

## Decision 4 — Authentication / Session Handling

**Decision**: A consumer completes a guided Privy login via the API (a
headless browser drives the OAuth/wallet-or-social flow) and receives a
short-lived consumer API key that maps to the captured fomo session token.
The session token is held **only** in volatile, encrypted-in-transit store
(Redis with a short TTL tied to the consumer's active use) — never persisted
to disk or a durable database, and never the consumer's wallet private key
(Privy performs signing; we keep only the resulting session token). This
satisfies FR-013 (consumer's own credentials, no central persistence beyond
the active session).

**Rationale**: Privy's login can't be replayed with raw HTTP (Decision 2), so
we harvest the resulting session token. Storing only that token — not the
wallet/key — and keeping it volatile satisfies the no-persistence rule. A
short-lived consumer API key avoids passing the raw fomo token on every call
and allows revocation on disconnect.

**Alternatives considered**:
- Ask the consumer to paste a raw fomo cookie/token: simplest, but fragile
  UX and the token expires. Offered as an optional fallback for advanced
  users, but the guided login is the primary path.
- Full replay of Privy OAuth over HTTP: impractical (see Decision 2).

## Decision 5 — Storage

**Decision**: Redis (volatile only) — session-token cache (short TTL),
rate-limit counters, and pub/sub for alert fan-out. **No persistent
database**: the API is a read-only proxy and must not store fomo data or
consumer credentials durably (FR-007, FR-013).

**Rationale**: All data is proxied live from fomo; caching is for freshness
and rate-limit relief only, and must be evictable. A persistent DB would
contradict the read-only, no-data-retention posture.

## Decision 6 — Testing

**Decision**: pytest + pytest-asyncio for unit/contract/integration tests;
httpx `AsyncClient` for endpoint tests; **respx** to mock the fomo upstream
so contract tests assert our API's shape without hitting fomo; Playwright
pytest fixture for the auth-flow path (mocked Privy in tests).

**Rationale**: Contract tests pin our public API shape against a mocked
upstream (Constitution Principle III: independent testability per story).
respx lets tests be deterministic and offline.

## Resolved NEEDS CLARIFICATION Summary

| Item | Resolution | See |
|------|-----------|-----|
| Language/Version | Python 3.12 | Decision 1 |
| Primary dependencies | FastAPI, httpx, Uvicorn, Pydantic, Playwright, Redis | Decisions 1,2,5 |
| Data acquisition | Reverse-engineered HTTP endpoints + Playwright login | Decision 2 |
| Real-time alerts | Upstream polling + SSE downstream + Redis pub/sub | Decision 3 |
| Auth/session | Guided Privy login; volatile session store; consumer API key | Decision 4 |
| Storage | Redis (volatile) only; no persistent DB | Decision 5 |
| Testing | pytest + pytest-asyncio + httpx + respx | Decision 6 |

All unknowns are resolved. No blocking clarifications remain.

## Addendum: Confirmed vs Unverified Upstream Surface (2026-07-24)

Static reverse-engineering of the fomo.family SPA bundles (`fomoFetch-BjKROXCg.js`,
`chains-B91MAH-X.js`, profile route chunk) established the following. CONFIRMED
items are verifiable from public static analysis; UNVERIFIED items are
best-guess defaults that require capture from an authenticated session and are
overridable via env vars (see `config.py`). The client never fabricates data
(FR-007): shape mismatches raise `UpstreamChangedError`.

| Item | Status | Evidence / Note |
|------|--------|-----------------|
| Data API base `https://prod-api.fomo.family` | **CONFIRMED** | `fomoFetch`: `function pt(){return"https://prod-api.fomo.family"}` |
| SPA base `https://fomo.family` (login only) | **CONFIRMED** | React Router SPA; Privy `/token` OAuth callback route |
| `Authorization: Bearer <privy_access_token>` | **CONFIRMED** | `fomoFetch` sets `Authorization: Bearer <i>` where `i` = Privy `getAccessToken()` |
| `X-Supported-Chains` header (comma-joined chain ids) | **CONFIRMED** | `chains-B91MAH-X.js` `ne()` = `f().join(",")`; default `56,143,4663,8453,1399811149` (eth `1` gated behind `eth_mainnet` flag) |
| `Content-Type: application/json` request header | **CONFIRMED** | `fomoFetch` default headers |
| `GET /profile/{trader_id}` | **CONFIRMED** | profile route chunk |
| `GET /leaderboard` | UNVERIFIED | best-guess; served from post-auth lazy chunk |
| `GET /profile/{trader_id}/activity` | UNVERIFIED | best-guess feed path |
| `GET /alerts` (per-trader) | UNVERIFIED | best-guess |
| `GET /tokens/trending` | UNVERIFIED | added for trading-bot signals |
| `GET /profile/{trader_id}/positions` | UNVERIFIED | added for open-position tracking |
| `GET /profile/{trader_id}/stats` | UNVERIFIED | added for PnL/win-rate stats |

**Wiring status**: `config.py` splits `upstream_base` (prod-api) from
`upstream_app_base` (SPA); each endpoint path is a settings field. `fomo_client.py`
reads all paths from settings and sends the confirmed headers. `privy_login.py`
and `auth.py` document the confirmed Privy-OAuth → Bearer-token flow and use the
SPA base for login. `conftest.py` derives its respx mock base + paths from
settings so tests stay in sync with any override.

**To go live**: complete the Privy login against a real account, capture the
UNVERIFIED paths from DevTools → Network (requests to `prod-api.fomo.family`),
and set the corresponding `FOMO_API_UPSTREAM_*_PATH` env vars.
