# Feature Specification: Fomo Family API

**Feature Branch**: `001-fomo-family-api`

**Created**: 2026-07-23

**Status**: Draft

**Input**: User description: "اريد انشاء api لهذا الموقع لانه لا يوفره fomo.family" (I want to create an API for this site because it does not provide one — fomo.family)

## User Scenarios & Testing *(mandatory)*

<!--
  IMPORTANT: User stories are prioritized as user journeys ordered by importance.
  Each user story is INDEPENDENTLY TESTABLE - implementing just ONE yields a viable MVP.
-->

### User Story 1 - Leaderboard & Trader Profiles (Priority: P1)

As a developer building crypto-social analytics or dashboards, I want to retrieve the fomo.family leaderboard and individual trader profiles so that I can identify top traders and their performance metrics programmatically, without using the website by hand.

**Why this priority**: The leaderboard and trader profiles are the foundational, publicly-marketed social data of fomo.family ("become a legend, top the leaderboard", "discover and follow top traders"). Every higher-level feature — activity feeds, alerts, and any copy/signal logic — depends on being able to resolve and rank traders. This story alone yields a complete "top traders" view and is independently valuable.

**Independent Test**: Query the leaderboard and receive a ranked list of traders with their stats; resolve a chosen trader's profile by identifier and receive their public stats. No other story is required for this to deliver value.

**Acceptance Scenarios**:

1. **Given** the consumer calls the leaderboard endpoint, **When** no filters are supplied, **Then** a ranked list of top traders is returned, each with a handle, rank, and core performance metrics.
2. **Given** the consumer has a trader's identifier, **When** they request that trader's profile, **Then** the trader's public profile (handle, rank, followers, performance metrics) is returned.
3. **Given** the leaderboard supports pagination, **When** the consumer requests the next page, **Then** a consistent, contiguous page of ranked traders is returned.

---

### User Story 2 - Trader Activity Feed (Priority: P2)

As a developer, I want to retrieve a specific trader's recent activity (their buys/sells and the tokens involved) so that I can analyze or display what top traders are doing over time.

**Why this priority**: Builds directly on P1's trader identity to expose the "follow top traders" social feed. Independently valuable for activity-monitoring and analytics use cases that already know which trader to watch.

**Independent Test**: Given a trader identifier (from P1 or known in advance), request the activity feed and receive a time-ordered list of that trader's recent actions with token and timestamp. Works standalone for monitoring a known trader.

**Acceptance Scenarios**:

1. **Given** a valid trader identifier, **When** the consumer requests the activity feed, **Then** a time-ordered list of the trader's recent actions (action type, token, amount where available, timestamp) is returned.
2. **Given** the consumer wants only recent activity, **When** a time-window filter is supplied, **Then** only actions within that window are returned.
3. **Given** a trader with no recent activity, **When** their feed is requested, **Then** an empty result is returned without error.

---

### User Story 3 - Real-Time Top-Trader Alerts (Priority: P3)

As a developer building automation or notification tools, I want to receive alerts when tracked top traders make notable buys so that I can react to, or surface, "what the best are buying" in real time.

**Why this priority**: "Real time notifications for what the best are buying" is a headline fomo.family feature and the highest-engagement signal, but it depends on trader identity (P1) and is richer with activity context (P2). It is independently valuable for alerting/bot use cases once the consumer knows which traders to track.

**Independent Test**: Subscribe to alerts for one or more traders and receive an alert event when a tracked trader makes a qualifying buy. Functions without the dashboard views of P1/P2 for a consumer who already knows the trader identifiers.

**Acceptance Scenarios**:

1. **Given** the consumer subscribes to alerts for a set of traders, **When** a tracked trader makes a qualifying buy, **Then** an alert event is delivered containing the trader, token, and timestamp.
2. **Given** the consumer is subscribed, **When** no qualifying activity occurs, **Then** no spurious alerts are delivered (no false positives).
3. **Given** the consumer unsubscribes, **When** subsequent activity occurs, **Then** no further alerts are delivered to that consumer.

---

### Edge Cases

- What happens when fomo.family data is unavailable (site down, login wall, or internal endpoint changes)?
- How does the API behave when rate limits (fomo.family's or its own) are exceeded?
- What happens when a requested trader or token no longer exists or has been delisted?
- How does the API handle stale or delayed data during fomo.family outages or maintenance?
- What happens when the consumer supplies invalid or expired authentication (if credentials are required)?
- How does the API respond when fomo.family changes its internal structure and the unofficial data source breaks?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The API MUST expose a way to retrieve the fomo.family leaderboard as a ranked list of traders with their public performance metrics.
- **FR-002**: The API MUST expose a way to retrieve a single trader's public profile by their identifier.
- **FR-003**: The API MUST expose a way to retrieve a specific trader's recent activity (actions, tokens, timestamps).
- **FR-004**: The API MUST expose a mechanism to receive alerts when tracked traders make qualifying buys.
- **FR-005**: The API MUST return data in a structured, machine-readable format with stable, documented field names.
- **FR-006**: The API MUST paginate list responses (leaderboard, activity) with consistent, predictable pagination controls.
- **FR-007**: The API MUST handle fomo.family unavailability gracefully and return clear, non-misleading responses — it MUST never fabricate or infer data it could not actually retrieve.
- **FR-008**: The API MUST respect fomo.family's Terms of Service and Privacy Policy and enforce reasonable request rate limits to avoid abuse or being blocked.
- **FR-009**: The API MUST return clear, actionable errors for invalid inputs, missing resources, and rate-limit conditions.
- **FR-010**: The API MUST support filtering activity by time window and, where applicable, by token or chain.
- **FR-011**: The API MUST indicate data freshness (timestamp of last successful refresh from fomo.family) so consumers know how current the data is.
- **FR-012**: The API MUST be strictly read-only: it retrieves fomo.family social and market data (leaderboard, profiles, activity, alerts) and MUST NOT place, close, or modify trades, perform copy-trades, or take any account action on the consumer's behalf.
- **FR-013**: The API MUST authenticate each consumer with their own fomo.family credentials/session to reach account-scoped data. The service MUST NOT centrally store or persist consumer credentials beyond what is required for the active session, and each consumer acts solely under their own fomo account.

### Key Entities *(include if feature involves data)*

- **Trader**: A fomo.family user with a public profile — handle, rank, follower count, performance metrics (as exposed by fomo, e.g., PnL/ROI/win-rate), and an activity history.
- **Trader Activity (Action)**: A time-stamped action by a trader — action type (buy/sell), associated token, amount/value where available, and chain.
- **Token**: A tradable asset on fomo.family — identifier, name, symbol, chain, and market metrics as exposed by fomo (price, volume, liquidity, market cap where available).
- **Alert**: A notification event generated when a tracked trader performs a qualifying buy — references the trader, token, timestamp, and qualifying criteria.
- **Leaderboard**: An ordered, paginated ranking of traders by a performance metric.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A consumer can retrieve the full leaderboard (paginated) and resolve any listed trader's profile using only the API, with no manual interaction with the fomo.family website.
- **SC-002**: Leaderboard and profile data returned by the API matches the equivalent data shown on fomo.family within a 60-second freshness window for at least 95% of requests during normal operation.
- **SC-003**: A consumer can retrieve a tracked trader's activity feed and receive alerts for that trader's qualifying buys, with alert delivery latency under 5 seconds from the corresponding fomo.family event for at least 90% of alerts.
- **SC-004**: The API sustains at least 50 concurrent consumers querying read endpoints without degraded data freshness or increased error rate beyond defined limits.
- **SC-005**: At least 90% of consumers can make their first successful authenticated request within 10 minutes using only the API documentation.

## Assumptions

- The primary deliverable is a programmatic API over standard web protocols returning structured, machine-readable (JSON) responses, rather than an embedded library or SDK; language-specific client helpers may follow later.
- The API targets fomo.family's social and market data as advertised on its site (leaderboard, trader profiles, trader feeds, real-time "top trader buy" alerts, and token/market data).
- fomo.family exposes no official public API; the API obtains data through the site's public/semi-public surfaces. The implementer MUST confirm compliance with fomo.family's Terms of Service and Privacy Policy before launch.
- The API is strictly read-only: it retrieves fomo.family social and market data and does not place trades or perform account actions (confirmed scope decision).
- Access model: each consumer authenticates with their own fomo.family credentials/session to reach account-scoped data; the service does not centralize or persist consumer credentials beyond the active session (confirmed access decision).
- Data freshness target is near-real-time (seconds to low minutes) for social data, and the API surfaces a "last refreshed" timestamp.
- Pagination, rate limiting, and clear error responses follow standard API conventions.
- The API is a separate consumer of fomo.family and does not modify fomo.family; it does not place trades or perform account actions unless Q1 confirms a write scope.
- Chain/token coverage follows fomo.family's own coverage (multichain, memecoin-focused); the API does not add chains or tokens that fomo does not support.
- Performance targets assume typical web-API expectations; specific throughput targets will be refined during planning once the data-source constraints are known.
