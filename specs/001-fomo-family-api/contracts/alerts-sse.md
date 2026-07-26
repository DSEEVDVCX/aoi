# SSE Alert Stream Contract: Fomo Family API

**Feature**: 001-fomo-family-api
**Date**: 2026-07-23
**Status**: Phase 1 design output

This contract defines the Server-Sent Events (SSE) stream for real-time
top-trader buy alerts (User Story 3). The downstream-to-consumer mechanism is
SSE (one-way push, HTTP-native); upstream ingestion is polling fomo's
activity endpoints, fanned out via Redis pub/sub (see [research.md](../research.md)
Decision 3 and [data-model.md](../data-model.md) for the `Alert` entity).

## Endpoint

### GET `/v1/alerts/stream`

Opens an SSE stream delivering `Alert` events for the consumer's active
subscriptions. Requires `Authorization: Bearer <consumer_key>`.

**Response**: `200 OK`, `Content-Type: text/event-stream`, `Cache-Control: no-cache`,
`Connection: keep-alive`.

## SSE Event Format

Standard SSE: lines prefixed `event:`, `data:`, and blank-line separators.
Each `data:` payload is a JSON `Alert` (see data-model.md).

```
event: alert
data: {"id":"al_1","trader_id":"t_1","token":{"id":"tok_1","symbol":"PUNCH","chain":"solana"},"amount_usd":250.0,"timestamp":"2026-07-23T19:39:00Z"}

```

## Event Types

| `event:` | `data:` payload | When sent |
|----------|-----------------|-----------|
| `alert` | `Alert` JSON | A tracked trader made a qualifying buy (US3 acceptance 1). |
| `heartbeat` | `{"ts":"<RFC 3339>"}` | Periodic keepalive (every ~15s) to keep the connection alive and detect dead consumers. |
| `subscribed` | `{"trader_ids":["t_1","t_2"]}` | Sent once on open, confirming the tracked set. |
| `error` | Error envelope (see rest-api.md) | Auth failed, rate limited, or upstream changed. Stream then closes. |

## Semantics

- **No false positives**: an `alert` event MUST only be sent when a tracked
  trader actually made a qualifying buy (US3 acceptance 2). The poller compares
  new activity against the last-seen watermark per trader; only qualifying,
  genuinely-new buys produce events.
- **Unsubscribe stops delivery**: after `DELETE /v1/alerts/subscriptions/{id}`
  (or closing the stream), no further `alert` events are delivered to that
  consumer (US3 acceptance 3).
- **Freshness**: the `Alert.timestamp` is the time of the qualifying buy on
  fomo, not the time of delivery.
- **Latency target**: delivery within 5s of the fomo event for ≥90% of
  alerts (SC-003), bounded by the upstream poll cadence (~5–15s) per
  research.md Decision 3.
- **Deduplication**: each `Alert.id` is unique; consumers SHOULD dedupe by
  `id` to tolerate at-least-once delivery across reconnects.

## Reconnection

SSE supports native reconnection via `retry:` (ms) and `id:` fields. On
reconnect, the consumer resumes from its last-seen `Alert.id`; events older
than the consumer's high-water mark are not replayed (alerts are live-only;
we do not persist history durably — FR-007/FR-013).

## Qualifying Buy Criteria

A buy "qualifies" for an alert when:
- The actor is in the consumer's tracked `trader_ids` set, AND
- The action type is `buy`, AND
- (Optional refinement, configured per implementation) the buy meets a
  notability threshold — e.g., minimum `amount_usd` — to avoid alert spam.

The default threshold (if any) is set during implementation and documented in
the quickstart; the contract guarantees only that qualifying buys produce
exactly one `alert` event each.
