# ADR-004: Neon PostgreSQL as the first demonstration JDBC source

- Status: Accepted for future implementation
- Date: 2026-07-30

## Context

The JDBC connector needs a realistic first source while minimizing operational and data risk.
Arbitrary driver uploads, broad database credentials, and unrestricted schema scans would
create an unacceptable supply-chain and access surface.

## Decision

Use Neon PostgreSQL as the first demonstration JDBC metadata source. Provision a dedicated
`rag_demo_source` schema and a dedicated read-only user. Connect over SSL using a pinned
PostgreSQL JDBC driver from the managed driver allowlist. Store credentials by reference and
restrict discovery to the allowed schema.

The isolated connector performs metadata-only operations for tables, views, columns, types,
comments, primary/foreign keys, indexes, and relationships. Every driver version has a
checksum. The UI and API never accept arbitrary JDBC JAR uploads.

## Consequences

- The demonstration has a narrow, auditable access boundary.
- Driver lifecycle and checksums become managed product configuration.
- Metadata completeness and Neon-specific behavior require connector integration tests.
- A read-only role, SSL, allowlists, timeouts, and network policy are mandatory before first
  connection.

Iteration 1 does not connect to Neon, create schemas or users, inspect metadata, or modify any
remote database state.
