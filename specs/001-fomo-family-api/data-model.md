# Data Model: Fomo Family API

**Feature**: 001-fomo-family-api
**Date**: 2026-07-23
**Status**: Phase 1 design output

This document defines the public data shapes exposed by the API. These are
**our API's** public schemas (Pydantic v2), not fomo.family's internal
formats — the `fomo_client` adapter translates fomo's responses into these
shapes. All field names are stable and documented (FR-005).

Entities derive from the spec's Key Entities section. Field types are
normative for our API; where a field mirrors fomo data that may be absent,
it is optional (`?`) and documented.

## Entity: Trader

A fomo.family user with a public profile.

| Field | Type | Required | Validation / Notes |
|-------|------|----------|---------------------|
| `id` | string | yes | Stable fomo trader identifier (opaque to consumers). Non-empty. |
| `handle` | string | yes | Display handle. Non-empty. |
| `rank` | integer | yes | Leaderboard rank. ≥1. |
| `followers_count` | integer | yes | Follower count. ≥0. |
| `metrics` | TraderMetrics | yes | Performance metrics as exposed by fomo. |

### TraderMetrics

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `pnl_pct` | number? | no | ROI/PnL percentage as exposed by fomo. May be null. |
| `win_rate_pct` | number? | no | Win-rate percentage. 0–100. May be null. |
| `volume_usd` | number? | no | Trading volume in USD. ≥0. May be null. |

> Only metrics fomo actually exposes are populated; the adapter MUST NOT
> fabricate metrics it could not retrieve (FR-007). Unknown/missing metrics
> are `null`, never inferred.

## Entity: TraderActivity (Action)

A time-stamped action by a trader.

| Field | Type | Required | Validation / Notes |
|-------|------|----------|---------------------|
| `id` | string | yes | Stable action identifier. Non-empty. |
| `trader_id` | string | yes | FK → Trader.id. Non-empty. |
| `action` | enum | yes | One of `buy`, `sell`. |
| `token` | TokenRef | yes | The token involved. |
| `amount_usd` | number? | no | Trade value in USD if exposed. ≥0. |
| `chain` | string | yes | Chain identifier (e.g., `solana`, `base`). Non-empty. |
| `timestamp` | string (RFC 3339) | yes | ISO-8601 UTC. Parsed/validated. |

## Entity: Token (and TokenRef)

A tradable asset on fomo.family. `Token` is the full market view;
`TokenRef` is the lightweight reference embedded in an action.

### Token

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | string | yes | Stable token identifier. Non-empty. |
| `name` | string | yes | Display name. Non-empty. |
| `symbol` | string | yes | Ticker symbol. Non-empty. |
| `chain` | string | yes | Chain identifier. Non-empty. |
| `price_usd` | number? | no | Current price in USD. ≥0. May be null. |
| `volume_24h_usd` | number? | no | 24h volume in USD. ≥0. May be null. |
| `liquidity_usd` | number? | no | Pool liquidity in USD. ≥0. May be null. |
| `market_cap_usd` | number? | no | Market cap in USD. ≥0. May be null. |

### TokenRef

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | string | yes | FK → Token.id. |
| `symbol` | string | yes | Ticker symbol. |
| `chain` | string | yes | Chain identifier. |

## Entity: Alert

A notification event generated when a tracked trader makes a qualifying buy.

| Field | Type | Required | Validation / Notes |
|-------|------|----------|---------------------|
| `id` | string | yes | Unique alert event id. Non-empty. |
| `trader_id` | string | yes | FK → Trader.id. |
| `token` | TokenRef | yes | The token bought. |
| `amount_usd` | number? | no | Buy value in USD if exposed. ≥0. |
| `timestamp` | string (RFC 3339) | yes | ISO-8601 UTC of the qualifying buy. |

## Entity: Leaderboard

An ordered, paginated ranking of traders.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `traders` | Trader[] | yes | Current page, ranked by `rank` asc. |
| `page` | Page | yes | Pagination metadata. |

### Page (shared pagination envelope)

| Field | Type | Required | Validation / Notes |
|-------|------|----------|---------------------|
| `page` | integer | yes | 1-indexed current page. ≥1. |
| `page_size` | integer | yes | Items per page. 1–100. |
| `total_items` | integer? | no | Total count when known. ≥0. |
| `total_pages` | integer? | no | Total pages when known. ≥1. |

## Envelope & Freshness

All responses (single-resource and lists) carry a freshness marker per
FR-011:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `data` | object \| array | yes | The payload (entity, or list of entities). |
| `last_refreshed_at` | string (RFC 3339) | yes | Timestamp of last successful refresh from fomo. |
| `page` | Page? | no | Present on list responses only. |

## State Transitions

### Consumer Session

```
absent → authenticating → active → expired/revoked → absent
```

- `authenticating`: Privy login in progress (headless browser).
- `active`: session token held in volatile store with short TTL; refreshes on
  activity; consumer API key valid.
- `expired/revoked`: TTL elapsed, explicit logout, or upstream 401 → session
  removed from volatile store; consumer must re-authenticate. No credentials
  persist (FR-013).

### Alert Subscription

```
unsubscribed → subscribed → unsubscribed
```

- `subscribed`: SSE stream open for a set of trader ids; upstream poller
  active for those ids; events fan out via Redis pub/sub.
- `unsubscribed`: stream closed or explicit unsubscribe; no further events
  delivered to that consumer (US3 acceptance 3).

## Validation Rules (from requirements)

- Pagination `page_size` MUST be 1–100 (FR-006).
- Activity time-window filters MUST accept `from`/`to` RFC 3339 bounds
  (FR-010); `to` ≥ `from` when both supplied.
- All `timestamp`/`*_at` fields MUST be RFC 3339 UTC and validated on output.
- Missing/unknown fomo fields MUST be `null`, never fabricated (FR-007).
- Errors MUST use a stable error envelope (see contracts/rest-api.md) (FR-009).
