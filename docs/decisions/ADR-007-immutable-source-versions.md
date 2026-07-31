# ADR-007: Tenant-scoped catalog and immutable source versions

Status: accepted and implemented in Iteration 4.

## Context

Future Website, Git and JDBC connectors need a stable catalog and activation boundary before
they can ingest content safely. Updating an active index in place would make failures,
incremental change detection and rollback unreliable.

## Decision

- Keep knowledge bases, sources, links, versions, objects and ingestion runs in PostgreSQL.
- Scope every record and every repository query to a tenant.
- Store discriminated, versioned Website/Git/JDBC configurations. Configs may contain opaque
  credential references but never passwords, tokens or embedded URL credentials.
- Use the lifecycle:
  `discovered → ingesting → staged → validating → ready → active → superseded`, with failure
  exits from ingestion, staging and validation.
- Permit object and manifest writes only while `ingesting`; after `staged` they are immutable.
- Serialize activation on the source row, supersede the current active version in the same
  transaction and enforce a partial unique index for one active version per source.
- Implement rollback by atomically reactivating a `ready` or `superseded` version.
- Expose catalog management only through existing tenant administrators. Connectors and fake
  ingestion results are not public API operations.

## Consequences

Connector workers can later build and validate a complete staging version without affecting
retrieval. Failed versions cannot be activated. PostgreSQL is required for production-grade
activation locking; SQLite remains unit-test-only. Physical artifact deletion, retention and
garbage collection remain future work.
