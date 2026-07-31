# Knowledge source catalog API

Status: admin-only catalog and on-demand Website/Git ingestion are implemented. Scheduled
refresh and JDBC ingestion are not implemented.

All paths require an authenticated `admin`. Tenant identity comes from the principal.

## Knowledge bases

- `GET|POST /v1/admin/knowledge-bases`
- `GET|PATCH|DELETE /v1/admin/knowledge-bases/{knowledge_base_id}`
- `GET /v1/admin/knowledge-bases/{knowledge_base_id}/sources`
- `PUT|DELETE /v1/admin/knowledge-bases/{knowledge_base_id}/sources/{source_id}`

## Sources

- `GET|POST /v1/admin/knowledge-sources`
- `GET|PATCH|DELETE /v1/admin/knowledge-sources/{source_id}`
- `GET /v1/admin/knowledge-sources/{source_id}/versions`
- `GET /v1/admin/knowledge-sources/{source_id}/ingestion-runs`
- `POST /v1/admin/knowledge-sources/{source_id}/refresh`
- `POST /v1/admin/knowledge-sources/{source_id}/versions/{version_id}/activate`
- `POST /v1/admin/knowledge-sources/{source_id}/versions/{version_id}/rollback`
- `GET /v1/admin/ingestion-runs/{run_id}`
- `GET /v1/admin/ingestion-runs/{run_id}/events`
- `POST /v1/admin/ingestion-runs/{run_id}/cancel`

Create/update accepts one discriminated config with `source_type`:

- `website`: root URL, allowed domains, include/exclude patterns, crawl limits and optional
  credential reference;
- `git`: repository/group URL, branch/tag/commit ref, include/exclude patterns and optional
  credential reference;
- `jdbc`: allowlisted driver ID, opaque connection reference, catalog/schema allowlists and
  metadata policy. An optional JDBC URL must not contain user info or secret query parameters.

Unknown fields and plaintext secret-shaped fields are rejected. Full credentials are never
returned or stored. Version contents are created only by ingestion application services, not
by a public endpoint.

## Refresh jobs

Refresh requires an `Idempotency-Key` header and accepts:

```json
{"auto_activate": true}
```

The API returns `202` with the durable ingestion run. Reusing the key with the same source and
payload returns the same run; different input returns `409`. A queued or running job can be
cancelled. Completion is safe under Redis redelivery because a worker must own the matching
PostgreSQL lease token.

The event endpoint returns bounded Redis Stream history for a future admin UI. Events contain
only run/stage/status metadata; URLs with credentials, connector secrets and fetched content are
not included.

Website refresh discovers sitemap and HTML links within the configured allowlist, applies
include/exclude and crawl limits, extracts document structure, and uses ETag/Last-Modified plus
checksums for incremental comparison. Git refresh resolves the configured ref to a commit SHA,
uses an isolated temporary checkout, and records path/language/symbol/line/checksum metadata.

Successful processing follows:

```text
discover -> incremental diff -> fetch/parse/chunk
         -> staged -> validating -> ready -> optional active
```

Any processing error marks both the source version and ingestion run failed with a sanitized
message. Failed and staging versions are not retrieval-visible. Retrieval integration itself is
outside Iteration 5.

## Credential resolution

Catalog configs contain only an opaque value such as `credential:engineering-docs`. The default
self-hosted worker hashes that reference and reads `RAG_CREDENTIAL_<FIRST_16_SHA256_HEX>` from
its environment. The value is a JSON object with optional `http_headers` and `git_environment`
string maps. It belongs in the deployment secret facility, never in source config, examples,
logs or Git. A different secret manager can be integrated by implementing `CredentialResolver`.
