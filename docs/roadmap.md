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

Proposed scope for separate approval:

- define PostgreSQL application-state schema and migrations for users/tenants, conversations,
  messages, jobs, and answer/source records;
- define versioned job and SSE event contracts;
- introduce Redis Streams and an inference worker with consumer groups, acknowledgements,
  idempotency, retry limits, and dead-letter handling;
- keep `/ask` and `/ask/stream` backward compatible while moving execution behind the job
  boundary;
- add local Compose services, integration tests, and observability basics;
- avoid source connectors, pgvector migration, and authentication UI until their contracts
  are separately reviewed.

Secrets, S3 model registry writes, Neon access, and large model downloads remain out of scope
unless explicitly authorized.

## Later planned stages

1. Authentication, tenant isolation, RBAC, audit log, and API credential management.
2. Knowledge source catalog and immutable version state machine.
3. Website and Git connectors with incremental ingestion and structure-aware parsing.
4. Isolated allowlisted JDBC metadata connector, followed by the Neon demo source.
5. BGE-M3 embedding service, pgvector, hybrid retrieval, and index activation/rollback.
6. Multi-label `RoutePlan`, parallel retrieval branches, merge/rerank, and hybrid answers.
7. SQL AST/schema validation, safe `EXPLAIN`, bounded repair, and only then evaluation of a
   dedicated SQL expert model.
8. React/TypeScript Web UI for chat, history, sources, SQL results, and administration.
9. Model/prompt registry, S3 model artifact cache workflow, evaluation framework, and mature
   observability.
10. Production deployment profiles and optional larger generator hardware.

Stage ordering may change after measurements, but no stage should claim a future capability
before its implementation and verification.
