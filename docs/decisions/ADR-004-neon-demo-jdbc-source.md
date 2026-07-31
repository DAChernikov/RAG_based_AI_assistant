# ADR-004: Neon PostgreSQL as the first demonstration JDBC source

- Status: Accepted; connector compatibility implemented in Iteration 6
- Date: 2026-07-30

## Context

The JDBC connector needs a realistic first source while minimizing operational and data risk.
Arbitrary driver uploads, broad database credentials, and unrestricted schema scans would
create an unacceptable supply-chain and access surface.

## Decision

Use Neon PostgreSQL as the first demonstration JDBC metadata source. Provision a dedicated
`rag_demo_source` schema and a dedicated read-only user. Connect over verified TLS using a
versioned PostgreSQL metadata adapter from the managed registry. Store credentials by reference
and restrict discovery to the allowed schema.

The isolated connector performs metadata-only operations for tables, views, columns, types,
comments, primary/foreign keys, indexes, and relationships. Every driver version has a
checksum. The UI and API never accept arbitrary JDBC JAR uploads.

## Consequences

- The demonstration has a narrow, auditable access boundary.
- Driver lifecycle and checksums become managed product configuration.
- Metadata completeness is covered against local PostgreSQL; a deployment smoke test against
  an owner-provisioned Neon demo remains pending.
- A read-only role, SSL, allowlists, timeouts, and network policy are mandatory before first
  connection.

Iteration 6 implements the managed PostgreSQL metadata adapter and safe Neon-compatible config.
It does not connect to Neon, create schemas or users, inspect remote metadata, or modify any
remote database state.
