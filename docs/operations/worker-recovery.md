# Worker and DLQ recovery

Stop the failing worker class, inspect sanitized event/error codes and fix the dependency. PostgreSQL run/job state determines recovery; Redis entries alone never do. Reclaimed work requires a new lease token, so a fenced old worker cannot commit. Exhausted attempts become terminal `failed` and are copied to the bounded DLQ.

After remediation, restart one worker, verify heartbeat and reclaim, then retry failed user jobs through `/v1/inference-jobs/{id}/retry` or create a new idempotent ingestion/indexing run. Never replay raw DLQ records without checking tenant, durable status and idempotency key. Cancelled jobs remain terminal.
