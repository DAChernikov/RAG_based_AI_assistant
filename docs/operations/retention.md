# Retention policy

Per-tenant policy covers superseded/failed source and index versions plus run/audit horizons. Admin uses `POST /v1/admin/retention/dry-run` before `run`; execution is bounded and repeatable. Active and pinned/rollback-protected versions are excluded. Referential constraints prevent deleting referenced content. PostgreSQL backups follow a separate policy and legal holds override GC.
