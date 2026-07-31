# Knowledge source catalog API

Status: admin-only catalog implemented in Iteration 4. Connectors and ingestion scheduling are
not implemented.

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
- `POST /v1/admin/knowledge-sources/{source_id}/versions/{version_id}/activate`
- `POST /v1/admin/knowledge-sources/{source_id}/versions/{version_id}/rollback`

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
