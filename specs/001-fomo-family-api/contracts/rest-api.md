# REST API Contract: Fomo Family API

**Feature**: 001-fomo-family-api
**Date**: 2026-07-23
**Status**: Phase 1 design output

This contract defines the public REST surface of the API. All responses are
JSON with the envelope defined in [data-model.md](../data-model.md)
(`data`, `last_refreshed_at`, optional `page`). The API is strictly read-only
(FR-012). Consumers authenticate with a `Authorization: Bearer <consumer_key>`
header (the short-lived key issued after guided Privy login — see
research.md Decision 4).

Base path: `/v1`. All list endpoints are paginated (FR-006) with `page` and
`page_size` query params (`page_size` 1–100, default 20). All timestamps are
RFC 3339 UTC.

---

## Authentication

### POST `/v1/auth/login`

Begins the guided Privy login flow (headless browser). Returns a consumer API
key once fomo session capture succeeds. This is the only non-read endpoint and
performs no fomo trading action — it only establishes a session (FR-012 still
holds: no trades/account actions).

**Request body**: Privy login parameters (wallet/social method + inputs). The
exact fields are finalized during implementation against Privy's flow; marked
as the one auth-only write surface.

**Responses**:
- `200 OK`: `{ "consumer_key": "<string>", "expires_at": "<RFC 3339>" }`
- `401 Unauthorized`: credentials invalid or login failed.
- `429 Too Many Requests`: rate limited (FR-008).
- `502 Bad Gateway`: fomo.family/Privy unreachable (FR-007).

### POST `/v1/auth/logout`

Revokes the consumer key and drops the volatile session (FR-013).

**Responses**: `204 No Content`; `401` if key invalid.

---

## User Story 1 — Leaderboard & Trader Profiles (P1, MVP)

### GET `/v1/leaderboard`

Returns a ranked, paginated list of top traders.

**Query params**:
- `page` (int, default 1) — 1-indexed page.
- `page_size` (int, default 20, max 100) — items per page.

**Response `200`**:
```json
{
  "data": [
    { "id": "t_1", "handle": "legend", "rank": 1,
      "followers_count": 1200,
      "metrics": { "pnl_pct": 42.5, "win_rate_pct": 61.0, "volume_usd": 98000.0 } }
  ],
  "last_refreshed_at": "2026-07-23T19:40:00Z",
  "page": { "page": 1, "page_size": 20, "total_items": 500, "total_pages": 25 }
}
```
**Errors**: `401`, `429`, `502` (upstream unavailable), `503` (stale data;
never fabricated — FR-007).

### GET `/v1/traders/{trader_id}`

Returns a single trader's public profile.

**Path**: `trader_id` — stable fomo trader identifier.

**Response `200`**:
```json
{
  "data": { "id": "t_1", "handle": "legend", "rank": 1,
    "followers_count": 1200,
    "metrics": { "pnl_pct": 42.5, "win_rate_pct": 61.0, "volume_usd": 98000.0 } },
  "last_refreshed_at": "2026-07-23T19:40:00Z"
}
```
**Errors**: `401`, `404` (trader not found / delisted), `429`, `502`.

---

## User Story 2 — Trader Activity Feed (P2)

### GET `/v1/traders/{trader_id}/activity`

Returns a time-ordered list of the trader's recent actions.

**Query params**:
- `page`, `page_size` — pagination (FR-006).
- `from` (RFC 3339, optional) — inclusive lower time bound (FR-010).
- `to` (RFC 3339, optional) — inclusive upper time bound; `to` ≥ `from`.
- `chain` (string, optional) — filter by chain (FR-010).
- `token_id` (string, optional) — filter by token (FR-010).

**Response `200`**:
```json
{
  "data": [
    { "id": "a_1", "trader_id": "t_1", "action": "buy",
      "token": { "id": "tok_1", "symbol": "PUNCH", "chain": "solana" },
      "amount_usd": 250.0, "chain": "solana",
      "timestamp": "2026-07-23T19:39:00Z" }
  ],
  "last_refreshed_at": "2026-07-23T19:40:00Z",
  "page": { "page": 1, "page_size": 20, "total_items": null, "total_pages": null }
}
```
**Errors**: `401`, `404` (trader not found), `422` (invalid `from`/`to`
range), `429`, `502`. Empty result for a trader with no activity returns
`data: []` with `200` (US2 acceptance 3).

---

## User Story 3 — Real-Time Top-Trader Alerts (P3)

Alert delivery is over Server-Sent Events — see [alerts-sse.md](alerts-sse.md)
for the streaming contract. A REST helper exists to manage subscriptions:

### POST `/v1/alerts/subscriptions`

Registers a set of trader ids to track for this consumer.

**Request body**: `{ "trader_ids": ["t_1", "t_2"] }` (non-empty array).

**Response `200`**: `{ "subscription_id": "<string>", "trader_ids": ["t_1","t_2"] }`
**Errors**: `401`, `422` (empty/invalid `trader_ids`), `429`.

### DELETE `/v1/alerts/subscriptions/{subscription_id}`

Unsubscribes; no further alerts delivered to that consumer (US3 acceptance 3).

**Response**: `204 No Content`; `404` if subscription unknown.

### GET `/v1/alerts/stream` (SSE)

Opens the SSE stream delivering alert events for the consumer's active
subscriptions. See [alerts-sse.md](alerts-sse.md).

---

## Shared Error Envelope (FR-009)

All error responses use:
```json
{ "error": { "code": "UPSTREAM_UNAVAILABLE",
             "message": "fomo.family is currently unreachable",
             "details": { "status": 502 } } }
```
Stable `code` values: `UNAUTHORIZED`, `RATE_LIMITED`, `NOT_FOUND`,
`VALIDATION_ERROR`, `UPSTREAM_UNAVAILABLE`, `STALE_DATA`, `UPSTREAM_CHANGED`.

`UPSTREAM_CHANGED` is returned (not fabricated data) when fomo's internal
endpoints change shape and the adapter cannot map a response (spec edge case:
"fomo.family changes its internal structure").
