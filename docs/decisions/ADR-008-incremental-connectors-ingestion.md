# ADR-008: Incremental connectors and a dedicated ingestion worker

Status: accepted (Iteration 5)

## Context

Website crawling and Git ingestion perform untrusted network and filesystem work, can exceed an
API request lifetime, and must preserve the immutable source-version lifecycle. Redis delivery
can be repeated, while PostgreSQL must remain the source of truth.

## Decision

- Website and Git are adapters behind the shared connector contract. Iteration 6 adds the
  allowlisted PostgreSQL metadata adapter to the same ingestion boundary.
- Credentials are resolved only from opaque references through `CredentialResolver` and never
  stored in catalog configuration or job events.
- Each refresh atomically creates a PostgreSQL ingestion run and discovered source version,
  then publishes a versioned contract to a dedicated ingestion Redis Stream.
- The ingestion worker uses a database lease and token-checked completion. Reclaimed delivery
  resumes the same immutable version instead of creating another version.
- Normalized documents and parsed chunks reference tenant-scoped content blobs by checksum.
  New version rows are retained while unchanged text is not duplicated.
- Only `ready` or atomically activated versions are eligible for future retrieval. Staging and
  failed versions are never exposed.
- Website requests validate schemes, allowlists, DNS results and redirects, with private,
  link-local and metadata addresses denied by default.
- Git uses an isolated temporary checkout, exact commit resolution, disabled submodules/LFS,
  tracked-file traversal and conservative binary/secret/vendor exclusions.
- Truncated Website discovery never produces deletions, and every version/document mutation
  from a worker is fenced by the current database lease token.

## Consequences

The API remains responsive and ingestion scales independently from model inference. Delivery is
at-least-once but processing is idempotent around the database lease. Content blobs require a
future garbage-collection policy after version-retention rules are defined. JavaScript-rendered
sites and repository-group orchestration remain optional extensions; ADR-010 implements scheduler,
embeddings and retrieval integration.
