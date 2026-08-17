# Retention policy

Per-tenant policy has four independent horizons: `source_versions_days`,
`index_versions_days`, `run_history_days` and `audit_days`. Admin uses
`POST /v1/admin/retention/dry-run` before `run`; both calls return the same typed candidate
categories and are capped by the maintenance batch size.

GC removes terminal run events/jobs before their parent ingestion, indexing and inference runs;
then eligible superseded/failed index and source versions; finally unreferenced chunk embeddings
and content blobs. Foreign-tenant rows never enter the candidate query. Active source/index
versions, explicit source/index pins and all rows still protected by foreign keys are excluded.
Repeated execution is idempotent. Each real run creates a sanitized audit event containing only
category counts, never source content or credentials.

`dry-run` is an operator preview, not a transaction reservation: a concurrent activation can
change the final candidate set, so execution re-evaluates every protection predicate. PostgreSQL
backups follow a separate policy. The current legal-hold implementation is the explicit immutable
version pin; an organization needing case-level hold metadata should map its process to these pins
before running GC.
