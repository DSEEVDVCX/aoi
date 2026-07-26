# Quickstart: Fomo Family API

**Feature**: 001-fomo-family-api
**Date**: 2026-07-23
**Status**: Phase 1 design output

This is a validation/run guide — it proves the feature works end-to-end. It
references the contracts and data model rather than duplicating them.
Implementation bodies belong in `tasks.md`, not here.

## Prerequisites

- Python 3.12 installed.
- Redis available locally (e.g., `redis-server` or `docker run -p 6379:6379 redis`).
- Playwright browsers installed (`playwright install chromium`).
- A valid fomo.family account (the consumer's own account) for the guided
  Privy login. **Before any real use, confirm compliance with fomo.family's
  Terms of Service and Privacy Policy** (spec Assumptions / FR-008).
- A fomo.family test/observation account is recommended for validation to
  avoid acting on a primary trading account.

## Setup

```bash
# From repo root
cd api
python -m venv .venv && . .venv/Scripts/activate    # Windows
pip install -e ".[dev]"
playwright install chromium
# Configure environment
export FOMO_API_REDIS_URL=redis://localhost:6379/0
export FOMO_API_UPSTREAM_BASE=https://fomo.family
uvicorn fomo_api.main:app --reload --port 8000
```

The server runs at `http://localhost:8000`; interactive docs at `/docs`.

## Validation Scenarios

These scenarios map to the spec's user stories and success criteria. Run them
against a local server with a mocked fomo upstream for deterministic
validation (see Testing notes).

### Scenario 1 — MVP: Leaderboard & Trader Profile (US1, P1)

Proves SC-001: a consumer retrieves the leaderboard and resolves a profile
using only the API.

1. Authenticate (guided Privy login) and obtain a consumer key:
   ```
   POST /v1/auth/login  ->  { "consumer_key": "...", "expires_at": "..." }
   ```
2. Fetch the leaderboard (see rest-api.md):
   ```
   GET /v1/leaderboard?page=1&page_size=20
   Authorization: Bearer <consumer_key>
   ```
   **Expected**: `200` with `data` (ranked Trader list) and `last_refreshed_at`
   (FR-011). Page metadata present (FR-006).
3. Pick the first trader's `id` and resolve its profile:
   ```
   GET /v1/traders/<trader_id>
   ```
   **Expected**: `200` with that Trader's public profile + metrics (only fields
   fomo exposes; missing metrics are `null`, never fabricated — FR-007).
4. **Pass**: leaderboard + profile both succeed with no manual website use.

### Scenario 2 — Trader Activity Feed (US2, P2)

1. Using a known `trader_id` (from Scenario 1 or supplied), fetch activity:
   ```
   GET /v1/traders/<trader_id>/activity?from=2026-07-23T00:00:00Z&to=2026-07-23T23:59:59Z
   ```
   **Expected**: `200` with a time-ordered list of actions (action, token,
   timestamp, chain); time-window filter honored (FR-010).
2. Request a trader with no recent activity.
   **Expected**: `200` with `data: []` (US2 acceptance 3 — no error).
3. **Pass**: feed returns ordered, filtered activity.

### Scenario 3 — Real-Time Alerts (US3, P3)

1. Subscribe to alerts for a trader:
   ```
   POST /v1/alerts/subscriptions  { "trader_ids": ["<trader_id>"] }
   -> { "subscription_id": "...", "trader_ids": ["<trader_id>"] }
   ```
2. Open the SSE stream:
   ```
   GET /v1/alerts/stream   (Accept: text/event-stream)
   Authorization: Bearer <consumer_key>
   ```
   **Expected**: a `subscribed` event confirming the tracked set, then periodic
   `heartbeat` events.
3. Trigger (or simulate, via mocked upstream) a qualifying buy by the tracked
   trader.
   **Expected**: an `alert` event arrives with the trader, token, and timestamp
   (US3 acceptance 1); no spurious alerts otherwise (acceptance 2).
4. Unsubscribe and confirm no further events:
   ```
   DELETE /v1/alerts/subscriptions/<subscription_id>
   ```
   **Expected**: `204`; no further `alert` events on a subsequent stream
   (acceptance 3).
5. **Pass**: alerts arrive within the latency target (SC-003) with no false
   positives and stop after unsubscribe.

### Scenario 4 — Graceful Degradation (spec Edge Cases)

1. With the mocked fomo upstream returning 5xx / unreachable:
   ```
   GET /v1/leaderboard
   ```
   **Expected**: `502 UPSTREAM_UNAVAILABLE` (or `503 STALE_DATA`), never a
   fabricated `200` (FR-007).
2. Exceed the per-consumer rate limit rapidly.
   **Expected**: `429 RATE_LIMITED` (FR-008/FR-009).
3. **Pass**: failures are clear and non-misleading.

## Testing Notes

- Contract tests (see data-model.md / rest-api.md) pin the API shape using
  **respx** to mock fomo upstream responses — deterministic and offline.
- Scenario 4 uses respx to simulate upstream failure without a live fomo
  connection.
- A live end-to-end run against real fomo.family is **optional** and only
  performed with the consumer's own test account after ToS confirmation.

## Expected Outcome

The feature is validated when Scenarios 1–4 pass: the MVP (leaderboard +
profiles) works standalone, the activity feed filters correctly, alerts
arrive in real time without false positives and stop on unsubscribe, and
upstream failures surface as clear errors rather than fabricated data.
