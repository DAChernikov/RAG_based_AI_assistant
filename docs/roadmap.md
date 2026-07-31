# Product roadmap

This roadmap is directional. Each iteration must remain reviewable, preserve the declared
compatibility surface, update tests and documentation, and stop before the next iteration.

## Iteration 1 — Repository foundation and local model gateway

Status: implemented on `feature/product-v1`.

- audit and safely remove the obsolete Render/Gemini runtime layer;
- establish repository instructions and architecture decisions;
- call a self-hosted OpenAI-compatible generator through a provider-neutral client;
- support non-streaming and SSE streaming with retries and error mapping;
- document native macOS model serving and Docker host access;
- preserve API, Telegram, retriever, SQL validation, and existing S3 artifact behavior.

This iteration does not load models, write to S3, connect to Neon, or add product data
infrastructure.

## Iteration 2 — Application state and asynchronous inference foundation

Status: implemented on `feature/product-v1`.

- PostgreSQL application-state schema and Alembic migration;
- Redis Streams, consumer group, bounded retry, DLQ, pending reclaim and heartbeat;
- versioned jobs/events, resumable SSE and async job endpoints;
- idempotent answers and conversation history;
- direct/queued compatibility, local Compose profiles and structured metric logs;
- safe artifact staging and reproducible research workspace.

Authentication, source connectors, pgvector, Neon access, S3 model writes and automatic model
downloads remain out of scope.

## Iteration 3 — Authentication, tenant isolation and RBAC

Status: implemented on `feature/product-v1`.

- local Argon2id authentication and short-lived JWT access tokens;
- opaque rotating refresh sessions with reuse detection and family revocation;
- tenant/user ownership, `admin`/`user` RBAC and admin user management;
- scoped, expiring, revocable API keys for Telegram and external clients;
- audit events without prompts or plaintext credentials;
- concurrency-safe message sequences, idempotency and inference worker leases;
- dev/test-only compatibility identity and fail-closed production configuration.

Web UI, source connectors, pgvector, Neon access and retrieval/router changes remain out of
scope.

## Iteration 4 — Knowledge source catalog and immutable source versions

Status: implemented on `feature/product-v1`.

- tenant-scoped knowledge bases, sources, links, versions, objects and ingestion runs;
- versioned typed Website, Git and JDBC configs with credential references only;
- immutable manifests and objects after staging;
- validated lifecycle, one active version, atomic activation and rollback;
- admin-only tenant-isolated catalog API with security audit events;
- Redis-backed distributed login throttling;
- platform-exclusive native macOS and Linux CPU Torch dependencies.

Website crawling, Git clone, JDBC connectivity, parsing, embeddings and scheduling remain out
of scope. Lifecycle tests use an internal fixture connector through the application service.

## Iteration 5 — Website/Git connectors and incremental ingestion

Status: implemented on `feature/product-v1`.

- SSRF-protected Website crawling with sitemap/link discovery, limits and conditional fetch;
- isolated Git fetch pinned to a resolved commit, safe traversal and structure-aware parsing;
- incremental add/change/rename/delete discovery and content-addressed document/chunk reuse;
- separate Redis ingestion stream and worker with leases, reclaim, bounded retry and DLQ;
- admin refresh, status, event and cancellation API with tenant isolation and audit events;
- immutable staging, validation, optional atomic activation and sanitized failures.

JDBC connectivity, scheduler, embeddings, pgvector, retrieval integration and Web UI remain
out of scope.

## Later planned stages

1. Isolated allowlisted JDBC metadata connector, followed by the Neon demo source.
2. BGE-M3 embedding service, pgvector, hybrid retrieval, and index activation/rollback.
3. Multi-label `RoutePlan`, parallel retrieval branches, merge/rerank, and hybrid answers.
4. SQL AST/schema validation, safe `EXPLAIN`, bounded repair, and only then evaluation of a
   dedicated SQL expert model.
5. React/TypeScript Web UI for chat, history, sources, SQL results, and administration.
6. Model/prompt registry, S3 model artifact cache workflow, evaluation framework, and mature
   observability.
7. Production deployment profiles and optional larger generator hardware.

Stage ordering may change after measurements, but no stage should claim a future capability
before its implementation and verification.
