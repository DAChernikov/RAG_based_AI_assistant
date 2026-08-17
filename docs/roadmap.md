# Product roadmap

## Completed product scope

- Repository foundation and self-hosted OpenAI-compatible generation gateway.
- PostgreSQL system of record, Redis Streams, queued/resumable inference and Telegram API client.
- Local authentication, tenant isolation, RBAC, refresh sessions, scoped API keys and audit.
- Knowledge-base/source catalog with immutable Website, Git and PostgreSQL JDBC metadata versions.
- Hardened incremental ingestion, managed JDBC registry and worker lease fencing.
- BGE-M3-compatible embedding HTTP service, pgvector index lifecycle and indexing worker.
- Tenant/KB-isolated dense + PostgreSQL full-text retrieval, RRF and optional HTTP reranking.
- Multi-label routing, grounded citations and sqlglot AST/schema/read-only EXPLAIN pipeline.
- Scheduler, retention, model/prompt registries, evaluation and observability.
- React/TypeScript Web UI, hardened Compose, Helm artifacts, CI/security/SBOM gates and runbooks.

The active release surface and limitations are documented in `README.md`; capabilities are not inferred from old iteration documents.

## Optional extensions

- JS-rendered crawling and site-specific enterprise adapters.
- Managed repository groups and additional structurally parsed languages.
- Additional reviewed JDBC vendors/JVM adapters.
- Provider-specific infrastructure modules and signed-image supply chain.
- A separately measured SQL expert model when evaluation demonstrates a gain.
