# Inference contracts v1

Status: implemented for authenticated queued mode.

## Job contract `1.0`

Redis job messages contain one Pydantic-validated JSON contract with job, tenant, user,
conversation and message UUIDs, question, optional mode/top-k/token limit, creation timestamp,
and correlation ID. Credentials are forbidden. Unknown versions are ACKed only after a
sanitized DLQ entry is written.

## Event contract `1.0`

Events are discriminated Pydantic models:

- `queued`
- `started`
- `meta`
- `token`
- `retrying`
- `completed`
- `failed`

Every event has `event_contract_version`, application event UUID, monotonic per-attempt
sequence, job UUID, timestamp, correlation UUID, event type, and typed payload.

Redis assigns the SSE resume ID. Clients reconnect with `Last-Event-ID`. Streams have
configurable max length and TTL, so PostgreSQL—not Redis—remains the durable result source.

## Compatibility

Direct `/ask/stream` keeps the old `data: {"type": ..., "data": ...}` format. Queued
`/ask/stream` adds SSE `id` and `event`, sends the complete v1 contract, and also includes
legacy `type`/`data` aliases. `/v1/inference-jobs/{id}/events` emits only the versioned
contract.

All inference endpoints require either a JWT access token or a scoped API key. Tenant and user
identifiers come exclusively from the authenticated principal. A client without credentials
receives HTTP 401; access to another user's job, conversation or SSE stream is HTTP 404.
