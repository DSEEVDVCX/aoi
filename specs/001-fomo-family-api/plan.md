# Implementation Plan: Fomo Family API

**Branch**: `001-fomo-family-api` | **Date**: 2026-07-23 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-fomo-family-api/spec.md`

**Note**: This plan was filled in by the `/speckit.plan` command. Phase 0
research is in [research.md](research.md); all Technical Context unknowns
were resolved there.

## Summary

Build an unofficial, **read-only** programmatic API for fomo.family (a
social crypto-trading platform that provides no official API). Each consumer
authenticates with their own fomo account via a guided Privy login; the API
replays that session against fomo's reverse-engineered internal endpoints to
expose the leaderboard, trader profiles, trader activity feeds, and real-time
top-trader buy alerts over a clean REST + SSE surface. MVP (P1) is the
leaderboard and trader profiles; P2 adds the activity feed; P3 adds real-time
alerts.

## Technical Context

> Phase 0 research ([research.md](research.md)) resolved every item below.
> fomo.family is a React Router v7 SPA using **Privy** auth and React Query;
> its data is served by internal JSON endpoints the browser calls after login.

**Language/Version**: Python 3.12 *(research.md Decision 1)*

**Primary Dependencies**: FastAPI (API framework), httpx (async HTTP client
to fomo upstream), Uvicorn (ASGI server), Pydantic v2 (models/validation),
Playwright (Privy login flow / headless browser), Redis (volatile session
cache, rate-limit counters, pub/sub fan-out) *(research.md Decisions 1, 2, 5)*

**Storage**: Redis (volatile only) — session-token cache (short TTL),
rate-limit counters, pub/sub for alerts. **No persistent database**: the
service is a read-only proxy and stores neither fomo data nor consumer
credentials durably (FR-007, FR-013) *(research.md Decision 5)*

**Testing**: pytest + pytest-asyncio; httpx `AsyncClient` for endpoint tests;
respx to mock the fomo upstream (deterministic, offline contract tests);
Playwright pytest fixture for the auth path *(research.md Decision 6)*

**Target Platform**: Linux server, containerized (Docker). Standard ASGI
deployment behind a reverse proxy.

**Project Type**: web-service (REST JSON API + Server-Sent Events for
real-time alerts)

**Performance Goals**: 50 concurrent consumers on read endpoints without
freshness degradation (SC-004); alert delivery latency <5s for ≥90% of alerts
(SC-003); leaderboard/profile freshness within 60s of fomo for ≥95% of
requests (SC-002); first successful authenticated request within 10 minutes
for ≥90% of new consumers (SC-005).

**Constraints**: Strictly read-only — no trades or account actions (FR-012);
respect fomo.family Terms of Service and Privacy Policy; enforce per-consumer
rate limits (FR-008); no credential persistence beyond the active session
(FR-013); never fabricate data on upstream failure (FR-007); surface a
last-refreshed timestamp (FR-011); clear, actionable errors (FR-009).

**Scale/Scope**: 3 independently-testable user stories. MVP = P1 (leaderboard
+ profiles). ~1 service (single Python app) + Redis. Internal fomo endpoint
shape is undocumented and may change — isolated behind one adapter module.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Gates derived from `.specify/memory/constitution.md` (v1.0.0):

| Principle | Gate | Status |
|-----------|------|--------|
| I. Spec-First (NON-NEGOTIABLE) | Approved `spec.md` exists before planning | ✅ Pass |
| II. Plan-Before-Code | This plan + Constitution Check gate precede code | ✅ Pass |
| III. Independent Testability (NON-NEGOTIABLE) | P1/P2/P3 are independently testable; contract tests pin API shape per story | ✅ Pass |
| IV. Incremental MVP Delivery | P1 = MVP (leaderboard+profiles); P2, P3 increment on it; checkpoints validate each | ✅ Pass |
| V. Constitution Compliance & Simplicity | Polling+SSE chosen over WebSocket (YAGNI); single Python service; one upstream adapter isolates reverse-engineering risk | ✅ Pass (see Complexity Tracking) |

**Post-Phase-1 re-check**: data model, contracts, and quickstart align with
the spec's entities (Trader, Activity, Token, Alert, Leaderboard) and the
resolved technical context. No new violations introduced. **Gate: PASS.**

## Project Structure

### Documentation (this feature)

```text
specs/001-fomo-family-api/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
│   ├── rest-api.md      # REST endpoint contracts
│   └── alerts-sse.md    # SSE alert stream contract
└── tasks.md             # Phase 2 output (/speckit.tasks - NOT created here)
```

### Source Code (repository root)

```text
api/
├── src/
│   └── fomo_api/
│       ├── __init__.py
│       ├── main.py            # FastAPI app factory + route wiring
│       ├── config.py          # settings (env-driven)
│       ├── auth/              # Privy login flow (Playwright) + session store
│       │   ├── privy_login.py
│       │   └── session.py     # volatile session store (Redis, short TTL)
│       ├── clients/
│       │   └── fomo_client.py # single adapter to fomo's reverse-engineered endpoints
│       ├── models/            # Pydantic schemas (see data-model.md)
│       ├── services/          # leaderboard, profile, activity, alerts logic
│       ├── api/               # REST route handlers
│       └── realtime/          # poller + Redis pub/sub + SSE manager
├── tests/
│   ├── contract/             # API-shape contract tests (respx-mocked upstream)
│   ├── integration/          # end-to-end through service with mocked fomo
│   └── unit/                 # pure logic / models / pagination
├── pyproject.toml
└── README.md
```

**Structure Decision**: Single-project web service (`api/`) with a clear
`src/` package layout. The fomo upstream is reachable only through
`clients/fomo_client.py` (one adapter) so undocumented endpoint changes are
contained. `auth/` isolates Privy login and volatile session handling;
`realtime/` isolates the polling + pub/sub + SSE machinery so P3 stays
independent. Tests mirror `src/` by layer (contract / integration / unit).

## Complexity Tracking

> Filled because Principle V requires justifying the one deviation from the
> simplest possible approach.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Headless browser (Playwright) for the login flow only | Privy uses OAuth + wallet/social login that cannot be reproduced with plain HTTP; a browser is required to complete login and harvest the session token | Pure-HTTP login rejected because Privy's OAuth/wallet flow is impractical to replay without a browser. Browser scope is limited to the login step; all data reads use lightweight httpx calls. |
| Redis (session cache + rate limits + pub/sub) | Volatile session storage (FR-013), per-consumer rate limiting (FR-008), and alert fan-out (P3) each need shared ephemeral state across workers | In-memory-only state rejected because it cannot be shared across Uvicorn workers or survive a worker restart, and cannot fan out alerts to many consumers. A persistent DB rejected because the read-only/no-retention posture forbids durable storage (FR-007/FR-013). |
