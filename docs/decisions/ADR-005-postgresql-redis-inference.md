# ADR-005: PostgreSQL state with Redis Streams delivery

- Status: Accepted and implemented in Iteration 2
- Date: 2026-07-30

## Context

Inference must survive API timeouts and worker restarts while preserving conversations,
answers, citations and idempotency. Redis alone is not an appropriate durable business record,
and loading the retriever in every API process wastes laptop memory.

## Decision

Use local PostgreSQL as system of record for identity, conversations, messages, inference
jobs, answers and sources. Use Redis Streams for job delivery, bounded event retention,
consumer groups, pending reclaim, DLQ and worker heartbeat. The queued API never imports the
retriever; one inference worker owns it.

Keep `direct` mode for compatibility and diagnostics. Queued failures never silently fall back
to direct execution. Migrations and compatibility identity seeding are explicit operator
commands.

## Consequences

- Terminal results and idempotency survive Redis event expiry.
- API and worker require local PostgreSQL/Redis availability in queued mode.
- Database/stream contracts need migrations and versioning.
- Authentication is still required before development job/history endpoints can be exposed
  beyond a trusted local environment.
