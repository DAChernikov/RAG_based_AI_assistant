# ADR-009: Managed PostgreSQL JDBC metadata connector

- Status: Accepted (Iteration 6)
- Date: 2026-07-31

## Context

Database metadata is needed for future schema retrieval and SQL validation. Accepting arbitrary
drivers, connection properties or SQL would create supply-chain, credential and data-access
risks. The connector must fit the existing immutable ingestion lifecycle without turning the
modular monolith into another network service prematurely.

## Decision

- Run metadata discovery in the dedicated ingestion worker behind the existing connector port.
- Maintain a versioned managed registry. Version `1` contains only the preinstalled PostgreSQL
  psycopg adapter and its manifest checksum; source configs cannot name modules or JAR files.
- Accept PostgreSQL JDBC URL syntax only as endpoint configuration. Reject embedded credentials,
  unknown properties and all drivers not present in the registry.
- Require exact host, database, catalog and schema allowlists, public DNS resolution,
  `sslmode=verify-full`, bounded connect/statement timeouts and credentials resolved from an
  opaque `connection_ref`.
- Establish a read-only transaction and execute only fixed, parameterized `pg_catalog` queries.
  Never read application rows or execute SQL supplied by an administrator.
- Normalize relation metadata deterministically and reuse the existing incremental checksum,
  source-version, content-addressed chunk, activation and rollback mechanisms.

## Consequences

PostgreSQL and Neon-compatible metadata can be ingested without adding a JVM process or accepting
untrusted driver artifacts. SQL Server, Oracle and other drivers remain unsupported even though
their URL forms are inspected for embedded-secret rejection. A separately isolated JVM connector
can be introduced later if a real supported-driver requirement justifies that boundary.

Neon provisioning is deliberately external to the application. The repository contains only
safe configuration structure; it does not contain credentials and Iteration 6 performs no remote
Neon connection or mutation.
