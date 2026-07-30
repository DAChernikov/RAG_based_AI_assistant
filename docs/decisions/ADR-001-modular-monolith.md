# ADR-001: Modular monolith for product business logic

- Status: Accepted
- Date: 2026-07-30

## Context

The product spans chat, retrieval, source ingestion, SQL safety, model selection, tenancy,
and administration. Splitting each capability into a service before contracts stabilize would
add deployment, versioning, observability, and failure-mode cost without proven scaling value.
Some workloads still require process or hardware isolation.

## Decision

Implement domain and application logic as a modular monolith with explicit module boundaries
and ports. Run separate processes only for the API, Web UI, Telegram worker, inference worker,
ingestion worker, model servers, JDBC connector, scheduler, PostgreSQL, and Redis.

API and workers reuse the same domain/application packages. Cross-process contracts are typed
and versioned. A module becomes an independent service only after isolation or scaling needs
are demonstrated.

## Consequences

- Refactoring and transactional changes remain straightforward while the product matures.
- Deployment has a small number of meaningful process boundaries.
- Module ownership and dependency direction require enforcement in code review and tests.
- Heavy or untrusted workloads can still be isolated without making every feature a service.

The current MVP is not yet reorganized into all target modules; this decision guides future
iterations rather than claiming that the target structure already exists.
