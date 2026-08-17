# PostgreSQL backup and restore

PostgreSQL is the system of record; Redis is disposable transport state. Back up the database, content blob storage and reviewed deployment configuration/manifests. Model cache can be reconstructed from verified artifacts.

Use encrypted `pg_dump --format=custom` or managed snapshots on a schedule matching owner-defined RPO/RTO (`RPO: ___`, `RTO: ___`). Capture database version, migration revision and application release. Test restore quarterly in an isolated network.

Restore procedure: stop API/workers/scheduler, provision the same/newer PostgreSQL with pgvector, restore the dump, verify `alembic current`, tenant counts and active source/index references, run only compatible migrations, start Redis empty, then services. Requeue durable queued/running jobs using the worker recovery runbook. Verify citations and index activation before reopening traffic.

Loss of Redis must not delete PostgreSQL jobs; recreate consumer groups and recover non-terminal runs. Loss of model cache requires checksum-verified re-download/preload. Loss of a worker requires no data restore. Never restore untrusted pickle/joblib artifacts.
