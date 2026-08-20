# Threat model

Protected assets are tenant content, credentials, user identity, model endpoints and audit integrity. Trust boundaries exist at browser/API, API/queues, workers/connectors, PostgreSQL/Redis and model HTTP services.

Controls: Argon2id, short JWT plus hashed rotating refresh tokens, hashed scoped API keys, CSRF double-submit cookies, exact CORS, rate limits and security headers; atomically locked and permanently closing first-run setup; tenant identity exclusively from principal; composite tenant constraints and ownership queries; lease fencing/idempotency; tenant-bound encrypted credential references whose master key remains outside PostgreSQL; JDBC allowlists, pinned public IP and TLS hostname; defused sitemap parsing; read-only AST/schema/EXPLAIN SQL; bounded bodies/streams/concurrency; immutable active indexes; structured redacted logs and audit.

Retrieved content is untrusted data and cannot override system instructions. It is delimited, size-bounded and cited. Model output remains untrusted: SQL is reparsed and fully revalidated after every repair. SSRF, DNS rebinding, credential leakage, cross-tenant object references, replayed refresh tokens, stale workers and dependency compromise have negative tests or scanners.

Residual risks: model hallucination, authorized source containing malicious text, browser/OS compromise, administrator abuse and misconfigured infrastructure. Mitigate with visible citations, least privilege, separate admin accounts, egress policies, backups, review of audit events, image signatures/SBOM and owner acceptance.
