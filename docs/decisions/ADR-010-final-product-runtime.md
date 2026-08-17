# ADR-010: Final self-hosted product runtime

Status: accepted.

## Context

The repository has authenticated API, PostgreSQL application state, Redis-backed inference
and ingestion workers, and immutable source versions. Its legacy retriever still loads
pickle-compatible artifacts and model weights inside the inference worker. Indexing, hybrid
retrieval, operations, UI and production deployment must converge on one supportable runtime.

## Decision

The final product is a modular monolith deployed as independently scalable processes:

- FastAPI API and React/TypeScript Web UI;
- inference, ingestion and indexing workers plus a singleton-leased scheduler;
- PostgreSQL with pgvector as system of record and retrieval index;
- Redis Streams only for bounded delivery, events, leases and coordination;
- provider-neutral generation, embedding and optional reranking HTTP clients;
- native macOS or separately deployed model servers; application processes never load weights;
- Website, Git and managed JDBC metadata connectors feeding immutable source versions;
- immutable hybrid index versions with atomic activation and rollback;
- tenant-scoped model/prompt registry, schedules, retention and audit records.

All user paths use queued inference. Authentication and tenant ownership are mandatory.
Production configuration fails closed. Secrets are represented only by environment or
credential references.

## Legacy removal

The pgvector path replaced direct mode,
`RetrieverLoader`, `ArtifactManager`, trusted joblib/NumPy corpora, the S3 ZIP downloader and
artifact volumes/settings/tests. ADR-003's retriever artifact workflow is
superseded by this decision; optional S3 use is limited to manifest-verified, non-pickle model
artifacts and remains disabled by default.

## Consequences

PostgreSQL and the embedding service become required for indexing and retrieval readiness.
Redis loss can delay delivery but cannot erase durable state. Rollout requires the pgvector
migrations and an explicit index build before chat has retrieval results. The runtime is larger
than the MVP but has one data model, one retrieval path and independently bounded processes.
