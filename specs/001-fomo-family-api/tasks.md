---

description: "Task list for the Fomo Family API feature"
---

# Tasks: Fomo Family API

**Input**: Design documents from `/specs/001-fomo-family-api/` — plan.md,
spec.md, research.md, data-model.md, contracts/rest-api.md,
contracts/alerts-sse.md, quickstart.md.

**Prerequisites**: plan.md (required), spec.md (required), research.md,
data-model.md, contracts/ (all present).

**Tests**: Included. The approved plan (research.md Decision 6) committed to
pytest + pytest-asyncio + httpx + respx, and quickstart.md defines validation
scenarios. Per constitution Principle III, tests for each story are written
FIRST and must FAIL before implementation (Red-Green-Refactor).

**Organization**: Tasks are grouped by user story so each story can be
implemented, tested, and validated independently. US1 (P1) is the MVP.

## Format: `[ID] [P?] [Story?] Description`

- **[P]**: Can run in parallel (different files, no dependencies on
  incomplete tasks)
- **[Story]**: User story this task belongs to (US1, US2, US3) — required
  only on user-story phase tasks
- Exact file paths included in every task description

## Path Conventions

- Project root layout: `api/src/fomo_api/...` (application),
  `api/tests/...` (tests), `api/pyproject.toml` (deps), `api/Dockerfile`.
- See plan.md "Project Structure" for the full tree.

## Tech Stack (from plan.md / research.md)

Python 3.12 · FastAPI · httpx · Uvicorn · Pydantic v2 · Playwright (Privy
login) · Redis (volatile sessions, rate limits, pub/sub) · pytest +
pytest-asyncio + respx.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [X] T001 Create project structure per implementation plan (api/src/fomo_api/
  with subdirs auth, clients, models, services, api, realtime; api/tests/
  with subdirs contract, integration, unit) in api/
- [X] T002 Initialize Python 3.12 project with FastAPI, httpx, uvicorn,
  pydantic v2, playwright, redis (asyncio) dependencies in api/pyproject.toml
- [X] T003 [P] Configure linting (ruff), formatting, and type checking
  (mypy) in api/pyproject.toml
- [X] T004 [P] Configure pytest + pytest-asyncio + respx dev dependencies in
  api/pyproject.toml
- [X] T005 [P] Create env-driven settings module (Redis URL, fomo upstream
  base URL, per-consumer rate limits, session TTL, poll cadence) in
  api/src/fomo_api/config.py

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story
can be implemented.

**CRITICAL**: No user story work can begin until this phase is complete.

- [X] T006 [P] Implement shared response envelope + Page pagination model +
  freshness (last_refreshed_at) wrapper in api/src/fomo_api/models/envelope.py
- [X] T007 [P] Implement error envelope + stable error codes (UNAUTHORIZED,
  RATE_LIMITED, NOT_FOUND, VALIDATION_ERROR, UPSTREAM_UNAVAILABLE, STALE_DATA,
  UPSTREAM_CHANGED) + exception classes + FastAPI exception handlers in
  api/src/fomo_api/api/errors.py
- [X] T008 [P] Implement per-consumer rate-limit middleware (Redis-backed
  counters, 429 RATE_LIMITED on breach) in api/src/fomo_api/api/rate_limit.py
- [X] T009 [P] Implement consumer API key issue/verify + volatile session
  store (Redis, short TTL, no disk persistence — FR-013) in
  api/src/fomo_api/auth/session.py
- [X] T010 Implement Privy login flow (Playwright headless browser drives
  OAuth/wallet-social login, harvests fomo session token) in
  api/src/fomo_api/auth/privy_login.py
- [X] T011 Implement fomo upstream client adapter base (httpx AsyncClient,
  consumer-session injection, pagination handling, last_refreshed tracking,
  UPSTREAM_CHANGED shape-change detection, never-fabricate policy — FR-007) in
  api/src/fomo_api/clients/fomo_client.py
- [X] T012 [P] Create FastAPI app factory + route wiring + middleware
  registration (rate limit, error handlers, /v1 router prefix) in
  api/src/fomo_api/main.py
- [X] T013 [P] Configure Dockerfile + docker-compose (api service + redis) in
  api/
- [X] T014 Foundational test harness: respx mock fomo upstream + async client
  fixtures + env overrides in api/tests/conftest.py

**Checkpoint**: Foundation ready — envelope, errors, rate limiting, auth
session, upstream adapter, app factory, and test harness all in place. User
story implementation can now begin in parallel/priority order.

---

## Phase 3: User Story 1 - Leaderboard & Trader Profiles (Priority: P1) MVP

**Goal**: A consumer can retrieve the ranked fomo.family leaderboard and
resolve any listed trader's profile using only the API (spec US1, SC-001).

**Independent Test**: Query the leaderboard → ranked traders; resolve a
chosen trader's profile by id → public stats. No other story required.

### Tests for User Story 1 (write FIRST, must FAIL)

> Per constitution Principle III: tests written first, must fail before
> implementation.

- [X] T015 [P] [US1] Contract test for GET /v1/leaderboard (ranked list,
  pagination, last_refreshed_at) with respx-mocked fomo upstream in
  api/tests/contract/test_leaderboard.py
- [X] T016 [P] [US1] Contract test for GET /v1/traders/{trader_id} (profile +
  metrics, null for missing fomo fields — FR-007) with respx-mocked upstream
  in api/tests/contract/test_trader_profile.py
- [X] T017 [P] [US1] Contract test for POST /v1/auth/login + /v1/auth/logout
  (consumer key issue/revoke) in api/tests/contract/test_auth.py

### Implementation for User Story 1

- [X] T018 [P] [US1] Create Trader + TraderMetrics Pydantic models in
  api/src/fomo_api/models/trader.py
- [X] T019 [P] [US1] Create Leaderboard model (Trader[] + Page) in
  api/src/fomo_api/models/leaderboard.py
- [X] T020 [US1] Implement FomoClient.get_leaderboard + get_trader_profile
  methods (map fomo internal endpoints → Trader/Leaderboard shapes) in
  api/src/fomo_api/clients/fomo_client.py
- [X] T021 [US1] Implement leaderboard service (pagination, freshness,
  upstream-unavailable → 502/503) in api/src/fomo_api/services/leaderboard_service.py
- [X] T022 [US1] Implement trader profile service (lookup, 404 on
  missing/delisted) in api/src/fomo_api/services/trader_service.py
- [X] T023 [US1] Implement GET /v1/leaderboard route in
  api/src/fomo_api/api/leaderboard.py
- [X] T024 [US1] Implement GET /v1/traders/{trader_id} route in
  api/src/fomo_api/api/traders.py
- [X] T025 [US1] Implement POST /v1/auth/login + /v1/auth/logout routes
  (guided Privy login → consumer key; revoke drops session — FR-013) in
  api/src/fomo_api/api/auth.py

**Checkpoint**: User Story 1 fully functional and testable independently.
MVP delivered — leaderboard + profiles + auth reachable via API with no
manual website use (SC-001). STOP and validate via quickstart Scenario 1.

---

## Phase 4: User Story 2 - Trader Activity Feed (Priority: P2)

**Goal**: A consumer can retrieve a tracked trader's recent activity
(actions, tokens, timestamps) with time-window/chain/token filtering (spec
US2, FR-010).

**Independent Test**: Given a trader id, request the activity feed →
time-ordered actions honoring filters. Works standalone for monitoring a
known trader.

### Tests for User Story 2 (write FIRST, must FAIL)

- [X] T026 [P] [US2] Contract test for GET /v1/traders/{trader_id}/activity
  (time-ordered list, from/to/chain/token_id filters, empty result for
  inactive trader — US2 acceptance 3, 422 on bad from/to range) with
  respx-mocked upstream in api/tests/contract/test_activity.py

### Implementation for User Story 2

- [X] T027 [P] [US2] Create TraderActivity + TokenRef Pydantic models
  (action enum buy/sell, RFC 3339 timestamp validation) in
  api/src/fomo_api/models/activity.py
- [X] T028 [P] [US2] Create Token Pydantic model (price/volume/liquidity/
  market_cap all nullable — FR-007) in api/src/fomo_api/models/token.py
- [X] T029 [US2] Implement FomoClient.get_trader_activity method (with
  upstream filter pass-through) in api/src/fomo_api/clients/fomo_client.py
- [X] T030 [US2] Implement activity service (time-window/chain/token_id
  filtering + pagination, 404 on unknown trader) in
  api/src/fomo_api/services/activity_service.py
- [X] T031 [US2] Implement GET /v1/traders/{trader_id}/activity route in
  api/src/fomo_api/api/traders.py

**Checkpoint**: User Stories 1 AND 2 both work independently. Validate via
quickstart Scenario 2.

---

## Phase 5: User Story 3 - Real-Time Top-Trader Alerts (Priority: P3)

**Goal**: A consumer receives alert events when tracked traders make
qualifying buys, with no false positives and delivery stopping on
unsubscribe (spec US3, SC-003). Alerts via SSE; upstream via polling; fan-out
via Redis pub/sub (research.md Decision 3).

**Independent Test**: Subscribe to a trader, open the SSE stream, receive an
alert on a qualifying buy; unsubscribe → no further alerts. Functions for a
consumer who already knows the trader ids.

### Tests for User Story 3 (write FIRST, must FAIL)

- [X] T032 [P] [US3] Contract test for POST /v1/alerts/subscriptions +
  DELETE /v1/alerts/subscriptions/{id} (subscribe/unsubscribe) in
  api/tests/contract/test_alert_subscriptions.py
- [X] T033 [P] [US3] Contract test for GET /v1/alerts/stream SSE (alert
  event on qualifying buy, no spurious alerts — US3 acceptance 2, events
  stop after unsubscribe — acceptance 3) in
  api/tests/contract/test_alert_stream.py

### Implementation for User Story 3

- [X] T034 [P] [US3] Create Alert Pydantic model (id, trader_id, TokenRef,
  amount_usd nullable, RFC 3339 timestamp) in api/src/fomo_api/models/alert.py
- [X] T035 [P] [US3] Implement Redis pub/sub alert channel + publisher in
  api/src/fomo_api/realtime/pubsub.py
- [X] T036 [US3] Implement upstream poller (per-trader watermark,
  qualifying-buy detection, publishes to pub/sub) in
  api/src/fomo_api/realtime/poller.py
- [X] T037 [US3] Implement SSE manager (subscribed/heartbeat/alert/error
  events, dedup by Alert.id, dead-consumer detection) in
  api/src/fomo_api/realtime/sse.py
- [X] T038 [US3] Implement alert subscription store (tracked trader_ids per
  consumer/subscription) in api/src/fomo_api/services/alert_subscription_service.py
- [X] T039 [US3] Implement POST + DELETE /v1/alerts/subscriptions routes in
  api/src/fomo_api/api/alerts.py
- [X] T040 [US3] Implement GET /v1/alerts/stream SSE route in
  api/src/fomo_api/api/alerts.py

**Checkpoint**: All three user stories independently functional. Validate
via quickstart Scenario 3.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Improvements affecting multiple user stories and quality gates
(constitution Principle V, plan.md Quality Gates).

- [X] T041 [P] API README (setup, env vars, quickstart link, ToS-compliance
  notice) in api/README.md
- [X] T042 [P] OpenAPI polish + interactive docs (request/response schemas
  from contracts) wiring in api/src/fomo_api/main.py
- [X] T043 [P] Integration test for graceful degradation (upstream 5xx →
  502 UPSTREAM_UNAVAILABLE / 503 STALE_DATA, never fabricated 200 — FR-007) in
  api/tests/integration/test_degradation.py
- [X] T044 [P] Integration test for rate limiting (429 RATE_LIMITED on
  per-consumer breach — FR-008) in api/tests/integration/test_rate_limit.py
- [X] T045 [P] Additional unit tests for pagination bounds (page_size 1–100),
  envelope freshness, and error-code mapping in api/tests/unit/test_models.py
- [X] T046 [P] Structured logging + UPSTREAM_CHANGED detection audit across
  services (clear non-misleading errors — FR-009) in api/src/fomo_api/
- [X] T047 [P] Security hardening: no-credential-persistence audit + session
  TTL eviction verification (FR-013) in api/src/fomo_api/auth/session.py
- [X] T048 Run quickstart.md validation (Scenarios 1–4) end-to-end against a
  local server with respx-mocked fomo upstream in api/

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup completion — BLOCKS all user
  stories. T014 (test harness) must precede the per-story contract tests.
- **User Story 1 (Phase 3)**: Depends on Phase 2. MVP — deliver and validate
  first.
- **User Story 2 (Phase 4)**: Depends on Phase 2. May reference US1's trader
  id resolution but is independently testable with a known id.
- **User Story 3 (Phase 5)**: Depends on Phase 2; reuses US2's
  FomoClient.get_trader_activity (T029) for poller watermarking, but is
  independently testable with a known trader id.
- **Polish (Phase 6)**: Depends on all desired user stories being complete.

### User Story Dependencies

- **US1 (P1)**: Starts after Phase 2. No dependency on other stories.
- **US2 (P2)**: Starts after Phase 2. Independently testable; may use US1's
  trader profile to discover ids but needs only a known id to validate.
- **US3 (P3)**: Starts after Phase 2. The upstream poller (T036) reuses
  US2's activity client method (T029) for watermark tracking; independently
  testable with a pre-known trader id.

### Within Each User Story

- Tests written FIRST and MUST fail before implementation (constitution
  Principle III).
- Models before services.
- Services before routes/endpoints.
- Core implementation before integration.
- Story complete (checkpoint) before moving to the next priority.

### Parallel Opportunities

- All Setup tasks marked [P] can run in parallel (T003, T004, T005).
- All Foundational tasks marked [P] can run in parallel within Phase 2
  (T006, T007, T008, T009, T012, T013) — T010, T011, T014 are sequential
  anchors.
- Within US1: T015/T016/T017 contract tests parallel; T018/T019 models
  parallel; T021/T022 services sequential after client; T023/T024/T025
  routes sequential after services.
- Within US2: T027/T028 models parallel.
- Within US3: T034/T035 (model + pubsub) parallel; T032/T033 contract tests
  parallel.
- All Polish tasks marked [P] can run in parallel (T041–T047).
- Different user stories may proceed in parallel by different developers
  once Phase 2 completes.

---

## Parallel Example: User Story 1

```bash
# Launch all US1 contract tests together (write first, expect failures):
Task: "T015 [P] [US1] Contract test for GET /v1/leaderboard in api/tests/contract/test_leaderboard.py"
Task: "T016 [P] [US1] Contract test for GET /v1/traders/{trader_id} in api/tests/contract/test_trader_profile.py"
Task: "T017 [P] [US1] Contract test for POST /v1/auth/login + /v1/auth/logout in api/tests/contract/test_auth.py"

# Launch all US1 models together:
Task: "T018 [P] [US1] Create Trader + TraderMetrics models in api/src/fomo_api/models/trader.py"
Task: "T019 [P] [US1] Create Leaderboard model in api/src/fomo_api/models/leaderboard.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1 (write tests first → implement → green)
4. **STOP and VALIDATE**: quickstart Scenario 1 (leaderboard + profile)
5. Deploy/demo MVP if ready

### Incremental Delivery

1. Setup + Foundational → Foundation ready
2. Add US1 → validate Scenario 1 → Deploy/Demo (MVP!)
3. Add US2 → validate Scenario 2 → Deploy/Demo
4. Add US3 → validate Scenario 3 → Deploy/Demo
5. Polish (Phase 6) → validate Scenario 4 (degradation) → release

### Parallel Team Strategy

With multiple developers after Phase 2:

1. Team completes Setup + Foundational together
2. Once Foundational done:
   - Developer A: User Story 1
   - Developer B: User Story 2 (needs only a known trader id)
   - Developer C: User Story 3 (reuses US2 client; stub for T029 if US2 lags)
3. Stories complete and integrate independently

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks.
- [Story] label maps task to its user story for traceability.
- Each user story is independently completable and testable.
- Verify tests fail before implementing (Red-Green-Refactor).
- Commit after each task or logical group.
- Stop at any checkpoint to validate a story independently.
- Avoid: vague tasks, same-file conflicts, cross-story dependencies that
  break independence.
- The fomo upstream is undocumented and may change; all access goes through
  the single `fomo_client.py` adapter so changes are contained (constitution
  Principle V, plan.md Complexity Tracking).
- Strictly read-only: no trades/account actions anywhere (FR-012); no
  credential persistence beyond the active session (FR-013).
