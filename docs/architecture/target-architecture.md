# Runtime architecture

Status: implemented repository architecture; actual production rollout remains an operator responsibility.

## Product and boundaries

The product is a tenant-isolated self-hosted RAG assistant for Website documentation, Git code and PostgreSQL/JDBC metadata. Web, Telegram and external HTTP clients authenticate against FastAPI. All generation, embedding and optional reranking capabilities are self-hosted HTTP services; application processes never import model weights.

```text
Browser / Telegram / HTTP client
              |
       Web proxy -> FastAPI API -> PostgreSQL + pgvector (system of record)
                          |
                     Redis Streams (transport only)
               /----------+-----------\
      inference worker  ingestion worker  indexing worker
         | Router/RAG       | connectors       | embedding HTTP
         | SQL pipeline     + source versions  + immutable indexes
         + generation HTTP
                          ^
               leader-leased scheduler
```

The modular monolith shares domain/application/repository contracts while API, Web, Telegram, workers, scheduler and model servers remain independently bounded processes.

## Request path

The API derives tenant/user from JWT or scoped API key, validates knowledge-base ownership, persists message/job and enqueues a versioned contract. A fenced worker builds a multi-label `RoutePlan`, executes documentation/code/schema branches in parallel against the active index, fuses results with RRF and optional reranking, then sends bounded untrusted context to the Model Gateway. SQL output is parsed with PostgreSQL sqlglot dialect, restricted to one SELECT/WITH SELECT, checked against active schema, limited, optionally safely explained and fully revalidated after bounded repair. SSE emits resumable structured events; answer, route, citations, prompt/model versions and feedback remain durable.

## Knowledge lifecycle

Admin registers tenant knowledge bases and typed Website/Git/JDBC sources using credential references. Connectors enforce SSRF/allowlists, limits and immutable manifests. Each refresh discovers changes/deletions, creates a version, reuses content-addressed chunks and atomically activates only after validation. The ingestion worker automatically requests an index for linked knowledge bases.

Index lifecycle is `created → indexing → validating → ready → active → superseded`, with `failed` terminal. Embeddings are reused by tenant + text checksum + model version. Only one index is active per tenant/KB; activation and rollback lock competing versions. Retrieval sees only active entries, so removed content cannot leak from staged/old indexes. Provenance links tenant, KB, source/version, document/chunk and canonical URI.

## Security and operations

Argon2id, short JWTs, hashed rotating refresh tokens, hashed scoped API keys, CSRF cookies, exact CORS, rate limits and audit protect access. PostgreSQL composite constraints back application ownership checks. Redis leases/fencing and idempotency prevent stale commits and duplicate work. JDBC resolves/validates public IP once, connects to that IP while preserving TLS hostname, and uses fixed metadata/read-only SQL.

Scheduler emits tenant-aware idempotent refreshes under a leader lease. Retention uses dry-run/bounded deletion and never removes active/pinned versions. Structured logs omit sensitive content; Prometheus and optional OTLP expose operational signals. Compose and Helm provide non-root, capability-dropped, probe/resource/network-policy profiles. See deployment and operations runbooks for topology-specific steps.

## Supported extensions

JS-rendered crawling, new reviewed JDBC vendors and provider-specific infrastructure are extensions. S3-compatible storage is optional read-only cold model cache with manifest, size/SHA-256 validation and atomic local staging; it is not an inference filesystem and never accepts pickle/joblib artifacts.
