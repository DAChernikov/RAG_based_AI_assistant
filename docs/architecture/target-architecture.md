# Target architecture

Status: target vision. Unless a capability is listed as current in the README, it is not
implemented yet.

## Product scope

The target product is a multi-user, self-hosted assistant for technical documentation,
websites and corporate articles, Git repositories and source code, relational database
metadata, SQL generation and validation, and hybrid questions that need several of these
sources at once.

The supported user-facing channels will be:

- React/TypeScript Web UI with chat, history, sources, SQL validation details, and
  administration;
- Telegram bot acting only as an API client;
- external HTTP API.

All generation and embedding models run as local or self-hosted HTTP services. Commercial
external LLM APIs are outside the architecture.

## Architectural shape

Business capabilities remain a modular monolith so domain rules, transactions, and contracts
can evolve without a network boundary for every module. Independent processes are introduced
only where isolation, hardware placement, or scaling justify them:

- FastAPI API;
- React/TypeScript Web UI;
- Telegram worker;
- inference worker;
- ingestion worker;
- scheduler;
- generator and embedding model servers;
- isolated JDBC connector;
- PostgreSQL with pgvector;
- Redis.

The API and workers import the same application/domain modules. Connectors implement stable
ports rather than becoming a collection of small services.

## Target runtime topology

```text
Web UI / Telegram / HTTP client
              |
              v
        FastAPI API + SSE
              |
      PostgreSQL application state
              |
              v
        Redis Streams jobs
         /             \
        v               v
Inference worker   Ingestion worker <--- Scheduler
        |               |
        |               +-- Web connector
        |               +-- Git connector
        |               +-- isolated JDBC metadata connector
        |               +-- versioned parsing/chunking/index activation
        |
        +-- multi-label router -> RoutePlan
        +-- docs/code/schema retrieval branches
        +-- merge/rerank
        +-- SQL validation / EXPLAIN / repair
        +-- Model Gateway
                 |                 |
                 v                 v
        generator model API   embedding model API
```

PostgreSQL stores application state, source/version metadata, conversations, messages,
jobs, prompts/models, audit records, feedback, and vector data through pgvector. Redis Streams
is a delivery mechanism for user inference and ingestion jobs, not the system of record.

## Request lifecycle

1. The API authenticates the caller, authorizes the requested resources, and persists the
   user message.
2. It appends an inference job to Redis Streams.
3. An inference worker claims the job using a consumer group.
4. A multi-label router creates a typed `RoutePlan`. The plan can contain multiple intents
   and retrieval targets instead of one exclusive `docs | code | sql` label.
5. Documentation, code, and database-schema retrieval branches run as needed.
6. Results are normalized, merged, deduplicated, and reranked.
7. The Model Gateway invokes the selected self-hosted generator model over HTTP.
8. SQL branches perform AST validation, schema validation, read-only enforcement, `EXPLAIN`,
   and bounded repair before an answer is accepted.
9. Tokens and structured events stream to the API and user through SSE.
10. The final answer, citations, route plan, SQL validation, latency/token metrics, and
    feedback are persisted.

Iteration 2 implements PostgreSQL state, Redis inference jobs/events, one inference worker,
heartbeat, direct/queued modes and versioned SSE. The router remains a single-label heuristic;
multi-label plans, hybrid retrieval, pgvector and AST validation are future work.

## Knowledge source catalog

Administrators will register typed sources with access policy, credentials by reference,
schedule, parsing policy, include/exclude rules, and active version.

### Website sources

Website ingestion supports HTML pages, documentation sites, Habr, Confluence, tables, code
blocks, lists, and other textual elements. Authentication is connector-specific. Audio and
video extraction are explicitly out of scope.

Iteration 5 implements bounded HTML crawling, sitemap and link discovery, canonical URLs,
conditional HTTP discovery, structured extraction and SSRF checks before every request and
redirect. Site-specific browser rendering and Confluence adapters are future extensions.

### Git sources

Git ingestion supports a repository or managed group, branch/tag/commit pinning, recursive
file traversal, include/exclude patterns, credential references, and structure-aware parsing
for supported languages.

Iteration 5 implements one repository per source, exact ref-to-commit resolution, safe
recursive tracked-file traversal, incremental checksums/rename detection and structural chunks
for Markdown, Python, SQL, Scala/Java and YAML/JSON. Repository-group orchestration remains
future work.

### JDBC metadata sources

The isolated JDBC connector exposes metadata only. Administrators choose a driver from a
managed allowlist, provide a JDBC URL and credential reference, and restrict catalog/schema
filters. The connector collects tables, views, columns, types, comments, primary keys,
foreign keys, indexes, and relationships.

Arbitrary JDBC JAR upload is forbidden. Every allowed driver has a managed version and
checksum. Network policy, read-only database roles, query timeouts, and metadata-only
operations limit connector impact.

## Immutable source version lifecycle

Each source refresh will:

1. discover changed and deleted source objects;
2. create an immutable source version;
3. parse and chunk changed objects;
4. reuse embeddings for byte/content-identical chunks;
5. write a staging index version;
6. run completeness, integrity, and retrieval smoke checks;
7. atomically activate the new version;
8. retain version metadata needed for rollback.

Queries pin or resolve an active knowledge-base version so results remain explainable during
concurrent ingestion. Failed staging versions never become visible.

Iteration 5 stores normalized documents and chunks through tenant-scoped content-addressed
blobs, so immutable version rows can reuse unchanged content. PostgreSQL remains authoritative;
a separate Redis Stream delivers refresh jobs to a dedicated ingestion worker. Durable job
leases, heartbeats, pending reclaim, bounded retry, cancellation and a DLQ protect repeated
delivery. Embedding/index staging and scheduled refresh are not implemented yet.

## Retrieval and answer composition

Retrieval becomes hybrid: lexical and vector candidates, source-specific structural signals,
metadata filters, deduplication, and reranking. Code chunks retain repository/ref/path/symbol
metadata. Database chunks retain catalog/schema/object and relationship metadata. Website
chunks preserve canonical URL and document structure.

The router emits multiple labels and explicit targets, for example:

```json
{
  "intents": ["explain_behavior", "generate_sql"],
  "retrieval_targets": ["documentation", "code", "database_schema"],
  "requires_sql_validation": true
}
```

The exact contract will be versioned before implementation. The current exclusive baseline
router remains for backward compatibility until that migration.

## Model serving

The initial generator is Qwen2.5-Coder-7B-Instruct in GGUF Q4_K_M form, served by native
Ollama or llama.cpp through an OpenAI-compatible HTTP API. On macOS it runs outside Docker
to use Apple Metal; containers use `host.docker.internal`. Only one generator model should
remain resident in memory.

BAAI/bge-m3 is the target embedding model and will run as a separate HTTP service. It is not
integrated yet. XiYanSQL-QwenCoder-7B-2504 is a possible later SQL expert and must not be
downloaded or loaded during the foundation stages. A production profile may select
Qwen3-Coder-30B-A3B-Instruct on separate hardware; it is not the default laptop profile.

The Model Gateway owns provider-neutral request/response contracts, timeouts, retries,
streaming, model selection, and future observability. It never loads weights into the API
process.

## S3-compatible model artifact registry

S3-compatible storage is future cold storage and a model artifact registry, not an inference
runtime. A model artifact has a neighboring manifest containing model id, role, version,
format, quantization, byte size, SHA-256, and object key.

A future runtime downloads into a temporary local file, validates size and SHA-256, atomically
moves the artifact into a local cache, and activates it only after verification. With the
current 10 GB storage limit, the registry should retain one active quantized generator, one
embedding model, manifests, and minimal extra artifacts.

The existing S3 download path for retriever artifacts now uses lazy client creation, bounded
safe ZIP staging, smoke validation and atomic activation. The trusted `joblib` format remains
a documented risk. Model registry uploads and remote S3 writes are not implemented.

## Neon demonstration JDBC source

Neon PostgreSQL is planned as the first demonstration JDBC metadata source. It will use:

- a dedicated `rag_demo_source` schema;
- a dedicated read-only user;
- the allowlisted PostgreSQL JDBC driver with a pinned checksum;
- SSL;
- an allowlist limiting scans to the demonstration schema;
- metadata-only access.

No Neon connection, schema change, user creation, or metadata scan is part of the current
iteration.

## Security, tenancy, and operations

Authentication and RBAC will scope conversations, sources, credentials, jobs, and admin
operations. Credentials are stored only in a secret manager or environment-backed credential
references; they are never embedded in source records, logs, prompts, or Git.

Audit events cover source administration, access decisions, model/prompt version selection,
SQL validation, and privileged operations. Metrics and traces cover queue latency, retrieval,
model inference, ingestion, token usage, failures, and SSE delivery. Structured logs redact
secrets and sensitive connection strings.

## Compatibility during migration

Until explicitly versioned replacements are available, the product keeps `/health`, `/ready`,
`/ask`, `/ask/stream`, Telegram behavior, current routing modes, retriever artifacts, and SQL
validation/repair contracts. Future async internals should be introduced behind these public
interfaces or through explicitly versioned API changes.
